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
