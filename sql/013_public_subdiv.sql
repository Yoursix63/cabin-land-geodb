-- Subdivided public lands for fast distance queries. PAD-US national
-- forest multipolygons carry 100K+ vertices; ST_Distance against them
-- is O(vertices) per call, which made the per-parcel metrics query
-- crawl. ST_Subdivide caps vertices per piece; min distance to any
-- piece equals distance to the whole.

CREATE TABLE IF NOT EXISTS public_lands_subdiv (
    id       BIGSERIAL PRIMARY KEY,
    unit_nm  TEXT,
    geom     GEOMETRY(POLYGON, 4326) NOT NULL
);

TRUNCATE public_lands_subdiv;

INSERT INTO public_lands_subdiv (unit_nm, geom)
SELECT pl.unit_nm, ST_Subdivide(pl.geom, 128)
FROM public_lands pl;

CREATE INDEX IF NOT EXISTS public_lands_subdiv_geom_idx
    ON public_lands_subdiv USING GIST (geom);

ANALYZE public_lands_subdiv;
