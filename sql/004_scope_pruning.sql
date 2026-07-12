-- Scope pruning: mark jurisdictions irrelevant to cabin hunting and
-- materialize the candidate parcel set so downstream suitability
-- scoring never touches urban lots.
--
-- Judgment calls, deliberately reversible:
--   UPDATE counties_in_scope SET cabin_relevant = true WHERE ...;
--   REFRESH MATERIALIZED VIEW CONCURRENTLY candidate_parcels;

ALTER TABLE counties_in_scope
    ADD COLUMN IF NOT EXISTS cabin_relevant BOOLEAN NOT NULL DEFAULT true;

-- VA independent cities carry FIPS last-3 >= 510. No cabin land there.
UPDATE counties_in_scope
SET cabin_relevant = false
WHERE state_abbr = 'VA'
  AND substring(county_fips FROM 3)::int >= 510;

-- Urban-core counties: fully suburbanized, no plausible cabin parcels.
-- (Loudoun and Stafford deliberately kept relevant — their western /
-- southern portions still hold acreage.)
UPDATE counties_in_scope
SET cabin_relevant = false
WHERE county_fips IN (
    '51013',  -- Arlington
    '51059',  -- Fairfax County
    '51153',  -- Prince William
    '51087',  -- Henrico
    '51041'   -- Chesterfield
);

-- ---------------------------------------------------------------------------
-- Candidate parcels: the scoring universe for Phases 3-4.
-- 2-1000 acres in cabin-relevant jurisdictions. The acre band and the
-- county flag together cut ~2.1M parcels to a few hundred thousand.
-- ---------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS candidate_parcels;

CREATE MATERIALIZED VIEW candidate_parcels AS
SELECT
    p.id,
    p.county_fips,
    c.name          AS county_name,
    c.state_abbr,
    c.drive_minutes,
    p.parcel_local_id,
    p.acres,
    p.owner_name,
    p.situs_address,
    p.geom
FROM parcels p
JOIN counties_in_scope c USING (county_fips)
WHERE c.cabin_relevant
  AND p.acres BETWEEN 2 AND 1000
WITH DATA;

-- Unique index required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX candidate_parcels_id_idx  ON candidate_parcels (id);
CREATE INDEX candidate_parcels_geom_idx       ON candidate_parcels USING GIST (geom);
CREATE INDEX candidate_parcels_acres_idx      ON candidate_parcels (acres);
CREATE INDEX candidate_parcels_county_idx     ON candidate_parcels (county_fips);
