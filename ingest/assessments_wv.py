"""
WV assessment attributes from the WVGISTC annual tax-parcel product
(per-county zips on the WV GIS Clearinghouse). Appraised land/building/
total values, year built, land use, tax class, deed + latest-transfer
book/page. Joined to parcels via the packed CleanParcelID we stored in
source_attrs at parcel load.

Multi-card parcels (several assessment cards per parcel) are
aggregated: values summed, earliest nonzero YearBuilt kept.

Usage:
    python -m ingest.assessments_wv            # all in-scope WV counties
    python -m ingest.assessments_wv 54031      # one county
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import pyogrio
from sqlalchemy import text

from .db import get_engine
from .http import make_session
from .parcels_wv import fips_to_wv_code
from .staging import bulk_load

BASE = ("https://data.wvgis.wvu.edu/pub/Clearinghouse/"
        "planningLanduseCadastres/Parcels/WVGISTC_2025/CountySplits/")
YEAR = 2025

# Clearinghouse filenames: {Name}_{code}_WVGISTCTax_2025_UTM83.zip
COUNTY_FILE = {
    "54003": "Berkeley_02",
    "54027": "Hampshire_14",
    "54031": "Hardy_16",
    "54037": "Jefferson_19",
    "54065": "Morgan_33",
}

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "raw" / "wv_tax"

SESSION = make_session()

STAGING_COLS = {
    "county_fips":        "text",
    "clean_parcel_id":    "text",
    "tax_year":           "smallint",
    "appraised_total":    "numeric",
    "appraised_land":     "numeric",
    "appraised_building": "numeric",
    "year_built":         "smallint",
    "land_use":           "text",
    "tax_class":          "text",
    "deed_book":          "text",
    "deed_page":          "text",
    "new_book":           "text",
    "new_page":           "text",
    "cards":              "smallint",
}

MERGE_SQL_TEMPLATE = """
    DELETE FROM parcel_assessments
    WHERE source = 'wvgistc_tax' AND parcel_id IN
        (SELECT id FROM parcels WHERE county_fips = '{fips}');
    INSERT INTO parcel_assessments (
        parcel_id, tax_year, appraised_total, appraised_land,
        appraised_building, year_built, land_use, tax_class,
        deed_book, deed_page, new_book, new_page, cards, source)
    SELECT p.id, s.tax_year, s.appraised_total, s.appraised_land,
           s.appraised_building, s.year_built, s.land_use, s.tax_class,
           s.deed_book, s.deed_page, s.new_book, s.new_page, s.cards,
           'wvgistc_tax'
    FROM _staging s
    JOIN parcels p
      ON p.county_fips = s.county_fips
     AND p.source_attrs->>'clean_parcel_id' = s.clean_parcel_id
    ON CONFLICT (parcel_id) DO UPDATE SET
        tax_year           = EXCLUDED.tax_year,
        appraised_total    = EXCLUDED.appraised_total,
        appraised_land     = EXCLUDED.appraised_land,
        appraised_building = EXCLUDED.appraised_building,
        year_built         = EXCLUDED.year_built,
        land_use           = EXCLUDED.land_use,
        tax_class          = EXCLUDED.tax_class,
        deed_book          = EXCLUDED.deed_book,
        deed_page          = EXCLUDED.deed_page,
        new_book           = EXCLUDED.new_book,
        new_page           = EXCLUDED.new_page,
        cards              = EXCLUDED.cards,
        loaded_at          = now();

    -- keep the parcels.assessed_value convenience column in sync
    UPDATE parcels p SET assessed_value = pa.appraised_total
    FROM parcel_assessments pa
    WHERE pa.parcel_id = p.id AND p.county_fips = '{fips}';
