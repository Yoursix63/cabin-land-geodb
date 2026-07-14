-- TIGER/Line roads for cabin-relevant counties, and nearest-road
-- distance on parcel_metrics. MTFCC classes of interest:
--   S1100 primary, S1200 secondary, S1400 local/neighborhood,
--   S1500 vehicular trail (4WD), S1740 private road.

CREATE TABLE IF NOT EXISTS roads (
    id          BIGSERIAL PRIMARY KEY,
    county_fips CHAR(5) NOT NULL,
    mtfcc       TEXT NOT NULL,
    fullname    TEXT,
    geom        GEOMETRY(MULTILINESTRING, 4326) NOT NULL
);

CREATE INDEX IF NOT EXISTS roads_geom_idx ON roads USING GIST (geom);
CREATE INDEX IF NOT EXISTS roads_county_idx ON roads (county_fips);

ALTER TABLE parcel_metrics
    ADD COLUMN IF NOT EXISTS road_dist_m       NUMERIC(8,1),
    ADD COLUMN IF NOT EXISTS road_mtfcc        TEXT,
    ADD COLUMN IF NOT EXISTS road_computed_at  TIMESTAMPTZ;
