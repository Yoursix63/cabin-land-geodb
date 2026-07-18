-- Remoteness: neighbor proximity (dwellings from USA Structures) and
-- convenience distance (OSM POIs, Census places). Two new scoring
-- components, both remote-positive per user preference; road-distance
-- 'seclusion' drops to weight 5 (driveway cost, not privacy — that's
-- now the neighbors component's job).

-- Geography index so ST_DWithin(geography) neighbor counts use the index.
CREATE INDEX IF NOT EXISTS structures_geog_idx
    ON structures USING GIST ((geom::geography));

-- ---------------------------------------------------------------------------
-- OSM points of interest (Overpass pull; ODbL — attribution in README).
-- kind: grocery | convenience | fuel | hardware | medical
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pois (
    id        BIGSERIAL PRIMARY KEY,
    osm_id    TEXT NOT NULL UNIQUE,
    kind      TEXT NOT NULL,
    name      TEXT,
    geom      GEOMETRY(POINT, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS pois_geom_idx ON pois USING GIST (geom);
CREATE INDEX IF NOT EXISTS pois_kind_idx ON pois (kind);

-- ---------------------------------------------------------------------------
-- Census places (towns/CDPs) with ACS population. VA + WV + MD (supply
-- towns across the Potomac count).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS places (
    geoid     CHAR(7) PRIMARY KEY,
    name      TEXT NOT NULL,
    state     CHAR(2) NOT NULL,
    pop       INTEGER,
    geom      GEOMETRY(MULTIPOLYGON, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS places_geom_idx ON places USING GIST (geom);

-- ---------------------------------------------------------------------------
-- parcel_metrics additions
-- ---------------------------------------------------------------------------
ALTER TABLE parcel_metrics
    ADD COLUMN IF NOT EXISTS nbr_dist_m      NUMERIC(8,1),
    ADD COLUMN IF NOT EXISTS nbr_cnt_500m    INTEGER,
    ADD COLUMN IF NOT EXISTS nbr_cnt_1km     INTEGER,
    ADD COLUMN IF NOT EXISTS nbr_computed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS grocery_dist_km NUMERIC(6,2),
    ADD COLUMN IF NOT EXISTS grocery_name    TEXT,
    ADD COLUMN IF NOT EXISTS town_dist_km    NUMERIC(6,2),
    ADD COLUMN IF NOT EXISTS town_name       TEXT,
    ADD COLUMN IF NOT EXISTS conv_computed_at TIMESTAMPTZ;

-- ---------------------------------------------------------------------------
-- Weights: two new components; seclusion demoted (privacy is now
-- measured directly by neighbors, road distance stays as access cost).
-- ---------------------------------------------------------------------------
INSERT INTO scoring_weights (component, weight, default_weight, rationale) VALUES
    ('neighbors', 15, 15,
     'Privacy: distance to nearest off-parcel dwelling, penalized by dwelling density within 1 km'),
    ('remoteness', 10, 10,
     'Remote-positive: distance to nearest supermarket and town (pop >= 2500), saturating ~30 km')
ON CONFLICT (component) DO NOTHING;

UPDATE scoring_weights SET weight = 5, default_weight = 5,
    rationale = 'Access cost: distance to public road (driveway construction), privacy now scored by neighbors'
WHERE component = 'seclusion';

-- ---------------------------------------------------------------------------
-- parcel_scores with the two new components.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS parcel_scores;

CREATE VIEW parcel_scores AS
WITH w AS (
    SELECT
        MAX(weight) FILTER (WHERE component = 'flood')      AS w_flood,
        MAX(weight) FILTER (WHERE component = 'slope')      AS w_slope,
        MAX(weight) FILTER (WHERE component = 'septic')     AS w_septic,
        MAX(weight) FILTER (WHERE component = 'size')       AS w_size,
        MAX(weight) FILTER (WHERE component = 'drive')      AS w_drive,
        MAX(weight) FILTER (WHERE component = 'seclusion')  AS w_seclusion,
        MAX(weight) FILTER (WHERE component = 'public')     AS w_public,
        MAX(weight) FILTER (WHERE component = 'neighbors')  AS w_neighbors,
        MAX(weight) FILTER (WHERE component = 'remoteness') AS w_remoteness,
        SUM(weight)                                         AS w_total
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
        pm.nbr_dist_m, pm.nbr_cnt_500m, pm.nbr_cnt_1km,
        pm.grocery_dist_km, pm.grocery_name, pm.town_dist_km, pm.town_name,
        pa.appraised_total, pa.appraised_land, pa.appraised_building,
        pa.year_built, pa.land_use, pa.tax_year,
        CASE WHEN cp.acres > 0 AND pa.appraised_total IS NOT NULL
             THEN ROUND(pa.appraised_total / cp.acres)
        END AS value_per_acre,
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
        CASE
            WHEN pm.public_computed_at IS NULL      THEN NULL
            WHEN pm.public_land_dist_m IS NULL      THEN 30
            WHEN pm.public_land_dist_m <= 100       THEN 100
            ELSE 100 - (pm.public_land_dist_m - 100) * (70.0 / 4900)
        END                                                     AS public_score,
        -- neighbors: 0 at <=75 m to nearest dwelling, 100 at >=800 m,
        -- minus 4 points per dwelling beyond 3 within 1 km.
        CASE
            WHEN pm.nbr_computed_at IS NULL THEN NULL
            WHEN pm.nbr_dist_m IS NULL      THEN 100   -- nothing within KNN reach
            ELSE GREATEST(0,
                 LEAST(100, (pm.nbr_dist_m - 75) * (100.0 / 725))
                 - GREATEST(0, (COALESCE(pm.nbr_cnt_1km, 0) - 3)) * 4)
        END                                                     AS neighbors_score,
        -- remoteness (remote-positive, saturating at 30 km):
        -- 60% grocery distance, 40% town distance.
        CASE
            WHEN pm.conv_computed_at IS NULL THEN NULL
            ELSE ROUND(
                 0.6 * LEAST(COALESCE(pm.grocery_dist_km, 30), 30) * (100.0 / 30)
               + 0.4 * LEAST(COALESCE(pm.town_dist_km, 30), 30) * (100.0 / 30), 1)
        END                                                     AS remoteness_score
    FROM candidate_parcels cp
    JOIN parcel_metrics pm ON pm.parcel_id = cp.id
    LEFT JOIN parcel_assessments pa ON pa.parcel_id = cp.id
)
SELECT c.*,
       ROUND((
           w.w_flood      * COALESCE(c.flood_score, 50) +
           w.w_slope      * COALESCE(c.slope_score, 50) +
           w.w_septic     * COALESCE(c.septic_score, 50) +
           w.w_size       * c.size_score +
           w.w_drive      * c.drive_score +
           w.w_seclusion  * COALESCE(c.seclusion_score, 50) +
           w.w_public     * COALESCE(c.public_score, 50) +
           w.w_neighbors  * COALESCE(c.neighbors_score, 50) +
           w.w_remoteness * COALESCE(c.remoteness_score, 50)
       ) / w.w_total, 1) AS score
FROM c CROSS JOIN w;