"""


def fetch_zip(fips: str) -> Path:
    stem = COUNTY_FILE[fips]
    path = CACHE / f"{stem.split('_')[0]}_{stem.split('_')[1]}.zip"
    if path.exists() and path.stat().st_size > 0:
        return path
    url = f"{BASE}{stem}_WVGISTCTax_{YEAR}_UTM83.zip"
    r = SESSION.get(url, timeout=900)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate assessment cards to one row per parcel."""
    num = lambda s: pd.to_numeric(s, errors="coerce")
    df = df.assign(
        TotalAppra=num(df["TotalAppra"]).fillna(0),
        LandApprai=num(df["LandApprai"]).fillna(0),
        BuildingAp=num(df["BuildingAp"]).fillna(0),
        YearBuilt=num(df["YearBuilt"]).replace(0, pd.NA),
        TaxYear=num(df["TaxYear"]),
    )
    g = df.groupby("CleanParce")
    out = pd.DataFrame({
        "appraised_total":    g["TotalAppra"].sum(),
        "appraised_land":     g["LandApprai"].sum(),
        "appraised_building": g["BuildingAp"].sum(),
        "year_built":         g["YearBuilt"].min(),
        "tax_year":           g["TaxYear"].max(),
        "land_use":           g["LandUse"].first(),
        "tax_class":          g["TaxClass"].first(),
        "deed_book":          g["DeedBook"].first(),
        "deed_page":          g["DeedPage"].first(),
        "new_book":           g["NewBook"].first(),
        "new_page":           g["NewPage"].first(),
        "cards":              g.size(),
    })
    return out.reset_index()


def load_county(fips: str, name: str) -> int:
    zpath = fetch_zip(fips)
    county_name = COUNTY_FILE[fips].split("_")[0]
    dbf = f"/vsizip/{zpath.as_posix()}/ParcelSummary_{YEAR}_{county_name}.dbf"
    df = pyogrio.read_dataframe(dbf, read_geometry=False)
    agg = summarize(df)

    def cell(v):
        return None if pd.isna(v) else v

    def icell(v):
        return None if pd.isna(v) else int(v)
    rows = [
        (fips, r.CleanParce, icell(r.tax_year), cell(r.appraised_total),
         cell(r.appraised_land), cell(r.appraised_building),
         icell(r.year_built), cell(r.land_use), cell(r.tax_class),
         cell(r.deed_book), cell(r.deed_page), cell(r.new_book),
         cell(r.new_page), int(r.cards))
        for r in agg.itertuples()
    ]
    n = bulk_load(STAGING_COLS, rows, MERGE_SQL_TEMPLATE.format(fips=fips))
    engine = get_engine()
    with engine.begin() as conn:
        matched, = conn.execute(text("""
            SELECT COUNT(*) FROM parcel_assessments pa
            JOIN parcels p ON p.id = pa.parcel_id
            WHERE p.county_fips = :fips
        """), {"fips": fips}).one()
        conn.execute(text("""
            INSERT INTO layer_loads (layer, scope, feature_count, notes)
            VALUES ('assessments_wv', :scope, :n, :notes)
            ON CONFLICT (layer, scope) DO UPDATE SET
                loaded_at = now(),
                feature_count = EXCLUDED.feature_count,
                notes = EXCLUDED.notes
        """), {"scope": fips, "n": matched,
               "notes": f"WVGISTC {YEAR}; {len(rows)} summary rows staged"})
    return matched


def main() -> None:
    explicit = [a for a in sys.argv[1:] if a.startswith("54")]
    CACHE.mkdir(parents=True, exist_ok=True)
    targets = [(f, COUNTY_FILE[f].split("_")[0])
               for f in (explicit or sorted(COUNTY_FILE))]
    total = 0
    t0 = time.time()
    for fips, name in targets:
        n = load_county(fips, name)
        total += n
        print(f"[{fips}] {name}: {n:,} parcels matched "
              f"({time.time()-t0:.0f}s)")
    print(f"\nDone. {total:,} parcel assessments loaded.")


if __name__ == "__main__":
    main()
