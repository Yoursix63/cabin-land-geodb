-- Extensions required by the cabin_land geodatabase (Phase 0).
-- Run as a superuser on the cabin_land database.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- fuzzy text search on owner/address
CREATE EXTENSION IF NOT EXISTS btree_gist;  -- composite geom + scalar indexes

-- Additional extensions added in later phases:
--   postgis_topology  (only if topology features used)
--   pgrouting         (drive-time isochrone refinement)
--   h3 / h3_postgis   (hex indexing — requires h3-pg installed separately)
