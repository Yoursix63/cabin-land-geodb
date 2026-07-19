-- USA Structures BUILD_ID is only unique within a state's dataset,
-- not nationally. The global UNIQUE(build_id) made cross-state loads
-- silently drop colliding rows (ON CONFLICT DO NOTHING) — Sussex DE
-- kept 4,859 of 141,203 fetched. Key on (county_fips, build_id).

ALTER TABLE structures DROP CONSTRAINT IF EXISTS structures_build_id_key;
ALTER TABLE structures
    ADD CONSTRAINT structures_county_build_key UNIQUE (county_fips, build_id);
