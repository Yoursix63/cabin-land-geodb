"""
Census places (incorporated places + CDPs) for VA, WV, MD with 2020
Decennial population (POP100), from the TIGERweb Census2020 service.
(The ACS API now requires a key; TIGERweb doesn't.)

Run:
    python -m ingest.places_census
"""
from __future__ import annotations

import json
import time

from sqlalchemy import text

from .db import get_engine
from .http import make_session
from .staging import bulk_load

BASE = ("https://tigerweb.geo.census.gov/arcgis/rest/services/Census2020/"
        "Places_CouSub_ConCity_SubMCD/MapServer")
LAYERS = {4: "incorporated", 5: "cdp"}
STATES = {"51": "VA", "54": "WV", "24": "MD", "42": "PA", "10": "DE",
          "34": "NJ"}   # NJ towns still matter as supply centers near DE
PAGE_SIZE = 1000

SESSION = make_session()

STAGING_COLS = {
    "geoid":     "text",
    "name":      "text",
    "state":     "text",
    "pop":       "integer",
    "geom_json": "text",
}

MERGE_SQL = """
    TRUNCATE places;
    INSERT INTO places (geoid, name, state, pop, geom)
    SELECT DISTINCT ON (geoid) geoid, name, state, pop,
           ST_Multi(ST_MakeValid(
               ST_SetSRID(ST_GeomFromGeoJSON(geom_json), 4326)))
    FROM _staging;
"""


def fetch_layer(layer: int, state_fips: str):
    offset = 0
    while True:
        r = SESSION.get(f"{BASE}/{layer}/query", params={
            "where": f"STATE='{state_fips}'",
            "outFields": "GEOID,NAME,POP100",
            "outSR": "4326",
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
        }, timeout=300)
        r.raise_for_status()
        page = r.json().get("features", [])
        yield from page
        if len(page) < PAGE_SIZE:
            return
        offset += PAGE_SIZE
        time.sleep(0.05)


def main() -> None:
    rows: list[tuple] = []
    for layer, kind in LAYERS.items():
        for st_fips, st_abbr in STATES.items():
            n0 = len(rows)
            for f in fetch_layer(layer, st_fips):
                p = f.get("properties") or {}
                if f.get("geometry") is None or not p.get("GEOID"):
                    continue
                rows.append((p["GEOID"], p.get("NAME"), st_abbr,
                             p.get("POP100"), json.dumps(f["geometry"])))
            print(f"  {kind} {st_abbr}: {len(rows) - n0} places")

    n = bulk_load(STAGING_COLS, rows, MERGE_SQL)
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO layer_loads (layer, scope, feature_count)
            VALUES ('places_census', 'va+wv+md', :n)
            ON CONFLICT (layer, scope) DO UPDATE SET
                loaded_at = now(), feature_count = EXCLUDED.feature_count
        """), {"n": n})
    print(f"Done. {n:,} places loaded.")


if __name__ == "__main__":
    main()
