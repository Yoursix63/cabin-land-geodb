"""
Load FEMA NFHL flood hazard zones (SFHA subset) for the AOI and compute
per-parcel flood metrics.

Source: ESRI Living Atlas mirror of FEMA NFHL (hazards.fema.gov was
refusing connections at build time; the Living Atlas layer carries the
full NFHL schema).

Strategy: query per cabin-relevant county bbox for SFHA polygons
(SFHA_TF='T') plus 'AREA NOT INCLUDED' (unmapped areas — absence of a
flood polygon there means "not assessed", not "clear"). Features are
deduped on FLD_AR_ID across overlapping bboxes, bulk-loaded via COPY
staging, and the flood_zones table is fully replaced. Finally,
parcel_metrics.sfha_pct is recomputed for all candidate parcels.

Run:
    python -m ingest.flood_nfhl
"""
from __future__ import annotations

import json
import time

from sqlalchemy import text

from .db import get_engine
from .http import make_session
from .staging import bulk_load

URL = (
    "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
    "USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0/query"
)
PAGE_SIZE = 750
WHERE = "SFHA_TF='T' OR FLD_ZONE='AREA NOT INCLUDED'"

SESSION = make_session()

STAGING_COLS = {
    "fld_ar_id":  "text",
    "dfirm_id":   "text",
    "fld_zone":   "text",
    "zone_subty": "text",
    "sfha":       "boolean",
    "static_bfe": "numeric",
    "attrs":      "text",
    "geom_json":  "text",
}

MERGE_SQL = """
    TRUNCATE flood_zones;
    INSERT INTO flood_zones (fld_ar_id, dfirm_id, fld_zone, zone_subty,
                             sfha, static_bfe, source_attrs, geom)
    SELECT DISTINCT ON (s.fld_ar_id)
        s.fld_ar_id, s.dfirm_id, s.fld_zone, s.zone_subty,
        s.sfha, s.static_bfe, s.attrs::jsonb,
        ST_CollectionExtract(
            ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(s.geom_json), 4326)), 3)
    FROM _staging s
    WHERE NOT ST_IsEmpty(ST_CollectionExtract(
        ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(s.geom_json), 4326)), 3));
"""

# Recompute flood metrics for every candidate parcel: % of parcel area
# intersecting SFHA polygons (capped at 100 — adjacent DFIRM versions can
# overlap slightly), plus the distinct zone codes touched.
METRICS_SQL = """
    INSERT INTO parcel_metrics (parcel_id, sfha_pct, sfha_zones, flood_computed_at)
    SELECT
        cp.id,
        LEAST(100, ROUND((
            100.0 * SUM(ST_Area(ST_Intersection(cp.geom, fz.geom)::geography))
            / NULLIF(ST_Area(cp.geom::geography), 0)
        )::numeric, 2)),
        ARRAY_AGG(DISTINCT fz.fld_zone ORDER BY fz.fld_zone),
        now()
    FROM candidate_parcels cp
    JOIN flood_zones fz
      ON fz.sfha AND ST_Intersects(cp.geom, fz.geom)
    GROUP BY cp.id, cp.geom
    ON CONFLICT (parcel_id) DO UPDATE SET
        sfha_pct          = EXCLUDED.sfha_pct,
        sfha_zones        = EXCLUDED.sfha_zones,
        flood_computed_at = EXCLUDED.flood_computed_at;

    -- Zero-fill candidates that touch no SFHA polygon. ON CONFLICT
    -- required: another metric may have created the row already
    -- (bit us when slope ran before a flood delta pass).
    INSERT INTO parcel_metrics (parcel_id, sfha_pct, sfha_zones, flood_computed_at)
    SELECT cp.id, 0, '{}', now()
    FROM candidate_parcels cp
    WHERE NOT EXISTS (
        SELECT 1 FROM parcel_metrics pm
        WHERE pm.parcel_id = cp.id AND pm.flood_computed_at IS NOT NULL
    )
    ON CONFLICT (parcel_id) DO UPDATE SET
        sfha_pct          = EXCLUDED.sfha_pct,
        sfha_zones        = EXCLUDED.sfha_zones,
        flood_computed_at = EXCLUDED.flood_computed_at;
"""


