# Decisions & findings log

Running log of design decisions and non-obvious findings, newest last.
Convention borrowed from the doctrine_to_h3 project.

## 2026-06 — Project setup

**Stack: PostgreSQL 17 + PostGIS 3.6.2, plain-Python loaders.**
Chosen over SpatiaLite (scale) and GeoPackage/QGIS-only (want scripted
scoring). The EDB winget package installs PG17; PostGIS came from the
OSGeo bundle installer, which also ships `h3_postgis` 4.1.4 — handy,
H3 hex indexing is an option for the suitability fabric later.

**Scope: WV + VA counties within 180 driving minutes of Alexandria.**
Drive time computed centroid-to-centroid with the public OSRM router
(`ingest/counties.py`). Result: 59 counties (54 VA, 5 WV).

- **OSRM's mountain routing excludes the classic eastern-WV cabin
  counties** — Pendleton, Grant, Tucker, Randolph, Pocahontas all land
  just past 180 min. If they should be in scope, set
  `DRIVE_TIME_MINUTES=210` in `.env` and re-run
  `ingest.counties` + `ingest.load_counties`.
- Python 3.14: `fiona` has no wheels and won't build without GDAL
  headers; use `pyogrio` (bundles GDAL) for any file-based geo I/O.

## 2026-06 — Parcel ingestion (Phase 2)

**WV: statewide `WV_Parcels` MapServer (WV GIS Tech Center).**
~1.39M parcels statewide, filtered by `CountyID`. Has owner name,
physical address, deeded acres (`Acres_C`), legal description.

- WV county codes are **alphabetical ordinals, not FIPS**:
  `fips = 54000 + (code * 2 - 1)`; inverse `code = (fips_last3 + 1) / 2`.
- `GISPID` is the stable unique parcel id.

**VA: statewide `VA_Parcels` MapServer (VGIN, quarterly refresh).**
~4.18M parcels statewide, filtered by `FIPS`.

- **Geometry-only**: no owner, no address, no assessed value. Those
  need per-county augmentation later — plan is to do that only for
  counties that survive suitability shortlisting.
- **Rappahannock County (51157) does not participate in VGIN** — zero
  parcels in the layer. It's prime cabin country, so it will need a
  county-direct source eventually. All other 53 in-scope VA
  jurisdictions are present.
- Acres computed from geometry (`ST_Area(geography)/4046.86…`);
  cross-checked against Madison County's known land area (within 2%).
- `LASTUPDATE` is preserved in `source_attrs` for future delta loads.

**Geometry hygiene: `ST_MakeValid` can return a `GeometryCollection`**
(polygon + sliver line) for self-intersecting inputs, which fails a
MULTIPOLYGON column. Both loaders wrap it in
`ST_CollectionExtract(…, 3)` and skip empties. This crashed the first
full VA run (Culpeper County) — see loaders for the pattern.

**Row counts after Phase 2** (2026-06-07): 2,090,681 parcels total —
145,476 WV (5 counties), 1,945,205 VA (53 jurisdictions).

## 2026-06 — Suitability architecture (Phase 3)

**Hybrid fabric: exact vector joins for vector layers, H3 hexes for
raster-derived layers.** Flood zones / streams are polygon data —
parcel intersection is exact and cheap against the 346K candidate set.
Slope/aspect/landcover come from rasters; those get aggregated onto H3
cells once (res 10 for slope), then parcel scoring is a hex join —
re-scoring never touches the rasters again. `h3` + `h3_postgis` 4.1.4
enabled in migration 005.

**Ingest infra (`ingest/http.py`, `ingest/staging.py`):** shared retry
session (5 tries, exponential backoff) and COPY-into-UNLOGGED-staging
bulk loads. Parcel loaders skip counties loaded within 30 days unless
`--force`, and a per-county failure no longer kills a run.

**FEMA NFHL via ESRI Living Atlas, not hazards.fema.gov** — FEMA's own
ArcGIS server refused TLS connections outright (curl exit 35). The
Living Atlas `USA_Flood_Hazard_Reduced_Set_gdb` mirror carries the full
NFHL schema (FLD_ZONE, SFHA_TF, STATIC_BFE). Fetched SFHA_TF='T' plus
'AREA NOT INCLUDED' per county bbox, deduped on FLD_AR_ID: 14,040
polygons AOI-wide. Per-parcel `sfha_pct` computed for all 345,793
candidates in ~8 min (single set-based query); 62,340 (18%) touch an
SFHA zone, only 3% are majority-floodplain.

