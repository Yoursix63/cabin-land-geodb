-- Phase 4: weighted parcel scoring.
--
-- Each component is normalized to 0-100 inside the parcel_scores view;
-- weights live in scoring_weights so tuning is an UPDATE + requery,
-- never a schema or code change. Components with NULL inputs score a
-- neutral 50 in the weighted total but expose NULL in their component
-- column so gaps stay visible.

CREATE TABLE IF NOT EXISTS scoring_weights (
    component  TEXT PRIMARY KEY,
    weight     NUMERIC(5,2) NOT NULL,
    rationale  TEXT
);

INSERT INTO scoring_weights (component, weight, rationale) VALUES
    ('septic',    25, 'Buildability: conventional drain-field soil is the scarce resource'),
    ('slope',     20, 'Buildability: site prep cost rises fast past ~8 deg'),
    ('flood',     15, 'Hard risk: SFHA share of parcel'),
    ('drive',     15, 'Usability: weekend-trip feasibility from Alexandria'),
    ('size',      15, 'Preference: 10-60 ac sweet spot'),
    ('seclusion', 10, 'Preference: near-but-not-on a public road')
ON CONFLICT (component) DO NOTHING;

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
        -- flood: 100 clean, 0 at >=30% SFHA
        GREATEST(0, 100 - pm.sfha_pct * (100.0 / 30))          AS flood_score,
        -- slope: 100 at <=3 deg mean, 0 at >=20
        CASE WHEN pm.slope_mean IS NULL THEN NULL
             ELSE GREATEST(0, LEAST(100,
                  (20 - pm.slope_mean) * (100.0 / 17))) END    AS slope_score,
        -- septic: share of parcel on workable soil
        pm.pct_septic_ok                                        AS septic_score,
        -- size: ramps 2->10 ac, plateau 10-60, tapers to 30 by 1000
        CASE
            WHEN cp.acres < 10   THEN 30 + (cp.acres - 2) * (70.0 / 8)
            WHEN cp.acres <= 60  THEN 100
            WHEN cp.acres <= 300 THEN 100 - (cp.acres - 60) * (50.0 / 240)
            ELSE GREATEST(30, 50 - (cp.acres - 300) * (20.0 / 700))
        END                                                     AS size_score,
        -- drive: 100 at <=75 min, 0 at 180
        GREATEST(0, LEAST(100,
            (180 - cp.drive_minutes) * (100.0 / 105)))          AS drive_score,
        -- seclusion: 60 on the road, best 250-800 m off it, 40 far out
        CASE
            WHEN pm.road_dist_m IS NULL  THEN NULL
            WHEN pm.road_dist_m < 250    THEN 60 + pm.road_dist_m * (40.0 / 250)
            WHEN pm.road_dist_m <= 800   THEN 100
            WHEN pm.road_dist_m <= 2500  THEN 100 - (pm.road_dist_m - 800) * (60.0 / 1700)
            ELSE 40
        END                                                     AS seclusion_score
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
           w.w_seclusion * COALESCE(c.seclusion_score, 50)
       ) / w.w_total, 1) AS score
FROM c CROSS JOIN w;
