"""
Manual/semi-manual listings import from CSV — the path for VA tax-sale
auctions (no statewide data; per-county lists appear ~3 weeks before
each auction on taxva.com / forsaleatauction.biz) and any FSBO finds.

CSV columns (header required; blank cells fine):
    source, source_listing_id, listing_kind, status, price, acres,
    title, url, address, county_fips, parcel_local_id, listed_at

Rows with county_fips + parcel_local_id are joined to parcels exactly.
listing_kind: tax_sale | fsbo | mls | auction | surplus

Run:
    python -m ingest.listings_csv path/to/file.csv
    python -m ingest.listings_csv --template   # print a starter CSV
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from sqlalchemy import text

from .db import get_engine

TEMPLATE = """\
source,source_listing_id,listing_kind,status,price,acres,title,url,address,county_fips,parcel_local_id,listed_at
taxva,FAUQ-2026-001,tax_sale,Upcoming,12500,6.2,Judicial sale: 6.2ac Goldvein,https://taxva.com/...,123 Example Rd Goldvein VA,51061,6971-01-9886-000,2026-08-01
"""

UPSERT = text("""
    INSERT INTO listings (source, source_listing_id, listing_kind, status,
                          price, acres, title, url, address, listed_at,
                          parcel_id)
    VALUES (:source, :source_listing_id, :listing_kind, :status,
            :price, :acres, :title, :url, :address,
            NULLIF(:listed_at, '')::date,
            (SELECT p.id FROM parcels p
             WHERE p.county_fips = :county_fips
               AND p.parcel_local_id = :parcel_local_id))
    ON CONFLICT (source, source_listing_id) DO UPDATE SET
        listing_kind = EXCLUDED.listing_kind,
        status       = EXCLUDED.status,
        price        = EXCLUDED.price,
        acres        = EXCLUDED.acres,
        title        = EXCLUDED.title,
        url          = EXCLUDED.url,
        address      = EXCLUDED.address,
        listed_at    = EXCLUDED.listed_at,
        parcel_id    = COALESCE(EXCLUDED.parcel_id, listings.parcel_id),
        fetched_at   = now()
""")

REQUIRED = ("source", "source_listing_id", "listing_kind")


def main() -> None:
    if "--template" in sys.argv:
        print(TEMPLATE)
        return
    paths = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not paths:
        sys.exit("usage: python -m ingest.listings_csv FILE.csv "
                 "(or --template)")
    path = Path(paths[0])
    engine = get_engine()
    ok = matched = skipped = 0
    with path.open(encoding="utf-8-sig", newline="") as f, \
            engine.begin() as conn:
        for i, row in enumerate(csv.DictReader(f), start=2):
            row = {k.strip(): (v.strip() or None) for k, v in row.items()}
            if not all(row.get(k) for k in REQUIRED):
                print(f"  line {i}: missing {REQUIRED} — skipped")
                skipped += 1
                continue
            params = {k: row.get(k) for k in (
                "source", "source_listing_id", "listing_kind", "status",
                "price", "acres", "title", "url", "address",
                "county_fips", "parcel_local_id")}
            params["listed_at"] = row.get("listed_at") or ""
            result = conn.execute(UPSERT, params)
            ok += result.rowcount
        if ok:
            n, = conn.execute(text(
                "SELECT COUNT(*) FROM listings "
                "WHERE parcel_id IS NOT NULL AND source = :s"),
                {"s": params["source"]}).one()
            matched = n
    print(f"Imported/updated {ok} listings ({skipped} skipped); "
          f"{matched} of source '{params['source']}' joined to parcels.")


if __name__ == "__main__":
    main()
