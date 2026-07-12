"""
Load VA parcels for in-scope counties from VGIN's statewide MapServer.

Source:
    https://vginmaps.vdem.virginia.gov/arcgis/rest/services/
        VA_Base_Layers/VA_Parcels/MapServer/0
    (~4.2M parcels statewide; paginated 2000/page)

VGIN provides only parcel geometry + FIPS + LOCALITY + PARCELID + VGIN_QPID.
Owner name, address, assessed value etc. are NOT in this layer; those
live at the county level and need separate per-county augmentation later.
Acres are computed from geometry via ST_Area(geography) in the upsert.

Usage:
    python -m ingest.parcels_va                       # all in-scope VA
    python -m ingest.parcels_va 51091                 # one county
    python -m ingest.parcels_va 51091 51015 51017     # several
"""
from __future__ import annotations

import json
import sys
import time
from typing import Iterable

import requests
from sqlalchemy import text
from tqdm import tqdm

from .db import get_engine

URL = (
    "https://vginmaps.vdem.virginia.gov/arcgis/rest/services/"
    "VA_Base_Layers/VA_Parcels/MapServer/0/query"
)
PAGE_SIZE = 2000
CHUNK_SIZE = 1000

OUT_FIELDS = ["FIPS", "LOCALITY", "PARCELID", "PTM_ID", "VGIN_QPID", "LASTUPDATE"]

# Acres computed from geography; geom validated to handle slivers, self-
# intersections, and the GeometryCollection that ST_MakeValid can return
# for some broken polygons. ST_CollectionExtract(..., 3) keeps only
# polygon parts, yielding a clean MultiPolygon (or empty -> WHERE clause
# silently skips).
UPSERT_PARCEL = text("""
    WITH g AS (
        SELECT ST_CollectionExtract(
            ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(:geom_json), 4326)),
            3
        ) AS geom
    )
    INSERT INTO parcels (
        county_fips, parcel_local_id, acres, source_attrs, geom
    )
    SELECT
        :county_fips, :parcel_local_id,
        ST_Area(g.geom::geography) / 4046.8564224,
        CAST(:source_attrs AS jsonb),
        g.geom
    FROM g
    WHERE NOT ST_IsEmpty(g.geom)
    ON CONFLICT (county_fips, parcel_local_id) DO UPDATE SET
        acres        = EXCLUDED.acres,
        source_attrs = EXCLUDED.source_attrs,
        geom         = EXCLUDED.geom,
        ingested_at  = now()
""")

UPSERT_SOURCE = text("""
    INSERT INTO parcel_source (
        county_fips, source_kind, source_url, source_layer,
        last_loaded_at, parcel_count, notes
    )
    VALUES (:fips, 'vgin_statewide', :url, 'VA_Parcels',
            now(), :n,
            'Geometry only. Owner/address need per-county augmentation.')
    ON CONFLICT (county_fips) DO UPDATE SET
        source_kind    = EXCLUDED.source_kind,
        source_url     = EXCLUDED.source_url,
        source_layer   = EXCLUDED.source_layer,
        last_loaded_at = EXCLUDED.last_loaded_at,
        parcel_count   = EXCLUDED.parcel_count,
        notes          = EXCLUDED.notes
""")


def fetch_county(fips: str) -> Iterable[dict]:
    """Yield parcel features for one VA county, paginating through the service."""
    offset = 0
    while True:
        params = {
            "where": f"FIPS='{fips}'",
            "outFields": ",".join(OUT_FIELDS),
            "outSR": "4326",
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
        }
        r = requests.get(URL, params=params, timeout=180)
        r.raise_for_status()
        data = r.json()
        page = data.get("features", [])
        for feat in page:
            yield feat
        if len(page) < PAGE_SIZE:
            return
        offset += PAGE_SIZE
        time.sleep(0.05)


def feature_to_row(feat: dict) -> dict | None:
    if feat.get("geometry") is None:
        return None
    p = feat.get("properties") or {}
    fips = p.get("FIPS")
    pid = p.get("PARCELID") or p.get("PTM_ID")
    if not fips or not pid:
        return None
    return {
        "county_fips":     fips,
        "parcel_local_id": str(pid).strip(),
        "source_attrs":    json.dumps({
            "vgin_qpid": p.get("VGIN_QPID"),
            "ptm_id":    p.get("PTM_ID"),
            "locality":  p.get("LOCALITY"),
            "lastupdate": p.get("LASTUPDATE"),
        }),
        "geom_json": json.dumps(feat["geometry"]),
    }


def dedupe(rows: Iterable[dict]) -> list[dict]:
    seen: dict[tuple[str, str], dict] = {}
    for r in rows:
        seen[(r["county_fips"], r["parcel_local_id"])] = r
    return list(seen.values())


def get_target_counties(explicit: list[str]) -> list[tuple[str, str]]:
    engine = get_engine()
    with engine.connect() as conn:
        if explicit:
            rows = conn.execute(text(
                "SELECT county_fips, name FROM counties_in_scope "
                "WHERE state_abbr='VA' AND county_fips = ANY(:fips) "
                "ORDER BY name"
            ), {"fips": explicit}).all()
        else:
            rows = conn.execute(text(
                "SELECT county_fips, name FROM counties_in_scope "
                "WHERE state_abbr='VA' ORDER BY name"
            )).all()
    return [(r[0], r[1]) for r in rows]


def load_county(fips: str, name: str) -> int:
    print(f"\n[{fips}] {name}")
    rows: list[dict] = []
    for feat in fetch_county(fips):
        row = feature_to_row(feat)
        if row is not None:
            rows.append(row)
    rows = dedupe(rows)
    print(f"  {len(rows):,} valid rows")
    if not rows:
        return 0
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(UPSERT_SOURCE, {"fips": fips, "url": URL, "n": len(rows)})
        chunks = list(range(0, len(rows), CHUNK_SIZE))
        for i in tqdm(chunks, unit="chunk", desc="  upsert"):
            conn.execute(UPSERT_PARCEL, rows[i:i + CHUNK_SIZE])
    return len(rows)


def main() -> None:
    explicit = [a for a in sys.argv[1:] if a.startswith("51")]
    targets = get_target_counties(explicit)
    print(f"Loading {len(targets)} VA counties from VGIN VA_Parcels\n")
    grand_total = 0
    for fips, name in targets:
        grand_total += load_county(fips, name)
    print(f"\nDone. Loaded {grand_total:,} VA parcels total.")


if __name__ == "__main__":
    main()
