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

## 2026-07 — Roads (Phase 3c)

**TIGER roads are barely a filter, mostly a signal.** 214K segments
(S1100/S1200/S1400 public + S1500/S1740 kept for context) from
per-county TIGER2024 zips. 92% of candidates sit within 100 m of a
mapped public road, so "has access" cuts the funnel only 43.5K→41.9K.
The discriminating tail is the ~2.2K parcels >500 m out — treat
distance-to-road as a Phase 4 scoring axis (seclusion vs. access
cost), not a screen.

**KNN metrics cost 2 hours** (385K lateral KNN + geography recheck of
5 candidates each). Fine as a one-time pass; if it becomes routine,
precompute in a projected CRS or band with ST_DWithin first.

## 2026-07 — Scoring (Phase 4)

**Weighted-sum view, weights in a table.** `parcel_scores` computes six
0-100 component scores (flood, slope, septic, size, drive, seclusion)
and a weighted total; `scoring_weights` holds the weights (defaults:
septic 25, slope 20, flood 15, drive 15, size 15, seclusion 10).
Tuning = UPDATE + requery. Component curves (piecewise linear) live in
the view SQL — changing a curve is a migration, changing a weight is
not. NULL components count as neutral 50 in the total but stay NULL in
their column so data gaps remain visible.

**Default weights favor close-in suburbia** — top-15 is all Loudoun/
Stafford/Fauquier, which are flat, dry, septic-fine, 60-75 min out,
and unaffordable. The missing axes are price (Phase 5 listings) and
cabin character (forest cover, terrain relief context). Until those
land, use `manage.py shortlist --state/--county/--max-drive` to cut
by region.

## 2026-07 — Listings (Phase 5)

**First source: WV delinquent lands, not MLS.** The WV GIS Tech Center
publishes a statewide Delinquent Properties service keyed by GISPID —
the same id as our parcels, so the join is exact (99% match). 1,128
listings across the 5 WV counties. Status semantics: 'No Bid' = unsold
at tax auction, often purchasable directly from the WV State Auditor
(the standing inventory); 'Deed' = tax deed in process; 'Redeemed' =
owner paid, gone. `manage.py shortlist --for-sale` joins the scored
parcels to active-ish listings (No Bid/Deed/Suspended).

**listings is multi-source by design** — UNIQUE(source,
source_listing_id), listing_kind enum-ish, optional parcel_id +
point geom for sources that don't share parcel keys (geocoded FSBO,
RSS feeds). Candidate next sources: VA county tax sales (scattered,
per-county auctioneer sites), state surplus property, FSBO feeds.
Commercial MLS/Zillow/LandWatch scraping stays out of scope (TOS).

## 2026-07 — Public land, structures, VA listings reality

**Public-land adjacency: PAD-US 4.1 fee lands** via the USGS AGOL
`Manager_Name_PADUS` FeatureServer, `FeatClass='Fee'`, per-county bbox,
deduped on OBJECTID → 2,409 polygons AOI-wide (George Washington NF
dominates). New `public` scoring component (weight 10): 100 when
within 100 m, tapering to 30 at 5 km; dist NULL after compute means
">5 km" (score 30), distinct from not-computed (neutral 50).

**Structures: FEMA/ORNL USA Structures**, county-FIPS server-side
filter + `returnCentroid` (no polygon payloads). Stored as centroid
points; `has_structure` = any centroid inside parcel. This is
imagery-derived "known structure", NOT a permit record — county permit
systems aren't accessible. Displayed + filterable, deliberately not
scored (a structure can be an asset or a teardown).

**VA for-sale data does not exist statewide.** Tax sales run
per-county (Va. Code 58.1-3965) through private auctioneers; lists
appear ~3 weeks pre-auction on taxva.com (TACS) and
forsaleatauction.biz (27+ localities). Scraping them is TOS-gray and
the lists are ephemeral. Path chosen: `ingest/listings_csv.py` manual
importer (same listings table, parcel-joined by county+parcel id) —
transcribe auction lists for counties that matter when they drop.
Also note VGIN parcels carry no owner/address, so VA listings often
must be matched by parcel id from the auction notice itself.

**Weights now carry `default_weight`** so the UI can reset after
tuning experiments.

## 2026-07 — Assessment values (WV)

**WVGISTC annual tax product, not the Blazor portal.** mapwv.gov's
assessment viewer is Blazor Server (SignalR — not scrapeable), but the
same data ships as per-county zips on the WV GIS Clearinghouse
(`.../Parcels/WVGISTC_2025/CountySplits/`). ParcelSummary DBF carries
appraised land/building/total, YearBuilt, LandUse, TaxClass, deed
book/page + latest-transfer refs. Multi-card parcels aggregated
(values summed). Join: ParcelSummary.CleanParce == the packed
`clean_parcel_id` we stored in parcels.source_attrs at load. ~99.6%
match.

**No sale prices anywhere public in WV** — deed references only;
consideration amounts live in courthouse records. Same for VA, where
assessments themselves are also per-county (no statewide source);
VA value data = future per-county augmentation for shortlist counties.

**Value is context, not score** — `value_per_acre` and
`appraised_total` are exposed in parcel_scores and filterable
(max $/ac, max total), deliberately not a scoring component: cheap
Hardy land is cheap *because* it is steep/septic-hostile, and the
suitability components already capture that. The interesting query is
high score + low $/ac — e.g. 32 ac active farm at $204/ac, 160 m from
GW National Forest.

