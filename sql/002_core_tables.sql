-- Core reference tables.
-- Run after 001_extensions.sql.

-- ---------------------------------------------------------------------------
-- Project origin (drive-time anchor). Single-row table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS project_origin (
    id              SMALLINT PRIMARY KEY DEFAULT 1,
    name            TEXT NOT NULL,
    geom            GEOMETRY(POINT, 4326) NOT NULL,
    drive_time_max  INTEGER NOT NULL,   -- minutes
    CONSTRAINT project_origin_singleton CHECK (id = 1)
);

-- ---------------------------------------------------------------------------
-- Counties within the drive-time isochrone, restricted to VA + WV.
-- Populated by ingest/counties.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS counties_in_scope (
    county_fips     CHAR(5) PRIMARY KEY,        -- 2-digit state + 3-digit county
    state_fips      CHAR(2) NOT NULL,
    state_abbr      CHAR(2) NOT NULL,           -- 'VA' | 'WV'
    name            TEXT NOT NULL,
    drive_minutes   NUMERIC(6,1),               -- from project_origin centroid-to-centroid
    geom            GEOMETRY(MULTIPOLYGON, 4326) NOT NULL,
    tiger_year      SMALLINT NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS counties_in_scope_geom_idx
    ON counties_in_scope USING GIST (geom);

CREATE INDEX IF NOT EXISTS counties_in_scope_state_idx
    ON counties_in_scope (state_abbr);

-- ---------------------------------------------------------------------------
-- Per-county loader registry. Lets us track which counties have parcels
-- loaded, from what source, and when.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS parcel_source (
    county_fips     CHAR(5) PRIMARY KEY REFERENCES counties_in_scope(county_fips),
    source_kind     TEXT NOT NULL,              -- 'wv_statewide' | 'vgin' | 'county_rest' | 'shapefile'
    source_url      TEXT,
    source_layer    TEXT,
    last_loaded_at  TIMESTAMPTZ,
    parcel_count    INTEGER,
    notes           TEXT
);

-- ---------------------------------------------------------------------------
-- Parcels — normalized cross-county schema. Raw county-specific attributes
-- are preserved in source_attrs (jsonb) so we don't lose information.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS parcels (
    id              BIGSERIAL PRIMARY KEY,
    county_fips     CHAR(5) NOT NULL REFERENCES counties_in_scope(county_fips),
    parcel_local_id TEXT NOT NULL,              -- county's own parcel id (PIN, tax map, etc.)
    acres           NUMERIC(12,3),
    assessed_value  NUMERIC(14,2),
    owner_name      TEXT,
    owner_kind      TEXT,                       -- 'private' | 'state' | 'federal' | 'tribal' | 'unknown'
    situs_address   TEXT,
    zoning_code     TEXT,
    land_use_code   TEXT,
    source_attrs    JSONB NOT NULL DEFAULT '{}'::jsonb,
    geom            GEOMETRY(MULTIPOLYGON, 4326) NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (county_fips, parcel_local_id)
);
