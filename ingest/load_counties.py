"""
Load data/processed/counties_in_scope.geojson into the counties_in_scope
table. Idempotent — re-running upserts on county_fips.

Prereq: 001_extensions.sql and 002_core_tables.sql have been applied.

Run:
    python -m ingest.load_counties
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text

from .db import get_engine

ROOT = Path(__file__).resolve().parents[1]
GEOJSON_PATH = ROOT / "data" / "processed" / "counties_in_scope.geojson"

UPSERT_SQL = text("""
    INSERT INTO counties_in_scope (
        county_fips, state_fips, state_abbr, name,
        drive_minutes, geom, tiger_year
    )
    VALUES (
        :county_fips, :state_fips, :state_abbr, :name,
        :drive_minutes,
        ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geom_json), 4326)),
        :tiger_year
    )
    ON CONFLICT (county_fips) DO UPDATE SET
        name           = EXCLUDED.name,
        drive_minutes  = EXCLUDED.drive_minutes,
        geom           = EXCLUDED.geom,
        tiger_year     = EXCLUDED.tiger_year,
        ingested_at    = now()
""")


def main() -> None:
    fc = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
    rows = []
    for feat in fc["features"]:
        p = feat["properties"]
        rows.append({
            "county_fips":   p["GEOID"],
            "state_fips":    p["STATE"],
            "state_abbr":    p["state_abbr"],
            "name":          p["NAME"],
            "drive_minutes": p["drive_minutes"],
            "tiger_year":    p["tiger_year"],
            "geom_json":     json.dumps(feat["geometry"]),
        })

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(UPSERT_SQL, rows)
    print(f"Loaded/updated {len(rows)} rows into counties_in_scope")


if __name__ == "__main__":
    main()
