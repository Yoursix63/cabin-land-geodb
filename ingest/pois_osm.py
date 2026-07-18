"""
Convenience POIs from OpenStreetMap via Overpass: groceries, fuel,
hardware, pharmacies/medical. One pull over the AOI bounds plus a
30 km pad (supply stores across the MD line count).

OSM data (c) OpenStreetMap contributors, ODbL.

Run:
    python -m ingest.pois_osm
"""
from __future__ import annotations

from sqlalchemy import text

from .db import get_engine
from .http import make_session
from .staging import bulk_load

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
PAD_DEG = 0.3   # ~30 km

SESSION = make_session()

KIND_RULES = [
    ("grocery",     'nwr["shop"~"^(supermarket|greengrocer)$"]'),
    ("convenience", 'nwr["shop"~"^(convenience|general)$"]'),
    ("fuel",        'nwr["amenity"="fuel"]'),
    ("hardware",    'nwr["shop"~"^(hardware|doityourself|farm)$"]'),
    ("medical",     'nwr["amenity"~"^(pharmacy|hospital|clinic)$"]'),
]

STAGING_COLS = {
    "osm_id": "text",
    "kind":   "text",
    "name":   "text",
    "lon":    "double precision",
    "lat":    "double precision",
}

MERGE_SQL = """
    TRUNCATE pois;
    INSERT INTO pois (osm_id, kind, name, geom)
    SELECT DISTINCT ON (osm_id) osm_id, kind, name,
           ST_SetSRID(ST_MakePoint(lon, lat), 4326)
    FROM _staging;
"""


def aoi_bbox() -> tuple[float, float, float, float]:
    engine = get_engine()
    with engine.connect() as conn:
        s, w, n, e = conn.execute(text("""
            SELECT MIN(ST_YMin(geom)), MIN(ST_XMin(geom)),
                   MAX(ST_YMax(geom)), MAX(ST_XMax(geom))
            FROM counties_in_scope WHERE cabin_relevant
        """)).one()
    return s - PAD_DEG, w - PAD_DEG, n + PAD_DEG, e + PAD_DEG


def main() -> None:
    s, w, n, e = aoi_bbox()
    bbox = f"({s},{w},{n},{e})"
    rows: list[tuple] = []
    for kind, rule in KIND_RULES:
        query = f"[out:json][timeout:300];({rule}{bbox};);out center tags;"
        elements = None
        for mirror in OVERPASS_MIRRORS:
            try:
                r = SESSION.post(mirror, data={"data": query}, timeout=600)
                r.raise_for_status()
                elements = r.json().get("elements", [])
                break
            except Exception as exc:
                print(f"  {mirror.split('/')[2]}: {exc} — trying next mirror")
        if elements is None:
            raise RuntimeError(f"all Overpass mirrors failed for {kind}")
        for el in elements:
            if el["type"] == "node":
                lon, lat = el.get("lon"), el.get("lat")
            else:
                c = el.get("center") or {}
                lon, lat = c.get("lon"), c.get("lat")
            if lon is None:
                continue
            rows.append((f"{el['type']}/{el['id']}", kind,
                         (el.get("tags") or {}).get("name"), lon, lat))
        print(f"  {kind}: {len(elements)} features")

    n_loaded = bulk_load(STAGING_COLS, rows, MERGE_SQL)
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO layer_loads (layer, scope, feature_count)
            VALUES ('pois_osm', 'aoi+30km', :n)
            ON CONFLICT (layer, scope) DO UPDATE SET
                loaded_at = now(), feature_count = EXCLUDED.feature_count
        """), {"n": n_loaded})
    print(f"Done. {n_loaded:,} POIs loaded.")


if __name__ == "__main__":
    main()
