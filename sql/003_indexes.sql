-- Indexes on parcels and supporting tables.
-- Run after 002_core_tables.sql, but before bulk parcel loads if possible
-- (GIST index builds are faster on empty tables, then incrementally maintained).

CREATE INDEX IF NOT EXISTS parcels_geom_idx
    ON parcels USING GIST (geom);

CREATE INDEX IF NOT EXISTS parcels_county_idx
    ON parcels (county_fips);

CREATE INDEX IF NOT EXISTS parcels_acres_idx
    ON parcels (acres)
    WHERE acres IS NOT NULL;

CREATE INDEX IF NOT EXISTS parcels_owner_kind_idx
    ON parcels (owner_kind)
    WHERE owner_kind IS NOT NULL;

-- Trigram index for fuzzy owner-name lookups.
CREATE INDEX IF NOT EXISTS parcels_owner_name_trgm_idx
    ON parcels USING GIN (owner_name gin_trgm_ops)
    WHERE owner_name IS NOT NULL;
