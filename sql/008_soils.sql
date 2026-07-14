-- SSURGO soils: map-unit polygons with septic-suitability ratings, and
-- per-parcel septic columns on parcel_metrics.
--
-- Ratings come from the NRCS interpretation "ENG - Septic Tank
-- Absorption Fields", dominant component per map unit:
--   'Not limited' | 'Somewhat limited' | 'Very limited' | 'Not rated'

CREATE TABLE IF NOT EXISTS soil_units (
    id             BIGSERIAL PRIMARY KEY,
    mukey          TEXT NOT NULL,
    areasymbol     TEXT NOT NULL,          -- survey area, e.g. VA091
    muname         TEXT,
    septic_rating  TEXT,
    drainage_class TEXT,                   -- muaggatt.drclassdcd
    hydro_group    TEXT,                   -- muaggatt.hydgrpdcd
    geom           GEOMETRY(MULTIPOLYGON, 4326) NOT NULL
);

CREATE INDEX IF NOT EXISTS soil_units_geom_idx
    ON soil_units USING GIST (geom);
CREATE INDEX IF NOT EXISTS soil_units_area_idx
    ON soil_units (areasymbol);

ALTER TABLE parcel_metrics
    ADD COLUMN IF NOT EXISTS pct_septic_ok      NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS septic_dominant    TEXT,
    ADD COLUMN IF NOT EXISTS septic_computed_at TIMESTAMPTZ;