## 2026-07 — Remoteness (neighbors + convenience)

**Neighbor privacy measured directly from dwellings, not road
distance.** nbr_dist_m = parcel boundary to nearest OFF-parcel
dwelling (structures classed Residential + Unclassified, per user
decision); nbr_cnt_500m/1km separate "one distant farmhouse" from
"subdivision edge". Structures backfilled for 32 adjacent/border
counties incl. MD across the Potomac (2.4M dwelling points total) so
AOI-edge parcels don't look falsely private. The KNN + count pass
took 4.7 h — the most expensive metric in the project; rerun only
after structure reloads.

**Scarcity finding:** only 4,967 of 385K candidates (1.3%) have no
dwelling within 500 m; 915 meet ">=800 m and <=3 within 1 km". True
seclusion is the rarest attribute in the corridor — rarer than flat,
dry, or septic-workable.

**Convenience: OSM POIs (Overpass w/ mirror fallback — the main
instance 504s routinely) + Census places from TIGERweb Census2020
(POP100; the ACS API now requires a key, TIGERweb doesn't).**
Straight-line distances; in ridge-and-valley terrain road distance
can be ~2x, fine for ranking, use OSRM one-offs for finalists.
Validation: Hampshire avg 15 km grocery / 26 km town vs Stafford ~5 km
both.

**Scoring:** neighbors (15, distance curve minus density penalty),
remoteness (10, remote-positive saturating at 30 km, 60/40
grocery/town), seclusion demoted to 5 (access cost only). Grocery
scoring direction (monotonic remote-positive vs sweet-band) chosen by
user: monotonic.

## 2026-07 — VA county augmentation + the stale-vintage bug

**The statewide "VA parcels" vendor on AGOL (wharcgisdeveloper, ~130
localities, uniform naming) is token-walled** — a commercial reseller.
Real path: county-run open services, found one by one. Registry lives
in `ingest/parcels_va_augment.py`; join is SPATIAL (county record
centroid inside our VGIN polygon) so parcel-id format drift can't
break it. Discovery recipe: AGOL search `"{county} virginia parcels
type:Feature Service"`, then county-hosted `gis.*` guesses, then
WebSearch; most counties hide behind commercial viewers
(actDataScout etc.).

**Configured so far:** Spotsylvania (self-hosted ArcGIS, full
assessment + SALEPRICE/TRANSFERDATE; 68,284 parcels) and Page (AGOL,
values + sale price/date + ArmsLength; 24,535). VA counties DO
publish sale prices where WV doesn't. ~96% of those counties'
candidates now carry owner/address; ~48% have a real transaction
record.

**VGIN re-keys parcel ids on some county refreshes** — the July
refresh gave Spotsylvania an entirely new id set, and the upsert-only
loader kept both vintages: 82,996 stale VA rows, ~24K phantom
duplicate candidates (the "385K candidates" number was inflated;
truth: 361,380). Loaders now purge rows not re-seen in a successful
county load (DELETE ... ingested_at < run_start). If a county's
candidate count ever jumps ~2x after a reload, suspect this first.

## 2026-07 — Multi-state expansion (MD / PA / DE; NJ ruled out)

**Scope revised by user (supersedes the original WV+VA-only call):**
MD, PA, DE, NJ requested. The 180-min isochrone itself ruled NJ out —
zero NJ counties within drive time (Salem, the closest, misses).
Result: +19 MD, +7 PA, +3 DE jurisdictions; 88 total, 63
cabin-relevant (urban exclusions: Baltimore city, Montgomery, Prince
George's, Anne Arundel, Howard MD; Delaware Co PA).

**Parcel sources:** MD iMap ParcelBoundaries is the best statewide
layer in the project — geometry + address + SDAT values + YEARBLT +
land use + TRADATE/CONSIDR1 (sale date/price) in one layer; owner
NAMES are stripped from MD's public data (SDATWEBADR link per parcel
instead). One load fills parcels AND parcel_assessments. DE FirstMap
DE_StateParcels is geometry+acres only (VGIN-style; county
augmentation later). **PA has no statewide parcels and several
relevant counties sell their data** — per-county discovery like VA
(Adams, Cumberland, Franklin, Fulton, Lancaster, York pending; treat
partial PA coverage as expected).

**Cascade checklist for new counties** (run after parcel loads):
refresh-candidates → parcel_cells → soils (survey-area overrides TBD
for MD/PA/DE) → roads → flood → PAD-US → slope (AOI grew — new tiles)
→ structures (some MD already loaded as border counties) → POIs/places
(PA/DE/NJ added) → metrics all → neighbors KNN last (longest).

## 2026-07 — USA Structures BUILD_ID is per-state, not national

The global UNIQUE(build_id) + ON CONFLICT DO NOTHING silently dropped
~940K structures across 42 counties on cross-state loads (Sussex DE
kept 3% of its rows; even five WV/VA counties from the original loads
were shorted — the WV "border building" diagnosis was wrong, this was
the real cause). Symptom that exposed it: 65% of DE candidates showed
as "secluded", vs 1.3% baseline. Migration 017 re-keys on
(county_fips, build_id); all shorted counties reloaded (4.64M total);
neighbors + has_structure recomputed (9.4 h). Corrected DE seclusion:
343 parcels ≥500 m, not 28,652. Lesson: when a joined metric looks
too good, audit the join keys — layer_loads.feature_count vs stored
row count is the tripwire.

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