**SQLAlchemy chokes on `%` in migration files** (placeholder parsing
even via `exec_driver_sql`); `manage.py migrate` runs raw psycopg
instead.

## 2026-07 — Slope fabric (Phase 3b)

**3DEP ImageServer computes slope server-side — but only correctly in a
projected output CRS.** `renderingRule={"rasterFunction":"Slope
Degrees"}` with `imageSR=4326` returns ~90° everywhere (rise in meters
over run in degrees). Requesting `imageSR=5070` (CONUS Albers, meters)
gives correct values, verified against Great North Mountain terrain
(mean 15.7°, max 52.8°). Pipeline fetches 2048px tiles at 10 m in 5070,
transforms pixel centers to WGS84 locally, and bins to H3 res 10
(~146 px/cell) with h3ronpy's vectorized `coordinates_to_cells`.

**Never polyfill parcels in SQL.** The first per-parcel metrics query
used `h3_polygon_to_cells` in a LATERAL over 385K candidates — one
Postgres backend, ~50 ms/parcel, killed after 6 hours. Replacement:
`ingest/parcel_cells.py` computes parcel→cell mappings with h3ronpy
(`wkb_to_cells`, ContainmentMode.Covers so sub-cell parcels still map)
— 4.48M mappings in 380 s — and stores them in `parcel_cells`. The
metrics query is then a plain join: 2 s. Rebuild parcel_cells after
every `refresh-candidates`.

**h3ronpy 0.22 API notes:** vectorized functions live in
`h3ronpy.vector` (not `.pandas.vector`); results are arro3 arrays whose
list scalars need `.as_py()`. `raster_to_dataframe` aggregates to one
value per cell — useless for within-cell percentiles, hence the manual
pixel binning.

## 2026-07 — Soils / septic (Phase 3c)

**SSURGO via per-survey-area zips + SDA tabular, not gSSURGO state
FGDBs.** Web Soil Survey's download cache has a scriptable URL pattern
`wss_SSA_{area}_[{saverest}]​.zip` (brackets URL-encoded; saverest date
from SDA's `sacatalog`). Polygons from `soilmu_a_*.shp` inside each
zip; septic rating ("ENG - Septic Tank Absorption Fields", dominant
component by comppct_r) + drainage class via one SDA query per area.
491,255 polygons for the AOI in ~3 min.

**Survey areas mostly map to counties as {state}{fips3}, with four
exceptions in our AOI** (multi-county surveys): James City→VA695,
King George→VA179, Hampshire→WV608, Hardy→WV628. See AREA_OVERRIDES.

**`overlaps` is a reserved word in Postgres** — can't be a CTE name.

**Septic is the great discriminator in the mountains.** Funnel over
candidates (5-50 ac): 183K sized → 149K dry → 117K flat → **43.5K
septic-workable (pct_septic_ok ≥ 50)**. Hardy County drops from
thousands of flat/dry parcels to **44 finalists**; Hampshire 14;
Morgan 12. Ridge-and-valley soils are overwhelmingly rated 'Very
limited' (95% of Hardy by area). Note: 'Very limited' ≠ unbuildable —
alternative/engineered systems exist — so scoring should penalize,
not exclude.

## 2026-07 — Post-reload staleness

**A parcel reload does not cascade.** `candidate_parcels` is a
materialized snapshot and `parcel_metrics` is keyed on parcel id, so
after any parcel load the sequence is:

    python manage.py refresh-candidates
    python manage.py metrics flood      # and future layers

Learned the hard way: VGIN's July quarterly refresh added ~114K VA
parcels (2.09M → 2.20M; candidates 346K → 385K), and the matview +
flood metrics silently reflected June until manually refreshed.
Loaders now print a reminder; wiring auto-refresh into the loaders was
considered and rejected (a multi-county load session would refresh
repeatedly — the 10-min metrics pass belongs at the end, invoked once).

## 2026-06 — Scope pruning

**`cabin_relevant` flag on `counties_in_scope`; `candidate_parcels`
materialized view.** Suitability scoring cost (raster zonal stats)
scales with parcel count, and most of the 2.09M parcels are urban
lots. Cities (VA FIPS last-3 ≥ 510) and the urban core counties
(Arlington, Fairfax, Prince William, Henrico, Chesterfield) are marked
not relevant; the view further filters to 2–1000 acres. Judgment
calls, deliberately easy to flip with an `UPDATE` + `REFRESH
MATERIALIZED VIEW`. Loudoun and Stafford kept relevant (western/southern
portions still hold acreage) despite suburbanization.
