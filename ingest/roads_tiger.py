"""
Load TIGER/Line roads for cabin-relevant counties and compute
nearest-public-road distance per candidate parcel.

Source: https://www2.census.gov/geo/tiger/TIGER2024/ROADS/
        tl_2024_{fips}_roads.zip   (per county, ~1-10 MB)

Zips cached in data/raw/tiger_roads/.

Usage:
    python -m ingest.roads_tiger            # all cabin-relevant counties
    python -m ingest.roads_tiger 54031      # one county
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pyogrio
from shapely import to_wkb
from sqlalchemy import text

from .db import get_engine
from .http import make_session
from .staging import bulk_load

TIGER_YEAR = 2024
URL = ("https://www2.census.gov/geo/tiger/TIGER{year}/ROADS/"
       "tl_{year}_{fips}_roads.zip")

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "raw" / "tiger_roads"

SESSION = make_session()

# Public-ish roads used for the access metric. S1500 (4WD trail) and
# S1740 (private) are loaded but excluded from nearest-road distance.
PUBLIC_MTFCC = ("S1100", "S1200", "S1400")
KEEP_MTFCC = PUBLIC_MTFCC + ("S1500", "S1740")

STAGING_COLS = {
    "county_fips": "text",
    "mtfcc":       "text",
    "fullname":    "text",
    "geom_wkb":    "bytea",
}

MERGE_SQL_TEMPLATE = """
    DELETE FROM roads WHERE county_fips = '{fips}';
    INSERT INTO roads (county_fips, mtfcc, fullname, geom)
    SELECT county_fips, mtfcc, fullname,
           ST_Multi(ST_SetSRID(ST_GeomFromWKB(geom_wkb), 4326))
    FROM _staging;
"""

# Nearest public road per candidate parcel. KNN (<->) orders in degrees,
# which is anisotropic at ~39N, so take the 5 KNN candidates and pick
# the true geographic minimum.
METRICS_SQL = """
    INSERT INTO parcel_metrics (parcel_id, road_dist_m, road_mtfcc,
                                road_computed_at)
    SELECT cp.id, n.dist_m, n.mtfcc, now()
    FROM candidate_parcels cp
    CROSS JOIN LATERAL (
        SELECT k.mtfcc,
               ROUND(ST_Distance(cp.geom::geography, k.geom::geography)::numeric, 1)
                   AS dist_m
        FROM (
            SELECT r.mtfcc, r.geom
            FROM roads r
            WHERE r.mtfcc IN ('S1100', 'S1200', 'S1400')
            ORDER BY cp.geom <-> r.geom
            LIMIT 5
        ) k
        ORDER BY ST_Distance(cp.geom::geography, k.geom::geography)
        LIMIT 1
    ) n
    ON CONFLICT (parcel_id) DO UPDATE SET
        road_dist_m      = EXCLUDED.road_dist_m,
        road_mtfcc       = EXCLUDED.road_mtfcc,
        road_computed_at = EXCLUDED.road_computed_at;
"""


def get_targets(county_fips: list[str]) -> list[tuple[str, str]]:
    where = "cabin_relevant"
    params = {}
    if county_fips:
        where += " AND county_fips = ANY(:fips)"
        params["fips"] = county_fips
    engine = get_engine()
    with engine.connect() as conn:
        return [(r[0], r[1]) for r in conn.execute(text(
            f"SELECT county_fips, name FROM counties_in_scope "
            f"WHERE {where} ORDER BY county_fips"), params).all()]


def fetch_zip(fips: str) -> Path:
    path = CACHE / f"tl_{TIGER_YEAR}_{fips}_roads.zip"
    if path.exists() and path.stat().st_size > 0:
        return path
    r = SESSION.get(URL.format(year=TIGER_YEAR, fips=fips), timeout=300)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def load_county(fips: str, name: str) -> int:
    zpath = fetch_zip(fips)
    gdf = pyogrio.read_dataframe(
        f"/vsizip/{zpath.as_posix()}/tl_{TIGER_YEAR}_{fips}_roads.shp")
    gdf = gdf[gdf["MTFCC"].isin(KEEP_MTFCC)]
    rows = [
        (fips, mtfcc, fullname, to_wkb(geom))
        for mtfcc, fullname, geom
        in zip(gdf["MTFCC"], gdf["FULLNAME"], gdf.geometry)
    ]
    n = bulk_load(STAGING_COLS, rows, MERGE_SQL_TEMPLATE.format(fips=fips))
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO layer_loads (layer, scope, feature_count, notes)
            VALUES ('roads_tiger', :scope, :n, :notes)
            ON CONFLICT (layer, scope) DO UPDATE SET
                loaded_at = now(),
                feature_count = EXCLUDED.feature_count,
                notes = EXCLUDED.notes
        """), {"scope": fips, "n": n, "notes": f"TIGER {TIGER_YEAR}"})
    return n


def main() -> None:
    county_fips = [a for a in sys.argv[1:] if a[:2] in ("51", "54")]
    CACHE.mkdir(parents=True, exist_ok=True)
    targets = get_targets(county_fips)
    print(f"Loading TIGER {TIGER_YEAR} roads for {len(targets)} counties")

    total = 0
    failures = []
    t0 = time.time()
    for fips, name in targets:
        try:
            n = load_county(fips, name)
            total += n
            print(f"[{fips}] {name}: {n:,} segments ({time.time()-t0:.0f}s)")
        except Exception as exc:
            failures.append(fips)
            print(f"[{fips}] {name} FAILED: {exc}")

    print(f"\nDone. {total:,} road segments loaded.")
    if failures:
        print(f"Failed: {', '.join(failures)}")
    print("Now run: python manage.py metrics roads")


if __name__ == "__main__":
    main()
