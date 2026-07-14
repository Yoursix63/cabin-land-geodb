# cabin-land-geodb

PostGIS geodatabase of land parcels and suitability layers for cabin-build
research in rural Virginia and West Virginia, within a ~3-hour drive of
Alexandria, VA.

Personal project. Not affiliated with any organization.

## What it does

Ingests county parcel data and physical-suitability layers (terrain, flood,
soils, hydrology, roads, land cover) into a single PostGIS database, then
scores each parcel against cabin-build criteria so candidate properties can
be ranked. Free FSBO / public listings are overlaid on top where available.

## Scope

- **States**: Virginia and West Virginia only
- **Geographic filter**: counties intersecting a 3-hour drive isochrone from
  Alexandria, VA
- **Out of scope**: Maryland and Pennsylvania counties (even if within drive
  time), commercial MLS scraping (TOS-restricted)

## Stack

- PostgreSQL 17 + PostGIS 3
- Python 3.14 (geopandas, shapely, rasterio, SQLAlchemy, psycopg)
- Source data from VGIN, WV GIS Tech Center, USGS 3DEP, FEMA NFHL, USDA
  SSURGO, USGS NHD, US Census TIGER, MRLC NLCD

## Layout

```
data/         downloaded + intermediate + processed data (gitignored)
sql/          schema migrations and analysis queries
sql/seeds/    small reference data committed alongside the schema
ingest/       one loader module per data source
scoring/      parcel scoring logic
notebooks/    exploratory analysis
docs/         design notes
```

## Setup

### 1. PostgreSQL + PostGIS

Install PostgreSQL 17 via winget:

```powershell
winget install PostgreSQL.PostgreSQL.17
```

The EDB installer will prompt for a superuser password — pick one and remember it.
After install completes, **Stack Builder** launches automatically. Use it to install
the **PostGIS 3 bundle** for PostgreSQL 17.

### 2. Python deps + config

```powershell
copy .env.example .env
# edit .env and set PGPASSWORD
python -m pip install --user -r requirements.txt
python -m pip install --user "psycopg[binary]" SQLAlchemy GeoAlchemy2 click
```

Optionally create `%APPDATA%\postgresql\pgpass.conf` with
`localhost:5432:*:postgres:<password>` so `psql` never prompts.

### 3. Create the database and load

```powershell
& "C:\Program Files\PostgreSQL\17\bin\psql.exe" -U postgres -c "CREATE DATABASE cabin_land;"
python manage.py migrate          # apply sql/NNN_*.sql in order
python manage.py load counties    # county scope (Census + OSRM drive time)
python manage.py load wv          # WV parcels (statewide MapServer)
python manage.py load va          # VA parcels (VGIN, geometry-only)
python manage.py verify           # sanity checks
python manage.py status           # row counts + freshness
```

## CLI

`manage.py` wraps everything: `migrate` (with `--fake`), `status`,
`verify`, `refresh-candidates`, and `load counties|wv|va [FIPS...]`.

## Status

- [x] Phase 0: PostgreSQL 17 + PostGIS 3.6.2, schema, migrations runner
- [x] Phase 1: county scope — 59 jurisdictions (54 VA + 5 WV)
- [x] Phase 2: parcels — 2.09M loaded (WV statewide + VGIN; Rappahannock
      absent from VGIN, needs county-direct source)
- [x] Scope pruning: `candidate_parcels` matview (~346K parcels,
      2–1000 ac in cabin-relevant jurisdictions)
- [ ] Phase 2.5: owner/address augmentation for shortlisted VA counties
- [x] Phase 3a: FEMA flood zones (NFHL via Living Atlas) + per-parcel
      `sfha_pct` metrics — 82% of candidates fully outside SFHA
- [x] Phase 3b: slope fabric — 3.5M H3 res-10 cells from 3DEP 10 m
      slope tiles; 98.7% of candidates have slope_mean/p90/pct_steep
- [ ] Phase 3c: soils (SSURGO), hydro (NHD), landcover (NLCD), road access
- [ ] Phase 4: weighted scoring (flat+dry 5-50 ac screen alone leaves
      ~117K parcels — needs more discriminators)
- [ ] Phase 4: parcel scoring
- [ ] Phase 5: FSBO / public listings overlay

See [docs/DECISIONS.md](docs/DECISIONS.md) for design decisions and
data-source findings.
