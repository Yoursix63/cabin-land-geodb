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

- PostgreSQL 16 + PostGIS 3
- Python (geopandas, shapely, rasterio, SQLAlchemy, psycopg)
- Source data from VGIN, WV GIS Tech Center, USGS 3DEP, FEMA NFHL, USDA
  SSURGO, USGS NHD, US Census TIGER, MRLC NLCD

## Layout

```
data/         downloaded + intermediate + processed data (gitignored)
sql/          schema migrations and analysis queries
ingest/       one loader module per data source
scoring/      parcel scoring logic
notebooks/    exploratory analysis
docs/         design notes
```

## Status

Project init. No code yet.