def fetch_bbox(bbox: str) -> list[dict]:
    """All matching features intersecting one bbox, paginated."""
    features: list[dict] = []
    offset = 0
    while True:
        r = SESSION.get(URL, params={
            "where": WHERE,
            "geometry": bbox,
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": ",".join([
                "FLD_AR_ID", "DFIRM_ID", "FLD_ZONE", "ZONE_SUBTY",
                "SFHA_TF", "STATIC_BFE", "STUDY_TYP", "GFID",
            ]),
            "outSR": "4326",
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
        }, timeout=180)
        r.raise_for_status()
        page = r.json().get("features", [])
        features.extend(page)
        if len(page) < PAGE_SIZE:
            return features
        offset += PAGE_SIZE
        time.sleep(0.05)


def feature_to_row(feat: dict) -> tuple | None:
    if feat.get("geometry") is None:
        return None
    p = feat.get("properties") or {}
    fld_ar_id = p.get("FLD_AR_ID") or p.get("GFID")
    if not fld_ar_id:
        return None
    bfe = p.get("STATIC_BFE")
    return (
        str(fld_ar_id),
        p.get("DFIRM_ID"),
        p.get("FLD_ZONE") or "UNKNOWN",
        p.get("ZONE_SUBTY"),
        p.get("SFHA_TF") == "T",
        None if bfe in (None, -9999) else bfe,
        json.dumps({"study_typ": p.get("STUDY_TYP"), "gfid": p.get("GFID")}),
        json.dumps(feat["geometry"]),
    )


def main() -> None:
    engine = get_engine()
    with engine.connect() as conn:
        counties = conn.execute(text("""
            SELECT county_fips, name,
                   ST_XMin(geom)||','||ST_YMin(geom)||','||
                   ST_XMax(geom)||','||ST_YMax(geom)
            FROM counties_in_scope WHERE cabin_relevant
            ORDER BY state_abbr, name
        """)).all()

    print(f"Fetching NFHL SFHA polygons for {len(counties)} county bboxes")
    by_id: dict[str, tuple] = {}
    per_county: list[tuple[str, str, int]] = []
    for fips, name, bbox in counties:
        feats = fetch_bbox(bbox)
        rows = [r for r in (feature_to_row(f) for f in feats) if r]
        for row in rows:
            by_id[row[0]] = row
        per_county.append((fips, name, len(rows)))
        print(f"  [{fips}] {name}: {len(rows)} features "
              f"({len(by_id)} unique so far)")

    print(f"\nBulk-loading {len(by_id):,} unique flood polygons ...")
    copied = bulk_load(STAGING_COLS, by_id.values(), MERGE_SQL)
    print(f"  staged {copied:,} rows")

    with engine.begin() as conn:
        for fips, _name, n in per_county:
            conn.execute(text("""
                INSERT INTO layer_loads (layer, scope, feature_count)
                VALUES ('flood_nfhl', :scope, :n)
                ON CONFLICT (layer, scope) DO UPDATE SET
                    loaded_at = now(), feature_count = EXCLUDED.feature_count
            """), {"scope": fips, "n": n})

    print("Computing per-parcel flood metrics (candidate set) ...")
    t0 = time.time()
    import psycopg

    from .db import get_conninfo
    with psycopg.connect(get_conninfo()) as pg:
        pg.execute(METRICS_SQL)
        pg.commit()
    print(f"  done in {time.time() - t0:.0f}s")

    with engine.connect() as conn:
        n_zones, = conn.execute(text("SELECT COUNT(*) FROM flood_zones")).one()
        touched, total = conn.execute(text(
            "SELECT COUNT(*) FILTER (WHERE sfha_pct > 0), COUNT(*) "
            "FROM parcel_metrics")).one()
    print(f"\nflood_zones: {n_zones:,} polygons")
    print(f"parcel_metrics: {total:,} candidates, "
          f"{touched:,} touch an SFHA zone")


if __name__ == "__main__":
    main()
