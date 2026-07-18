"""
Maryland parcels + assessment attributes from MD iMap ParcelBoundaries
(SDAT-derived). One load fills parcels AND parcel_assessments:
address, land/improvement/total values, year built, land use, and the
last transfer (TRADATE + CONSIDR1 consideration). Owner NAMES are not
in MD's public layer (stripped for privacy) — SDATWEBADR carries the
per-parcel SDAT link instead.

Usage:
    python -m ingest.parcels_md            # all in-scope MD counties
    python -m ingest.parcels_md 24021      # one county
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime

from sqlalchemy import text

from .db import get_engine
from .http import make_session
from .staging import bulk_load

URL = ("https://mdgeodata.md.gov/imap/rest/services/PlanningCadastre/"
       "MD_ParcelBoundaries/MapServer/0/query")
PAGE_SIZE = 1000

SESSION = make_session()

JURSCODE = {
    "24003": "ANNE", "24510": "BACI", "24005": "BACO", "24009": "CALV",
    "24011": "CARO", "24013": "CARR", "24015": "CECI", "24017": "CHAR",
    "24019": "DORC", "24021": "FRED", "24025": "HARF", "24027": "HOWA",
    "24029": "KENT", "24031": "MONT", "24033": "PRIN", "24035": "QUEE",
    "24037": "STMA", "24041": "TALB", "24043": "WASH",
}

OUT_FIELDS = ("ACCTID,JURSCODE,ADDRESS,CITY,ZIPCODE,ACRES,LU,DESCLU,"
              "YEARBLT,SQFTSTRC,NFMLNDVL,NFMIMPVL,NFMTTLVL,TRADATE,"
              "CONSIDR1,SDATWEBADR")

STAGING_COLS = {
    "county_fips":  "text",
    "acctid":       "text",
    "address":      "text",
    "acres":        "numeric",
    "land_use":     "text",
    "year_built":   "smallint",
    "val_land":     "numeric",
    "val_impr":     "numeric",
    "val_total":    "numeric",
    "sale_price":   "numeric",
    "sale_date":    "date",
    "attrs":        "text",
    "geom_json":    "text",
}

MERGE_SQL_TEMPLATE = """
    INSERT INTO parcels (county_fips, parcel_local_id, acres,
                         assessed_value, situs_address, source_attrs, geom)
    SELECT s.county_fips, s.acctid, s.acres, s.val_total, s.address,
           s.attrs::jsonb,
           ST_CollectionExtract(ST_MakeValid(
               ST_SetSRID(ST_GeomFromGeoJSON(s.geom_json), 4326)), 3)
    FROM _staging s
    WHERE s.acctid IS NOT NULL
      AND NOT ST_IsEmpty(ST_CollectionExtract(ST_MakeValid(
          ST_SetSRID(ST_GeomFromGeoJSON(s.geom_json), 4326)), 3))
    ON CONFLICT (county_fips, parcel_local_id) DO UPDATE SET
        acres          = EXCLUDED.acres,
        assessed_value = EXCLUDED.assessed_value,
        situs_address  = EXCLUDED.situs_address,
        source_attrs   = EXCLUDED.source_attrs,
        geom           = EXCLUDED.geom,
        ingested_at    = now();

    INSERT INTO parcel_assessments (
        parcel_id, appraised_total, appraised_land, appraised_building,
        year_built, land_use, sale_price, sale_date, source)
    SELECT p.id, s.val_total, s.val_land, s.val_impr,
           s.year_built, s.land_use, s.sale_price, s.sale_date, 'md_imap'
    FROM _staging s
    JOIN parcels p ON p.county_fips = s.county_fips
                  AND p.parcel_local_id = s.acctid
    ON CONFLICT (parcel_id) DO UPDATE SET
        appraised_total    = EXCLUDED.appraised_total,
        appraised_land     = EXCLUDED.appraised_land,
        appraised_building = EXCLUDED.appraised_building,
        year_built         = EXCLUDED.year_built,
        land_use           = EXCLUDED.land_use,
        sale_price         = EXCLUDED.sale_price,
        sale_date          = EXCLUDED.sale_date,
        source             = EXCLUDED.source,
        loaded_at          = now();
