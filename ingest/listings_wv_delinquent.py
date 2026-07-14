"""
WV delinquent-lands listings from the WV GIS Tech Center's statewide
Delinquent Properties service (polygon layer, keyed by GISPID — the
same id as our WV parcels, so the parcel join is exact).

Status semantics (tblInput_latest_status):
    'No Bid'    unsold at tax auction — often purchasable directly from
                the WV State Auditor's land office; the standing inventory
    'Deed'      tax deed issued / in process
    'Sold'      sold at auction (gone)
    'Redeemed'  owner paid the taxes (gone)
    'Dismissed'/'Suspended'  process halted

Run:
    python -m ingest.listings_wv_delinquent
"""
from __future__ import annotations

import json
import time

from sqlalchemy import text

from .db import get_engine
from .http import make_session
from .parcels_wv import fips_to_wv_code
from .staging import bulk_load

URL = ("https://services.wvgis.wvu.edu/arcgis/rest/services/"
       "Planning_Cadastre/Delinquent_Properties/MapServer/1/query")
PAGE_SIZE = 2000

SESSION = make_session()

OUT_FIELDS = [
    "GISPID", "CountyID", "tblInput_latest_status", "certno",
    "Acres_C", "FullOwnerName", "FullPhysicalAddress", "county",
    "deed_book", "page_number", "account_number", "description",
]

STAGING_COLS = {
    "gispid":      "text",
    "county_fips": "text",
    "status":      "text",
    "certno":      "text",
    "acres":       "numeric",
    "owner":       "text",
    "address":     "text",
    "attrs":       "text",
}

MERGE_SQL = """
    DELETE FROM listings WHERE source = 'wv_delinquent';
    INSERT INTO listings (source, source_listing_id, listing_kind, status,
                          acres, title, address, parcel_id, source_attrs)
    SELECT DISTINCT ON (s.gispid)
        'wv_delinquent',
        s.gispid,
        'tax_sale',
        s.status,
        s.acres,
        'Tax-delinquent: ' || COALESCE(s.owner, 'unknown owner')
            || ' (cert ' || COALESCE(s.certno, '?') || ')',
        s.address,
        p.id,
        s.attrs::jsonb
    FROM _staging s
    LEFT JOIN parcels p
        ON p.county_fips = s.county_fips
       AND p.parcel_local_id = s.gispid
    ORDER BY s.gispid, s.certno DESC;
"""


def fetch_all(wv_codes: list[str]) -> list[dict]:
    codes = ",".join(f"'{c}'" for c in wv_codes)
    features: list[dict] = []
    offset = 0
    while True:
        r = SESSION.get(URL, params={
            "where": f"CountyID IN ({codes})",
            "outFields": ",".join(OUT_FIELDS),
            "returnGeometry": "false",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
        }, timeout=120)
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
        wv = conn.execute(text(
            "SELECT county_fips FROM counties_in_scope "
            "WHERE state_abbr='WV' ORDER BY county_fips")).all()
    fips_by_code = {fips_to_wv_code(r[0]): r[0] for r in wv}

    print(f"Fetching WV delinquent properties for {len(fips_by_code)} counties")
    feats = fetch_all(list(fips_by_code))
    print(f"  {len(feats):,} records")

    rows = []
    for f in feats:
        a = f["attributes"]
        gispid = a.get("GISPID")
        code = a.get("CountyID")
        if not gispid or code not in fips_by_code:
            continue
        rows.append((
            gispid,
            fips_by_code[code],
            a.get("tblInput_latest_status"),
            str(a.get("certno") or ""),
            a.get("Acres_C"),
            (a.get("FullOwnerName") or "").strip() or None,
            (a.get("FullPhysicalAddress") or "").strip() or None,
            json.dumps({
                "county": a.get("county"),
                "deed_book": a.get("deed_book"),
                "page_number": a.get("page_number"),
                "account_number": a.get("account_number"),
                "description": a.get("description"),
            }),
        ))

    n = bulk_load(STAGING_COLS, rows, MERGE_SQL)
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO layer_loads (layer, scope, feature_count)
            VALUES ('listings_wv_delinquent', 'wv', :n)
            ON CONFLICT (layer, scope) DO UPDATE SET
                loaded_at = now(), feature_count = EXCLUDED.feature_count
        """), {"n": n})
        matched, = conn.execute(text(
            "SELECT COUNT(*) FROM listings "
            "WHERE source='wv_delinquent' AND parcel_id IS NOT NULL")).one()
        total, = conn.execute(text(
            "SELECT COUNT(*) FROM listings WHERE source='wv_delinquent'")).one()
    print(f"Loaded {total:,} listings; {matched:,} joined to parcels "
          f"({100.0 * matched / max(total, 1):.0f}%)")


if __name__ == "__main__":
    main()
