-- Slope fabric: per-H3-cell slope statistics derived from USGS 3DEP
-- (10 m, served as slope-degrees by the 3DEP ImageServer), plus the
-- per-parcel slope columns on parcel_metrics.
--
-- Cells are H3 resolution 10 (~1.5 ha, ~150 slope pixels per cell).
-- Populated by ingest/slope_3dep.py; full-replace on each run.

CREATE TABLE IF NOT EXISTS hex_slope (
    h3           h3index PRIMARY KEY,
    px_count     INTEGER NOT NULL,        -- valid 10 m pixels aggregated
    slope_mean   NUMERIC(5,2) NOT NULL,   -- degrees
    slope_p90    NUMERIC(5,2) NOT NULL,   -- degrees, 90th percentile
    pct_gt15     NUMERIC(5,2) NOT NULL,   -- % of pixels steeper than 15 deg
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE parcel_metrics
    ADD COLUMN IF NOT EXISTS slope_mean        NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS slope_p90         NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS pct_steep         NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS slope_computed_at TIMESTAMPTZ;
