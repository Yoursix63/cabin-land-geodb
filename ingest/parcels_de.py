"""
Delaware parcels from FirstMap DE_StateParcels (geometry + PIN + acres
only — owner/value need county augmentation later, VGIN-style).

Usage:
    python -m ingest.parcels_de
"""
from __future__ import annotations

import json
import sys
import time

from sqlalchemy import text

from .db import get_engine
from .http import make_session
from .staging import bulk_load

URL = ("https://enterprise.firstmap.delaware.gov/arcgis/rest/services/"
       "PlanningCadastre/DE_StateParcels/FeatureServer/0/query")
PAGE_SIZE = 2000

SESSION = make_session()

COUNTY_FIPS = {"New Castle": "10003", "Kent": "10001", "Sussex": "10005",
               "NEW CASTLE": "10003", "KENT": "10001", "SUSSEX": "10005"}

STAGING_COLS = {
    "county_fips": "text",
    "pin":         "text",
    "acres":       "numeric",
    "geom_json":   "text",
}

MERGE_SQL = """
    INSERT INTO parcels (county_fips, parcel_local_id, acres,
                         source_attrs, geom)
    SELECT DISTINCT ON (s.county_fips, s.pin)
           s.county_fips, s.pin, s.acres, '{"src":"de_firstmap"}'::jsonb,
           ST_CollectionExtract(ST_MakeValid(
               ST_SetSRID(ST_GeomFromGeoJSON(s.geom_json), 4326)), 3)
    FROM _staging s
    WHERE NOT ST_IsEmpty(ST_CollectionExtract(ST_MakeValid(
        ST_SetSRID(ST_GeomFromGeoJSON(s.geom_json), 4326)), 3))
    ON CONFLICT (county_fips, parcel_local_id) DO UPDATE SET
        acres       = EXCLUDED.acres,
        geom        = EXCLUDED.geom,
        ingested_at = now();
"""


def fetch_all():
    offset = 0
    while True:
        r = SESSION.get(URL, params={
            "where": "1=1",
            "outFields": "PIN,ACRES,COUNTY",
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
    engine = get_engine()
    with engine.begin() as conn:
        run_started, = conn.execute(text("SELECT now()")).one()
    rows = []
    for f in fetch_all():
        a = f.get("properties") or {}
        fips = COUNTY_FIPS.get((a.get("COUNTY") or "").strip())
        pin = (a.get("PIN") or "").strip()
        if not fips or not pin or f.get("geometry") is None:
            continue
        try:
            acres = float(a.get("ACRES")) if a.get("ACRES") else None
        except (TypeError, ValueError):
            acres = None
        rows.append((fips, pin, acres, json.dumps(f["geometry"])))
        if len(rows) % 100000 == 0:
            print(f"  fetched {len(rows):,} ...")
    n = bulk_load(STAGING_COLS, rows, MERGE_SQL)
    with engine.begin() as conn:
        for fips in ("10001", "10003", "10005"):
            cnt, = conn.execute(text(
                "SELECT COUNT(*) FROM parcels WHERE county_fips = :f"),
                {"f": fips}).one()
            conn.execute(text("""
                INSERT INTO parcel_source (county_fips, source_kind,
                    source_url, source_layer, last_loaded_at, parcel_count,
                    notes)
                VALUES (:fips, 'de_firstmap', :url, 'DE_StateParcels',
                        now(), :n, 'Geometry+acres only; owner/value need county augmentation')
                ON CONFLICT (county_fips) DO UPDATE SET
                    source_kind = EXCLUDED.source_kind,
                    last_loaded_at = now(),
                    parcel_count = EXCLUDED.parcel_count,
                    notes = EXCLUDED.notes
            """), {"fips": fips, "url": URL, "n": cnt})
        stale = conn.execute(text(
            "DELETE FROM parcels WHERE county_fips LIKE '10%' "
            "AND ingested_at < :t0"), {"t0": run_started})
        if stale.rowcount:
            print(f"  purged {stale.rowcount:,} stale rows")
    print(f"Done. {n:,} DE parcels staged.")


if __name__ == "__main__":
    main()
