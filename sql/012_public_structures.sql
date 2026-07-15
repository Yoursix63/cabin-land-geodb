-- Public-land adjacency (PAD-US 4.1) + known-structure flag
-- (FEMA/ORNL USA Structures), plus weight-reset support.

-- ---------------------------------------------------------------------------
-- PAD-US fee-owned public lands (national forest, WMA, state forest...).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public_lands (
    id          BIGSERIAL PRIMARY KEY,
    padus_oid   BIGINT NOT NULL UNIQUE,   -- PAD-US OBJECTID (dedupe key)
    unit_nm     TEXT,
    mang_name   TEXT,                     -- managing agency (USFS, state DNR...)
    own_type    TEXT,                     -- FED | STAT | LOC | ...
    des_tp      TEXT,                     -- designation type
    pub_access  TEXT,                     -- Open | Restricted | Closed
    geom        GEOMETRY(MULTIPOLYGON, 4326) NOT NULL
);

CREATE INDEX IF NOT EXISTS public_lands_geom_idx
    ON public_lands USING GIST (geom);

-- ---------------------------------------------------------------------------
-- Structure centroids (USA Structures; imagery-derived footprints).
-- "Known structure", not "permitted" — permits are county records we
-- don't have. OCC_CLS/PRIM_OCC give occupancy class where known.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS structures (
    id          BIGSERIAL PRIMARY KEY,
    build_id    TEXT NOT NULL UNIQUE,
    county_fips CHAR(5) NOT NULL,
    occ_cls     TEXT,
    prim_occ    TEXT,
    sqfeet      NUMERIC(10,1),
    geom        GEOMETRY(POINT, 4326) NOT NULL
);

CREATE INDEX IF NOT EXISTS structures_geom_idx
    ON structures USING GIST (geom);
CREATE INDEX IF NOT EXISTS structures_county_idx
    ON structures (county_fips);

-- ---------------------------------------------------------------------------
-- parcel_metrics additions
-- ---------------------------------------------------------------------------
ALTER TABLE parcel_metrics
    ADD COLUMN IF NOT EXISTS public_land_dist_m   NUMERIC(8,1),  -- NULL after compute = >5km
    ADD COLUMN IF NOT EXISTS public_land_name     TEXT,
    ADD COLUMN IF NOT EXISTS public_computed_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS has_structure        BOOLEAN,
    ADD COLUMN IF NOT EXISTS structure_count      INTEGER,
    ADD COLUMN IF NOT EXISTS structure_sqft       NUMERIC(10,1),
    ADD COLUMN IF NOT EXISTS structure_computed_at TIMESTAMPTZ;

-- ---------------------------------------------------------------------------
-- Weights: remember defaults so the UI can reset; add the new component.
-- ---------------------------------------------------------------------------
ALTER TABLE scoring_weights
    ADD COLUMN IF NOT EXISTS default_weight NUMERIC(5,2);

UPDATE scoring_weights SET default_weight = weight WHERE default_weight IS NULL;

INSERT INTO scoring_weights (component, weight, default_weight, rationale) VALUES
    ('public', 10, 10,
     'Adjacency to public land (PAD-US fee): borrowed backyard, protected viewshed')
ON CONFLICT (component) DO NOTHING;

-- ---------------------------------------------------------------------------
-- parcel_scores view, now with the public-land component.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS parcel_scores;

CREATE VIEW parcel_scores AS
WITH w AS (
    SELECT
        MAX(weight) FILTER (WHERE component = 'flood')     AS w_flood,
        MAX(weight) FILTER (WHERE component = 'slope')     AS w_slope,
        MAX(weight) FILTER (WHERE component = 'septic')    AS w_septic,
        MAX(weight) FILTER (WHERE component = 'size')      AS w_size,
        MAX(weight) FILTER (WHERE component = 'drive')     AS w_drive,
        MAX(weight) FILTER (WHERE component = 'seclusion') AS w_seclusion,
        MAX(weight) FILTER (WHERE component = 'public')    AS w_public,
        SUM(weight)                                        AS w_total
    FROM scoring_weights
),
c AS (
    SELECT
        cp.id, cp.county_fips, cp.county_name, cp.state_abbr,
        cp.parcel_local_id, cp.acres, cp.drive_minutes,
        cp.owner_name, cp.situs_address,
        pm.sfha_pct, pm.slope_mean, pm.slope_p90, pm.pct_steep,
        pm.pct_septic_ok, pm.septic_dominant,
        pm.road_dist_m, pm.road_mtfcc,
        pm.public_land_dist_m, pm.public_land_name,
        pm.has_structure, pm.structure_count, pm.structure_sqft,
        GREATEST(0, 100 - pm.sfha_pct * (100.0 / 30))          AS flood_score,
        CASE WHEN pm.slope_mean IS NULL THEN NULL
             ELSE GREATEST(0, LEAST(100,
                  (20 - pm.slope_mean) * (100.0 / 17))) END    AS slope_score,
        pm.pct_septic_ok                                        AS septic_score,
        CASE
            WHEN cp.acres < 10   THEN 30 + (cp.acres - 2) * (70.0 / 8)
            WHEN cp.acres <= 60  THEN 100
            WHEN cp.acres <= 300 THEN 100 - (cp.acres - 60) * (50.0 / 240)
            ELSE GREATEST(30, 50 - (cp.acres - 300) * (20.0 / 700))
        END                                                     AS size_score,
        GREATEST(0, LEAST(100,
            (180 - cp.drive_minutes) * (100.0 / 105)))          AS drive_score,
        CASE
            WHEN pm.road_dist_m IS NULL  THEN NULL
            WHEN pm.road_dist_m < 250    THEN 60 + pm.road_dist_m * (40.0 / 250)
            WHEN pm.road_dist_m <= 800   THEN 100
            WHEN pm.road_dist_m <= 2500  THEN 100 - (pm.road_dist_m - 800) * (60.0 / 1700)
            ELSE 40
        END                                                     AS seclusion_score,
        -- public land: 100 when adjacent (<=100 m), tapering to 30 at 5 km.
        -- dist NULL after compute means ">5 km"; not computed stays NULL.
        CASE
            WHEN pm.public_computed_at IS NULL      THEN NULL
            WHEN pm.public_land_dist_m IS NULL      THEN 30
            WHEN pm.public_land_dist_m <= 100       THEN 100
            ELSE 100 - (pm.public_land_dist_m - 100) * (70.0 / 4900)
        END                                                     AS public_score
    FROM candidate_parcels cp
    JOIN parcel_metrics pm ON pm.parcel_id = cp.id
)
SELECT c.*,
       ROUND((
           w.w_flood     * COALESCE(c.flood_score, 50) +
           w.w_slope     * COALESCE(c.slope_score, 50) +
           w.w_septic    * COALESCE(c.septic_score, 50) +
           w.w_size      * c.size_score +
           w.w_drive     * c.drive_score +
           w.w_seclusion * COALESCE(c.seclusion_score, 50) +
           w.w_public    * COALESCE(c.public_score, 50)
       ) / w.w_total, 1) AS score
FROM c CROSS JOIN w;