"""


def _esri_date(ms):
    if not isinstance(ms, (int, float)) or not ms:
        return None
    try:
        return datetime.utcfromtimestamp(ms / 1000).date()
    except (OSError, OverflowError, ValueError):
        return None


def _num(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def fetch_county(jurs: str):
    offset = 0
    while True:
        r = SESSION.get(URL, params={
            "where": f"JURSCODE = '{jurs}'",
            "outFields": OUT_FIELDS,
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


def load_county(fips: str, name: str) -> int:
    jurs = JURSCODE[fips]
    rows = []
    for f in fetch_county(jurs):
        if f.get("geometry") is None:
            continue
        a = f.get("properties") or {}
        acct = (a.get("ACCTID") or "").strip()
        if not acct:
            continue
        yb = a.get("YEARBLT")
        rows.append((
            fips, acct,
            " ".join(x for x in (a.get("ADDRESS"), a.get("CITY")) if x) or None,
            _num(a.get("ACRES")),
            a.get("DESCLU"),
            int(yb) if _num(yb) else None,
            _num(a.get("NFMLNDVL")), _num(a.get("NFMIMPVL")),
            _num(a.get("NFMTTLVL")),
            _num(a.get("CONSIDR1")), _esri_date(a.get("TRADATE")),
            json.dumps({"jurscode": jurs, "lu": a.get("LU"),
                        "sqft_struct": a.get("SQFTSTRC"),
                        "sdat_url": a.get("SDATWEBADR")}),
            json.dumps(f["geometry"]),
        ))
    # dedupe on acctid (condo stacks share geometry; keep max value)
    by_id = {}
    for r_ in rows:
        k = r_[1]
        if k not in by_id or (r_[8] or 0) > (by_id[k][8] or 0):
            by_id[k] = r_
    engine = get_engine()
    with engine.begin() as conn:
        run_started, = conn.execute(text("SELECT now()")).one()
    n = bulk_load(STAGING_COLS, by_id.values(), MERGE_SQL_TEMPLATE)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO parcel_source (county_fips, source_kind, source_url,
                                       source_layer, last_loaded_at, parcel_count)
            VALUES (:fips, 'md_imap', :url, 'ParcelBoundaries', now(), :n)
            ON CONFLICT (county_fips) DO UPDATE SET
                source_kind = EXCLUDED.source_kind,
                source_url = EXCLUDED.source_url,
                last_loaded_at = now(), parcel_count = EXCLUDED.parcel_count
        """), {"fips": fips, "url": URL, "n": n})
        stale = conn.execute(text(
            "DELETE FROM parcels WHERE county_fips = :fips "
            "AND ingested_at < :t0"), {"fips": fips, "t0": run_started})
        if stale.rowcount:
            print(f"  purged {stale.rowcount:,} stale rows")
    return n


def main() -> None:
    explicit = [a for a in sys.argv[1:] if a.startswith("24")]
    engine = get_engine()
    with engine.connect() as conn:
        targets = conn.execute(text(
            "SELECT county_fips, name FROM counties_in_scope "
            "WHERE state_abbr='MD'" +
            (" AND county_fips = ANY(:f)" if explicit else "") +
            " ORDER BY county_fips",
        ), {"f": explicit} if explicit else {}).all()
    total = 0
    t0 = time.time()
    for fips, name in targets:
        try:
            n = load_county(fips, name)
            total += n
            print(f"[{fips}] {name}: {n:,} parcels ({time.time()-t0:.0f}s)")
        except Exception as exc:
            print(f"[{fips}] {name} FAILED: {exc}")
    print(f"\nDone. {total:,} MD parcels. Cascade: refresh-candidates, "
          f"parcel_cells, metrics.")


if __name__ == "__main__":
    main()
