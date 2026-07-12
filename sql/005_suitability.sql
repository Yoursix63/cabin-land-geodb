-- Phase 3 foundations: H3 extensions, suitability layer tables, and the
-- per-parcel metrics table that scoring (Phase 4) will read.

-- H3 hex indexing (bundled with the OSGeo PostGIS installer). The hex
-- fabric is used for raster-derived layers (slope, aspect, landcover);
-- vector layers like flood zones join to parcels directly.
CREATE EXTENSION IF NOT EXISTS h3;
CREATE EXTENSION IF NOT EXISTS h3_postgis CASCADE;

-- ---------------------------------------------------------------------------
-- FEMA NFHL flood hazard zones (SFHA subset), AOI-wide.
-- Source: ESRI Living Atlas mirror of FEMA NFHL.
-- Full-replace on each load; fld_ar_id is FEMA's stable area id.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS flood_zones (
    fld_ar_id    TEXT PRIMARY KEY,
    dfirm_id     TEXT,
    fld_zone     TEXT NOT NULL,          -- A, AE, AO, VE, ...
    zone_subty   TEXT,
    sfha         BOOLEAN NOT NULL,       -- Special Flood Hazard Area
    static_bfe   NUMERIC,                -- base flood elevation, ft (null if n/a)
    source_attrs JSONB NOT NULL DEFAULT '{}'::jsonb,
    geom         GEOMETRY(MULTIPOLYGON, 4326) NOT NULL
);

CREATE INDEX IF NOT EXISTS flood_zones_geom_idx
    ON flood_zones USING GIST (geom);

-- ---------------------------------------------------------------------------
-- Generic load registry for suitability layers (one row per layer+scope).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS layer_loads (
    layer          TEXT NOT NULL,
    scope          TEXT NOT NULL,        -- county fips, or 'aoi'
    loaded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    feature_count  INTEGER,
    notes          TEXT,
    PRIMARY KEY (layer, scope)
);

-- ---------------------------------------------------------------------------
-- Per-parcel suitability metrics, populated layer by layer.
-- Only candidate parcels get rows; columns are nullable so each layer
-- can be computed independently.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS parcel_metrics (
    parcel_id          BIGINT PRIMARY KEY REFERENCES parcels(id) ON DELETE CASCADE,
    -- flood (FEMA NFHL)
    sfha_pct           NUMERIC(5,2),     -- % of parcel area in SFHA
    sfha_zones         TEXT[],           -- distinct zones intersected
    flood_computed_at  TIMESTAMPTZ
    -- slope/aspect/landcover columns arrive with their layers
);
