"""
Load SSURGO soils for cabin-relevant counties: map-unit polygons from
Web Soil Survey per-survey-area zips, septic/drainage ratings from the
Soil Data Access (SDA) tabular service, then per-parcel septic metrics.

Survey areas map to counties as {state_abbr}{fips3} (VA091 = Highland).
Zips are cached in data/raw/ssurgo/; delete to force refetch.

Usage:
    python -m ingest.soils_ssurgo            # all cabin-relevant counties
    python -m ingest.soils_ssurgo 54031      # one county
"""
from __future__ import annotations

import io
import sys
import time
import zipfile
from pathlib import Path

import pyogrio
from shapely import to_wkb
from sqlalchemy import text

from .db import get_engine
from .http import make_session
from .staging import bulk_load

SDA_URL = "https://sdmdataaccess.nrcs.usda.gov/Tabular/post.rest"
CACHE_URL = ("https://websoilsurvey.sc.egov.usda.gov/DSD/Download/Cache/SSA/"
             "wss_SSA_{area}_%5B{date}%5D.zip")

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "raw" / "ssurgo"

SESSION = make_session()

OK_RATINGS = ("Not limited", "Somewhat limited")

# Counties whose survey area doesn't follow the {state}{fips3} pattern
# (multi-county surveys). Discovered against sacatalog 2026-07.
AREA_OVERRIDES = {
    "51095": "VA695",  # James City -> James City/York/Williamsburg
    "51099": "VA179",  # King George -> Stafford and King George
    "54027": "WV608",  # Hampshire -> Hampshire and Mineral
    "54031": "WV628",  # Hardy -> Grant and Hardy
}

STAGING_COLS = {
    "mukey":          "text",
    "areasymbol":     "text",
    "muname":         "text",
    "septic_rating":  "text",
    "drainage_class": "text",
    "hydro_group":    "text",
    "geom_wkb":       "bytea",
}

MERGE_SQL_TEMPLATE = """
    DELETE FROM soil_units WHERE areasymbol = '{area}';
    INSERT INTO soil_units (mukey, areasymbol, muname, septic_rating,
                            drainage_class, hydro_group, geom)
    SELECT mukey, areasymbol, muname, septic_rating,
           drainage_class, hydro_group,
           ST_CollectionExtract(ST_MakeValid(
               ST_SetSRID(ST_GeomFromWKB(geom_wkb), 4326)), 3)
    FROM _staging
    WHERE NOT ST_IsEmpty(ST_CollectionExtract(ST_MakeValid(
        ST_SetSRID(ST_GeomFromWKB(geom_wkb), 4326)), 3));
"""

# Area-weighted septic suitability per candidate parcel:
#   pct_septic_ok  — % of parcel area on 'Not limited'/'Somewhat limited'
#   septic_dominant — areally dominant rating class
METRICS_SQL = """
    WITH soil_overlap AS (
        SELECT cp.id,
               su.septic_rating,
               ST_Area(ST_Intersection(cp.geom, su.geom)::geography) AS a
        FROM candidate_parcels cp
        JOIN soil_units su ON ST_Intersects(cp.geom, su.geom)
    ),
    agg AS (
        SELECT id,
               SUM(a) AS a_total,
               SUM(a) FILTER (WHERE septic_rating IN
                   ('Not limited', 'Somewhat limited')) AS a_ok
        FROM soil_overlap GROUP BY id
    ),
    dominant AS (
        SELECT DISTINCT ON (id) id, septic_rating
        FROM (SELECT id, septic_rating, SUM(a) AS a
              FROM soil_overlap GROUP BY id, septic_rating) x
        ORDER BY id, a DESC
    )
    INSERT INTO parcel_metrics (parcel_id, pct_septic_ok, septic_dominant,
                                septic_computed_at)
    SELECT agg.id,
           LEAST(100, ROUND((100.0 * COALESCE(agg.a_ok, 0)
                             / NULLIF(agg.a_total, 0))::numeric, 2)),
           dominant.septic_rating,
           now()
    FROM agg JOIN dominant USING (id)
    ON CONFLICT (parcel_id) DO UPDATE SET
        pct_septic_ok      = EXCLUDED.pct_septic_ok,
        septic_dominant    = EXCLUDED.septic_dominant,
        septic_computed_at = EXCLUDED.septic_computed_at;
"""


def sda_query(sql: str) -> list[list]:
    r = SESSION.post(SDA_URL, json={"query": sql, "format": "JSON+COLUMNNAME"},
                     timeout=180)
    r.raise_for_status()
    table = r.json().get("Table", [])
    return table[1:]  # drop header row


