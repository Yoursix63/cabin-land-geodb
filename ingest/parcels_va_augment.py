"""
VA per-county augmentation: owner, address, assessed values, and sale
price/date from county-run GIS services, joined to our VGIN geometries
SPATIALLY (county feature centroid inside our parcel polygon) so
parcel-id format differences can't break the match.

The registry below is the product of per-county endpoint discovery —
most VA counties hide data behind commercial viewers; these publish
open ArcGIS services. Add counties as they're discovered (see
docs/DECISIONS.md for the discovery recipe).

Usage:
    python -m ingest.parcels_va_augment            # all configured
    python -m ingest.parcels_va_augment 51139      # one county
"""
from __future__ import annotations

import sys
import time
from datetime import date, datetime

from sqlalchemy import text

from .db import get_engine
from .http import make_session
from .staging import bulk_load

SESSION = make_session()


def _esri_date(v):
    """Esri date: epoch ms, or a formatted string depending on server."""
    if not v:
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.utcfromtimestamp(v / 1000).date()
        except (OSError, OverflowError, ValueError):
            return None
    s = str(v).strip()[:19]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d",
                "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _ymd(y, m, d):
    try:
        y, m, d = int(y), int(m), int(d)
        if y < 1800:
            return None
        return date(y, max(1, min(12, m)), max(1, min(28, d)))
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


# --- per-county adapters: raw attributes -> normalized dict ---------------

def adapt_spotsylvania(a: dict) -> dict:
    land = _num(a.get("LANDASSESSMENT"))
    bldg = _num(a.get("BLDGASSESSMENT"))
    return {
        "owner":     (a.get("OwnerSearch") or "").strip() or None,
        "address":   " ".join(x for x in (a.get("PROPADDRESS"),
                                          a.get("PROPCITY")) if x) or None,
        "land":      land,
        "building":  bldg,
        "total":     (land or 0) + (bldg or 0) or None,
        "year_built": int(a["YEARBUILT"]) if _num(a.get("YEARBUILT")) else None,
        "land_use":  a.get("LANDUSE"),
        "sale_price": _num(a.get("SALEPRICE")),
        "sale_date": _esri_date(a.get("TRANSFERDATE")),
        "book":      a.get("BOOKNUM"),
        "page":      a.get("PAGE"),
    }


def adapt_page(a: dict) -> dict:
    return {
        "owner":     (a.get("GIS_MLNAM") or "").strip() or None,
        "address":   (a.get("GIS_FULL_ADDR") or "").strip() or None,
        "land":      _num(a.get("TOTLD16")),
        "building":  _num(a.get("IMPRV16")),
        "total":     _num(a.get("TOTPR16")),
        "year_built": None,
        "land_use":  str(a.get("LU16")) if a.get("LU16") is not None else None,
        "sale_price": _num(a.get("GSELLP")),
        "sale_date": _ymd(a.get("GYRSLD"), a.get("GMOSLD"), a.get("GDASLD")),
        "book":      None,
        "page":      None,
    }


REGISTRY = {
    "51177": {
        "name": "Spotsylvania",
        "url": ("https://gis.spotsylvania.va.us/arcgis/rest/services/"
                "Spotsylvania_Public_Prod/FeatureServer/8"),
        "out_fields": ("OwnerSearch,PROPADDRESS,PROPCITY,LANDASSESSMENT,"
                       "BLDGASSESSMENT,YEARBUILT,LANDUSE,SALEPRICE,"
                       "TRANSFERDATE,BOOKNUM,PAGE"),
        "page_size": 2000,
        "adapter": adapt_spotsylvania,
    },
    "51139": {
        "name": "Page",
        "url": ("https://services1.arcgis.com/vzTTDUcNo7s6eLCJ/arcgis/rest/"
                "services/PageCoParcelsAssessment_Pub/FeatureServer/0"),
        "out_fields": ("GIS_MLNAM,GIS_FULL_ADDR,TOTLD16,IMPRV16,TOTPR16,"
                       "LU16,GSELLP,GMOSLD,GDASLD,GYRSLD"),
        "page_size": 1000,
        "adapter": adapt_page,
    },
}

STAGING_COLS = {
    "county_fips": "text",
    "owner":       "text",
    "address":     "text",
    "land":        "numeric",
    "building":    "numeric",
    "total":       "numeric",
    "year_built":  "smallint",
    "land_use":    "text",
    "sale_price":  "numeric",
    "sale_date":   "date",
    "deed_book":   "text",
    "deed_page":   "text",
    "lon":         "double precision",
    "lat":         "double precision",
}

