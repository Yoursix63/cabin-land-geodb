"""
Remoteness metrics for candidate parcels.

Neighbors (slow — run after adjacent-county structures are loaded):
    nbr_dist_m    boundary distance to nearest OFF-parcel dwelling
                  (Residential + Unclassified structures)
    nbr_cnt_500m / nbr_cnt_1km   dwelling counts near the boundary

Convenience (fast):
    grocery_dist_km / grocery_name   nearest OSM supermarket-class POI
    town_dist_km / town_name         nearest Census place, pop >= 2500
                                     (0 km if the parcel is inside one)

Invoked via manage.py: `metrics neighbors`, `metrics convenience`.
"""
from __future__ import annotations

DWELLING_CLASSES = "('Residential', 'Unclassified')"

NEIGHBORS_SQL = f"""
    INSERT INTO parcel_metrics (parcel_id, nbr_dist_m, nbr_cnt_500m,
                                nbr_cnt_1km, nbr_computed_at)
    SELECT cp.id, n.dist_m, COALESCE(c.n500, 0), COALESCE(c.n1km, 0), now()
    FROM candidate_parcels cp
    LEFT JOIN LATERAL (
        SELECT ROUND(ST_Distance(cp.geom::geography, k.geom::geography)::numeric, 1)
               AS dist_m
        FROM (
            SELECT s.geom
            FROM structures s
            WHERE s.occ_cls IN {DWELLING_CLASSES}
              AND NOT ST_Contains(cp.geom, s.geom)
            ORDER BY cp.geom <-> s.geom
            LIMIT 8
        ) k
        ORDER BY ST_Distance(cp.geom::geography, k.geom::geography)
        LIMIT 1
    ) n ON true
    LEFT JOIN LATERAL (
        SELECT COUNT(*) FILTER (
                   WHERE ST_DWithin(cp.geom::geography, s.geom::geography, 500))
                   AS n500,
               COUNT(*) AS n1km
        FROM structures s
        WHERE s.occ_cls IN {DWELLING_CLASSES}
          AND ST_DWithin(cp.geom::geography, s.geom::geography, 1000)
          AND NOT ST_Contains(cp.geom, s.geom)
    ) c ON true
    ON CONFLICT (parcel_id) DO UPDATE SET
        nbr_dist_m      = EXCLUDED.nbr_dist_m,
        nbr_cnt_500m    = EXCLUDED.nbr_cnt_500m,
        nbr_cnt_1km     = EXCLUDED.nbr_cnt_1km,
        nbr_computed_at = EXCLUDED.nbr_computed_at;
"""

CONVENIENCE_SQL = """
    INSERT INTO parcel_metrics (parcel_id, grocery_dist_km, grocery_name,
                                town_dist_km, town_name, conv_computed_at)
    SELECT cp.id, g.km, g.name, t.km, t.name, now()
    FROM candidate_parcels cp
    LEFT JOIN LATERAL (
        SELECT ROUND((ST_Distance(cp.geom::geography, p.geom::geography)
                      / 1000)::numeric, 2) AS km,
               p.name
        FROM pois p
        WHERE p.kind = 'grocery'
        ORDER BY cp.geom <-> p.geom
        LIMIT 1
    ) g ON true
    LEFT JOIN LATERAL (
        SELECT ROUND((ST_Distance(cp.geom::geography, pl.geom::geography)
                      / 1000)::numeric, 2) AS km,
               pl.name
        FROM places pl
        WHERE pl.pop >= 2500
        ORDER BY cp.geom <-> pl.geom
        LIMIT 1
    ) t ON true
    ON CONFLICT (parcel_id) DO UPDATE SET
        grocery_dist_km  = EXCLUDED.grocery_dist_km,
        grocery_name     = EXCLUDED.grocery_name,
        town_dist_km     = EXCLUDED.town_dist_km,
        town_name        = EXCLUDED.town_name,
        conv_computed_at = EXCLUDED.conv_computed_at;
"""