def get_survey_areas(county_fips: list[str]) -> list[tuple[str, str, str]]:
    """(county_fips, areasymbol, name) for relevant counties."""
    where = "cabin_relevant"
    params = {}
    if county_fips:
        where += " AND county_fips = ANY(:fips)"
        params["fips"] = county_fips
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT county_fips, state_abbr, name FROM counties_in_scope "
            f"WHERE {where} ORDER BY county_fips"), params).all()
    return [(f, AREA_OVERRIDES.get(f, f"{st}{f[2:]}"), n)
            for f, st, n in rows]


def get_ratings(area: str) -> dict[str, tuple]:
    """mukey -> (muname, septic, drainage, hydro) for one survey area."""
    rows = sda_query(f"""
        SELECT M.mukey, M.muname, A.drclassdcd, A.hydgrpdcd,
               (SELECT TOP 1 CI.interphrc
                FROM component C
                JOIN cointerp CI ON CI.cokey = C.cokey
                WHERE C.mukey = M.mukey
                  AND CI.mrulename = 'ENG - Septic Tank Absorption Fields'
                  AND CI.ruledepth = 0
                ORDER BY C.comppct_r DESC) AS septic
        FROM mapunit M
        JOIN legend L ON L.lkey = M.lkey
        LEFT JOIN muaggatt A ON A.mukey = M.mukey
        WHERE L.areasymbol = '{area}'
    """)
    return {r[0]: (r[1], r[4], r[2], r[3]) for r in rows}


def fetch_zip(area: str, saverest: str) -> Path:
    path = CACHE / f"{area}.zip"
    if path.exists() and path.stat().st_size > 0:
        return path
    url = CACHE_URL.format(area=area, date=saverest)
    r = SESSION.get(url, timeout=600)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def load_area(fips: str, area: str, name: str, saverest: str) -> int:
    ratings = get_ratings(area)
    zpath = fetch_zip(area, saverest)
    shp_inner = f"{area}/spatial/soilmu_a_{area.lower()}.shp"
    with zipfile.ZipFile(zpath) as z:
        # locate the shapefile regardless of top-level folder naming
        cands = [n for n in z.namelist()
                 if n.lower().endswith(f"soilmu_a_{area.lower()}.shp")]
        if not cands:
            raise RuntimeError(f"no soilmu_a shapefile in {zpath.name}")
        shp_inner = cands[0]
    gdf = pyogrio.read_dataframe(f"/vsizip/{zpath.as_posix()}/{shp_inner}")
    rows = []
    for mukey, geom in zip(gdf["MUKEY"], gdf.geometry):
        muname, septic, drainage, hydro = ratings.get(
            str(mukey), (None, None, None, None))
        rows.append((str(mukey), area, muname, septic, drainage, hydro,
                     to_wkb(geom)))
    n = bulk_load(STAGING_COLS, rows, MERGE_SQL_TEMPLATE.format(area=area))
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO layer_loads (layer, scope, feature_count, notes)
            VALUES ('soils_ssurgo', :scope, :n, :notes)
            ON CONFLICT (layer, scope) DO UPDATE SET
                loaded_at = now(),
                feature_count = EXCLUDED.feature_count,
                notes = EXCLUDED.notes
        """), {"scope": fips, "n": n, "notes": f"{area} saverest {saverest}"})
    return n


def main() -> None:
    county_fips = [a for a in sys.argv[1:] if a[:2] in ("51", "54")]
    CACHE.mkdir(parents=True, exist_ok=True)
    targets = get_survey_areas(county_fips)
    symbols = ",".join(f"'{a}'" for _, a, _ in targets)
    dates = dict(sda_query(
        f"SELECT areasymbol, CONVERT(varchar(10), saverest, 126) "
        f"FROM sacatalog WHERE areasymbol IN ({symbols})"))
    print(f"{len(targets)} counties; {len(dates)} matching survey areas\n")

    total = 0
    failures = []
    loaded: dict[str, int] = {}   # multi-county areas load once
    t0 = time.time()
    for fips, area, name in targets:
        if area not in dates:
            print(f"[{area}] {name}: NO SURVEY AREA — skipped")
            failures.append(area)
            continue
        if area in loaded:
            print(f"[{area}] {name}: already loaded ({loaded[area]:,} polygons)")
            continue
        try:
            n = load_area(fips, area, name, dates[area])
            loaded[area] = n
            total += n
            print(f"[{area}] {name}: {n:,} polygons ({time.time()-t0:.0f}s)")
        except Exception as exc:
            failures.append(area)
            print(f"[{area}] {name} FAILED: {exc}")

    print(f"\nDone. {total:,} soil polygons loaded.")
    if failures:
        print(f"Failed/missing areas: {', '.join(failures)}")
    print("Now run: python manage.py metrics septic")


if __name__ == "__main__":
    main()