# Spatial join: county-record centroid inside our VGIN parcel polygon.
# When several records land in one parcel (condo stacks), keep the
# highest-value one.
MERGE_SQL_TEMPLATE = """
    CREATE TEMP TABLE _matched AS
    SELECT DISTINCT ON (p.id)
        p.id AS parcel_id, s.*
    FROM _staging s
    JOIN parcels p
      ON p.county_fips = s.county_fips
     AND ST_Contains(p.geom, ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326))
    ORDER BY p.id, s.total DESC NULLS LAST;

    UPDATE parcels p SET
        owner_name    = COALESCE(m.owner, p.owner_name),
        situs_address = COALESCE(m.address, p.situs_address),
        assessed_value = COALESCE(m.total, p.assessed_value)
    FROM _matched m WHERE m.parcel_id = p.id;

    INSERT INTO parcel_assessments (
        parcel_id, appraised_total, appraised_land, appraised_building,
        year_built, land_use, deed_book, deed_page,
        sale_price, sale_date, source)
    SELECT parcel_id, total, land, building, year_built, land_use,
           deed_book, deed_page, sale_price, sale_date,
           'county_rest:{fips}'
    FROM _matched
    ON CONFLICT (parcel_id) DO UPDATE SET
        appraised_total    = EXCLUDED.appraised_total,
        appraised_land     = EXCLUDED.appraised_land,
        appraised_building = EXCLUDED.appraised_building,
        year_built         = EXCLUDED.year_built,
        land_use           = EXCLUDED.land_use,
        deed_book          = EXCLUDED.deed_book,
        deed_page          = EXCLUDED.deed_page,
        sale_price         = EXCLUDED.sale_price,
        sale_date          = EXCLUDED.sale_date,
        source             = EXCLUDED.source,
        loaded_at          = now();

    DROP TABLE _matched;
"""


def fetch_county(cfg: dict):
    offset = 0
    while True:
        r = SESSION.get(f"{cfg['url']}/query", params={
            "where": "1=1",
            "outFields": cfg["out_fields"],
            "returnGeometry": "false",
            "returnCentroid": "true",
            "outSR": "4326",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": cfg["page_size"],
        }, timeout=300)
        r.raise_for_status()
        page = r.json().get("features", [])
        yield from page
        if len(page) < cfg["page_size"]:
            return
        offset += cfg["page_size"]
        time.sleep(0.05)


def load_county(fips: str, cfg: dict) -> tuple[int, int]:
    rows = []
    for f in fetch_county(cfg):
        cen = f.get("centroid") or {}
        if "x" not in cen:
            continue
        n = cfg["adapter"](f.get("attributes") or {})
        rows.append((fips, n["owner"], n["address"], n["land"],
                     n["building"], n["total"], n["year_built"],
                     n["land_use"], n["sale_price"], n["sale_date"],
                     n["book"], n["page"], cen["x"], cen["y"]))
    staged = bulk_load(STAGING_COLS, rows, MERGE_SQL_TEMPLATE.format(fips=fips))
    engine = get_engine()
    with engine.begin() as conn:
        matched, = conn.execute(text(
            "SELECT COUNT(*) FROM parcel_assessments "
            "WHERE source = :s"), {"s": f"county_rest:{fips}"}).one()
        conn.execute(text("""
            INSERT INTO layer_loads (layer, scope, feature_count, notes)
            VALUES ('va_augment', :scope, :n, :notes)
            ON CONFLICT (layer, scope) DO UPDATE SET
                loaded_at = now(),
                feature_count = EXCLUDED.feature_count,
                notes = EXCLUDED.notes
        """), {"scope": fips, "n": matched,
               "notes": f"{cfg['name']}; {staged} staged"})
    return staged, matched


def main() -> None:
    explicit = [a for a in sys.argv[1:] if a.startswith("51")]
    targets = {f: c for f, c in REGISTRY.items()
               if not explicit or f in explicit}
    for fips, cfg in targets.items():
        staged, matched = load_county(fips, cfg)
        print(f"[{fips}] {cfg['name']}: {staged:,} county records -> "
              f"{matched:,} parcels matched")
    print("\nDone. Remember: python manage.py refresh-candidates picks up "
          "owner/address into the matview.")


if __name__ == "__main__":
    main()
