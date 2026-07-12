"""
Load WV parcels for in-scope counties from the WV statewide MapServer.

Source:
    https://services.wvgis.wvu.edu/arcgis/rest/services/
        Planning_Cadastre/WV_Parcels/MapServer/0
    (~1.39M parcels statewide; pagination at 2000/page)

For each in-scope WV county, queries by CountyID, paginates through all
features, normalizes to the parcels schema, and upserts.

Run:
    python -m ingest.parcels_wv
"""
from __future__ import annotations

import json
import time
from typing import Iterable

from sqlalchemy import text
from tqdm import tqdm

from .db import get_engine
from .http import make_session

SESSION = make_session()

URL = (
    "https://services.wvgis.wvu.edu/arcgis/rest/services/"
    "Planning_Cadastre/WV_Parcels/MapServer/0/query"
)
PAGE_SIZE = 2000
CHUNK_SIZE = 1000

OUT_FIELDS = [
    "GISPID", "ROOTID", "CountyID",
    "Map", "Parcel", "Suffix", "Dist", "Label",
    "Acres_C", "CleanParcelID",
    "FullOwnerName", "FullOwnerAddress", "FullPhysicalAddress",
    "FullLegalDescription",
]

UPSERT_PARCEL = text("""
    WITH g AS (
        SELECT ST_CollectionExtract(
            ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(:geom_json), 4326)),
            3
        ) AS geom
    )
    INSERT INTO parcels (
        county_fips, parcel_local_id, acres,
        owner_name, situs_address, source_attrs, geom
    )
    SELECT
        :county_fips, :parcel_local_id, :acres,
        :owner_name, :situs_address, CAST(:source_attrs AS jsonb),
        g.geom
    FROM g
    WHERE NOT ST_IsEmpty(g.geom)
    ON CONFLICT (county_fips, parcel_local_id) DO UPDATE SET
        acres         = EXCLUDED.acres,
        owner_name    = EXCLUDED.owner_name,
        situs_address = EXCLUDED.situs_address,
        source_attrs  = EXCLUDED.source_attrs,
        geom          = EXCLUDED.geom,
        ingested_at   = now()
""")

UPSERT_SOURCE = text("""
    INSERT INTO parcel_source (
        county_fips, source_kind, source_url, source_layer,
        last_loaded_at, parcel_count
    )
    VALUES (:fips, 'wv_statewide', :url, 'WVParcels', now(), :n)
    ON CONFLICT (county_fips) DO UPDATE SET
        source_kind    = EXCLUDED.source_kind,
        source_url     = EXCLUDED.source_url,
        source_layer   = EXCLUDED.source_layer,
        last_loaded_at = EXCLUDED.last_loaded_at,
        parcel_count   = EXCLUDED.parcel_count
""")


def wv_code_to_fips(code: str) -> str:
    """WV state county code (01..55, alphabetical) -> 5-digit FIPS."""
    return f"54{int(code) * 2 - 1:03d}"


def fips_to_wv_code(fips: str) -> str:
    county_part = int(fips[-3:])
    return f"{(county_part + 1) // 2:02d}"


def fetch_county(wv_code: str) -> list[dict]:
    """Paginate through every parcel for one WV county code."""
    features: list[dict] = []
    offset = 0
    while True:
        params = {
            "where": f"CountyID='{wv_code}'",
            "outFields": ",".join(OUT_FIELDS),
            "outSR": "4326",
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
        }
        r = SESSION.get(URL, params=params, timeout=180)
        r.raise_for_status()
        data = r.json()
        page = data.get("features", [])
        features.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.05)
    return features


def feature_to_row(feat: dict) -> dict | None:
    if feat.get("geometry") is None:
        return None
    p = feat.get("properties") or feat.get("attributes") or {}
    wv_code = p.get("CountyID")
    if not wv_code:
        return None
    local_id = p.get("GISPID") or p.get("ROOTID") or p.get("CleanParcelID")
    if not local_id:
        return None
    return {
        "county_fips":     wv_code_to_fips(wv_code),
        "parcel_local_id": str(local_id).strip(),
        "acres":           p.get("Acres_C"),
        "owner_name":      (p.get("FullOwnerName") or "").strip() or None,
        "situs_address":   (p.get("FullPhysicalAddress") or "").strip() or None,
        "source_attrs":    json.dumps({
            "wv_county_code": wv_code,
            "map":            p.get("Map"),
            "parcel":         p.get("Parcel"),
            "suffix":         p.get("Suffix"),
            "dist":           p.get("Dist"),
            "label":          p.get("Label"),
            "rootid":         p.get("ROOTID"),
            "clean_parcel_id": p.get("CleanParcelID"),
            "legal":          p.get("FullLegalDescription"),
            "owner_address":  p.get("FullOwnerAddress"),
        }),
        "geom_json": json.dumps(feat["geometry"]),
    }


def dedupe_by_local_id(rows: Iterable[dict]) -> list[dict]:
    seen: dict[tuple[str, str], dict] = {}
    for r in rows:
        seen[(r["county_fips"], r["parcel_local_id"])] = r
    return list(seen.values())


FRESH_DAYS = 30


def main() -> None:
    import sys
    force = "--force" in sys.argv
    engine = get_engine()
    with engine.connect() as conn:
        in_scope = conn.execute(text(
            "SELECT county_fips, name FROM counties_in_scope "
            "WHERE state_abbr='WV' ORDER BY name"
        )).all()
        if not force:
            fresh = {r[0] for r in conn.execute(text(
                "SELECT county_fips FROM parcel_source "
                "WHERE parcel_count IS NOT NULL "
                "AND last_loaded_at > now() - make_interval(days => :d)"
            ), {"d": FRESH_DAYS}).all()}
            skipped = [n for f, n in in_scope if f in fresh]
            in_scope = [(f, n) for f, n in in_scope if f not in fresh]
            if skipped:
                print(f"Skipping {len(skipped)} fresh counties "
                      f"(--force to reload)")

    print(f"Loading {len(in_scope)} WV counties from WV_Parcels MapServer\n")

    grand_total = 0
    for fips, name in in_scope:
        wv_code = fips_to_wv_code(fips)
        print(f"[{wv_code}] {name} County (FIPS {fips})")
        feats = fetch_county(wv_code)
        print(f"  fetched {len(feats):,} features")
        rows = [r for r in (feature_to_row(f) for f in feats) if r is not None]
        rows = dedupe_by_local_id(rows)
        print(f"  {len(rows):,} valid rows after dedupe")
        if not rows:
            continue
        with engine.begin() as conn:
            conn.execute(UPSERT_SOURCE,
                         {"fips": fips, "url": URL, "n": len(rows)})
            chunks = range(0, len(rows), CHUNK_SIZE)
            # disable=None -> progress bar only on a real terminal
            for i in tqdm(list(chunks), unit="chunk", desc="  upsert", disable=None):
                conn.execute(UPSERT_PARCEL, rows[i:i + CHUNK_SIZE])
        grand_total += len(rows)
        print()

    print(f"Done. Loaded {grand_total:,} WV parcels total.")


if __name__ == "__main__":
    main()
