-- WV assessment attributes (WVGISTC annual tax product: appraised
-- values, year built, land use, deed refs). No sale prices — WV's
-- public extract carries deed book/page only; consideration amounts
-- stay in courthouse records.

CREATE TABLE IF NOT EXISTS parcel_assessments (
    parcel_id           BIGINT PRIMARY KEY REFERENCES parcels(id) ON DELETE CASCADE,
    tax_year            SMALLINT,
    appraised_total     NUMERIC(14,2),
    appraised_land      NUMERIC(14,2),
    appraised_building  NUMERIC(14,2),
    year_built          SMALLINT,
    land_use            TEXT,
    tax_class           TEXT,
    deed_book           TEXT,
    deed_page           TEXT,
    new_book            TEXT,      -- most recent transfer reference
    new_page            TEXT,
    cards               SMALLINT,  -- assessment cards aggregated
    source              TEXT NOT NULL,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- parcel_scores: expose assessment context (not scored — value is what
-- you pay, not what the land is like; use as filter/ranking context).
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
        END                                                     AS public_score
    FROM candidate_parcels cp
    JOIN parcel_metrics pm ON pm.parcel_id = cp.id
    LEFT JOIN parcel_assessments pa ON pa.parcel_id = cp.id
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
