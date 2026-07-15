"""
Known structures per parcel, from FEMA/ORNL USA Structures (imagery-
derived building footprints). Fetched as centroids (returnCentroid,
no polygon payload), filtered server-side by county FIPS.

"Known structure" — NOT a permit record; permits are county systems we
don't have access to.

Usage:
    python -m ingest.structures            # all cabin-relevant counties
    python -m ingest.structures 54031      # one county
"""
from __future__ import annotations

import sys
import time

from sqlalchemy import text

from .db import get_engine
from .http import make_session
from .staging import bulk_load

URL = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
       "USA_Structures_View/FeatureServer/0/query")
PAGE_SIZE = 2000

SESSION = make_session()

STAGING_COLS = {
    "build_id":    "text",
    "county_fips": "text",
    "occ_cls":     "text",
    "prim_occ":    "text",
    "sqfeet":      "numeric",
    "lon":         "double precision",
    "lat":         "double precision",
}

# Border buildings can be reported under both adjacent counties' FIPS;
# first county in wins (DO NOTHING on the build_id collision).
MERGE_SQL_TEMPLATE = """
    DELETE FROM structures WHERE county_fips = '{fips}';
    INSERT INTO structures (build_id, county_fips, occ_cls, prim_occ,
                            sqfeet, geom)
    SELECT DISTINCT ON (build_id)
        build_id, county_fips, occ_cls, prim_occ, sqfeet,
        ST_SetSRID(ST_MakePoint(lon, lat), 4326)
    FROM _staging
    ON CONFLICT (build_id) DO NOTHING;
"""

METRICS_SQL = """
    WITH agg AS (
        SELECT cp.id,
               COUNT(s.id)  AS n,
               MAX(s.sqfeet) AS max_sqft
        FROM candidate_parcels cp
        LEFT JOIN structures s ON ST_Contains(cp.geom, s.geom)
        GROUP BY cp.id
    )
    INSERT INTO parcel_metrics (parcel_id, has_structure, structure_count,
                                structure_sqft, structure_computed_at)
    SELECT id, n > 0, n, max_sqft, now()
    FROM agg
    ON CONFLICT (parcel_id) DO UPDATE SET
        has_structure         = EXCLUDED.has_structure,
        structure_count       = EXCLUDED.structure_count,
        structure_sqft        = EXCLUDED.structure_sqft,
        structure_computed_at = EXCLUDED.structure_computed_at;
"""


def fetch_county(fips: str):
    offset = 0
    while True:
        r = SESSION.get(URL, params={
            "where": f"FIPS = '{fips}'",
            "outFields": "BUILD_ID,OCC_CLS,PRIM_OCC,SQFEET",
            "returnGeometry": "false",
            "returnCentroid": "true",
            "outSR": "4326",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
        }, timeout=300)
        r.raise_for_status()
        data = r.json()
        page = data.get("features", [])
        yield from page
        if len(page) < PAGE_SIZE:
            return
        offset += PAGE_SIZE
        time.sleep(0.05)


def load_county(fips: str, name: str) -> int:
    rows = []
    for f in fetch_county(fips):
        a = f.get("attributes") or {}
        cen = f.get("centroid") or {}
        bid = a.get("BUILD_ID")
        if bid is None or "x" not in cen:
            continue
        rows.append((str(bid), fips, a.get("OCC_CLS"), a.get("PRIM_OCC"),
                     a.get("SQFEET"), cen["x"], cen["y"]))
    n = bulk_load(STAGING_COLS, rows, MERGE_SQL_TEMPLATE.format(fips=fips))
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO layer_loads (layer, scope, feature_count)
            VALUES ('structures_usa', :scope, :n)
            ON CONFLICT (layer, scope) DO UPDATE SET
                loaded_at = now(), feature_count = EXCLUDED.feature_count
        """), {"scope": fips, "n": n})
    return n


def main() -> None:
    explicit = [a for a in sys.argv[1:] if a[:2] in ("51", "54")]
    engine = get_engine()
    with engine.connect() as conn:
        where = "cabin_relevant"
        params = {}
        if explicit:
            where += " AND county_fips = ANY(:f)"
            params["f"] = explicit
        targets = conn.execute(text(
            f"SELECT county_fips, name FROM counties_in_scope WHERE {where} "
            f"ORDER BY county_fips"), params).all()

    print(f"Fetching USA Structures for {len(targets)} counties")
    total = 0
    failures = []
    t0 = time.time()
    for fips, name in targets:
        try:
            n = load_county(fips, name)
            total += n
            print(f"[{fips}] {name}: {n:,} structures ({time.time()-t0:.0f}s)")
        except Exception as exc:
            failures.append(fips)
            print(f"[{fips}] {name} FAILED: {exc}")

    print(f"\nDone. {total:,} structures loaded.")
    if failures:
        print(f"Failed: {', '.join(failures)} — rerun to fill.")
    print("Now run: python manage.py metrics structures")


if __name__ == "__main__":
    main()
