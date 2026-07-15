"""
PAD-US 4.1 fee-owned public lands for the AOI, plus per-parcel
distance-to-public-land metrics.

Source: USGS PAD-US Manager Name layer (FeatureServer), FeatClass='Fee'.
George Washington NF, state forests, and WMAs are the big features in
our AOI.

Run:
    python -m ingest.public_lands
"""
from __future__ import annotations

import json
import time

from sqlalchemy import text

from .db import get_engine
from .http import make_session
from .staging import bulk_load

URL = ("https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/"
       "Manager_Name_PADUS/FeatureServer/0/query")
PAGE_SIZE = 1000     # polygons are vertex-heavy; keep pages modest
WHERE = "FeatClass = 'Fee'"

SESSION = make_session()

STAGING_COLS = {
    "padus_oid":  "bigint",
    "unit_nm":    "text",
    "mang_name":  "text",
    "own_type":   "text",
    "des_tp":     "text",
    "pub_access": "text",
    "geom_json":  "text",
}

MERGE_SQL = """
    TRUNCATE public_lands;
    INSERT INTO public_lands (padus_oid, unit_nm, mang_name, own_type,
                              des_tp, pub_access, geom)
    SELECT DISTINCT ON (s.padus_oid)
        s.padus_oid, s.unit_nm, s.mang_name, s.own_type,
        s.des_tp, s.pub_access,
        ST_CollectionExtract(ST_MakeValid(
            ST_SetSRID(ST_GeomFromGeoJSON(s.geom_json), 4326)), 3)
    FROM _staging s
    WHERE NOT ST_IsEmpty(ST_CollectionExtract(ST_MakeValid(
        ST_SetSRID(ST_GeomFromGeoJSON(s.geom_json), 4326)), 3));

    -- Rebuild the vertex-capped copy used by distance queries
    -- (see sql/013_public_subdiv.sql).
    TRUNCATE public_lands_subdiv;
    INSERT INTO public_lands_subdiv (unit_nm, geom)
    SELECT pl.unit_nm, ST_Subdivide(pl.geom, 128) FROM public_lands pl;
    ANALYZE public_lands_subdiv;
"""

# Distance to nearest public land within 5 km; NULL dist (with the
# timestamp set) means "computed, nothing within 5 km". Runs against
# the ST_Subdivide'd copy — raw national-forest multipolygons made
# this query crawl (O(vertices) per distance call).
METRICS_SQL = """
    INSERT INTO parcel_metrics (parcel_id, public_land_dist_m,
                                public_land_name, public_computed_at)
    SELECT cp.id, n.dist_m, n.unit_nm, now()
    FROM candidate_parcels cp
    LEFT JOIN LATERAL (
        SELECT k.unit_nm,
               ROUND(ST_Distance(cp.geom::geography, k.geom::geography)::numeric, 1)
                   AS dist_m
        FROM (
            SELECT pl.unit_nm, pl.geom
            FROM public_lands_subdiv pl
            ORDER BY cp.geom <-> pl.geom
            LIMIT 5
        ) k
        WHERE ST_DWithin(cp.geom::geography, k.geom::geography, 5000)
        ORDER BY ST_Distance(cp.geom::geography, k.geom::geography)
        LIMIT 1
    ) n ON true
    ON CONFLICT (parcel_id) DO UPDATE SET
        public_land_dist_m = EXCLUDED.public_land_dist_m,
        public_land_name   = EXCLUDED.public_land_name,
        public_computed_at = EXCLUDED.public_computed_at;
"""


def fetch_bbox(bbox: str) -> list[dict]:
    features: list[dict] = []
    offset = 0
    while True:
        r = SESSION.get(URL, params={
            "where": WHERE,
            "geometry": bbox,
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "OBJECTID,Unit_Nm,Mang_Name,Own_Type,Des_Tp,Pub_Access",
            "outSR": "4326",
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
        }, timeout=300)
        r.raise_for_status()
        page = r.json().get("features", [])
        features.extend(page)
        if len(page) < PAGE_SIZE:
            return features
        offset += PAGE_SIZE
        time.sleep(0.05)


def main() -> None:
    engine = get_engine()
    with engine.connect() as conn:
        counties = conn.execute(text("""
            SELECT county_fips, name,
                   ST_XMin(geom)||','||ST_YMin(geom)||','||
                   ST_XMax(geom)||','||ST_YMax(geom)
            FROM counties_in_scope WHERE cabin_relevant
            ORDER BY state_abbr, name
        """)).all()

    print(f"Fetching PAD-US fee lands for {len(counties)} county bboxes")
    by_oid: dict[int, tuple] = {}
    for fips, name, bbox in counties:
        feats = fetch_bbox(bbox)
        for f in feats:
            p = f.get("properties") or {}
            oid = p.get("OBJECTID")
            if oid is None or f.get("geometry") is None:
                continue
            by_oid[oid] = (
                oid, p.get("Unit_Nm"), p.get("Mang_Name"), p.get("Own_Type"),
                p.get("Des_Tp"), p.get("Pub_Access"),
                json.dumps(f["geometry"]),
            )
        print(f"  [{fips}] {name}: {len(feats)} features "
              f"({len(by_oid)} unique)")

    print(f"\nLoading {len(by_oid):,} public-land polygons ...")
    n = bulk_load(STAGING_COLS, by_oid.values(), MERGE_SQL)
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO layer_loads (layer, scope, feature_count)
            VALUES ('public_lands_padus', 'aoi', :n)
            ON CONFLICT (layer, scope) DO UPDATE SET
                loaded_at = now(), feature_count = EXCLUDED.feature_count
        """), {"n": n})
    print(f"Done ({n:,} rows). Now run: python manage.py metrics public")


if __name__ == "__main__":
    main()
