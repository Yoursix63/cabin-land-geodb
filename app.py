"""
Browser UI for the cabin-land geodatabase.

    python app.py          # http://localhost:5001

Port 5001 (5000 belongs to the doctrine_to_h3 app). No reloader —
stacked stale instances serve old code.
"""
from __future__ import annotations

import json

from flask import Flask, jsonify, request, send_from_directory
from sqlalchemy import text

from ingest.db import get_engine

app = Flask(__name__, static_folder="static", static_url_path="")
engine = get_engine()

MAX_BBOX_DEG = 0.45          # server-side guard: force county view when wide
PARCEL_LIMIT = 1500

_county_cache: dict | None = None


FILTER_SQL = {
    "min_score":  "ps.score >= :min_score",
    "min_acres":  "ps.acres >= :min_acres",
    "max_acres":  "ps.acres <= :max_acres",
    "max_drive":  "ps.drive_minutes <= :max_drive",
    "max_slope":  "ps.slope_mean <= :max_slope",
    "min_septic": "ps.pct_septic_ok >= :min_septic",
    "state":      "ps.state_abbr = :state",
    "county":     "ps.county_name ILIKE :county",
}

LISTING_ACTIVE = ("l.listing_kind <> 'tax_sale' "
                  "OR l.status IN ('No Bid', 'Deed', 'Suspended')")


def parse_filters(args) -> tuple[list[str], dict]:
    conds, params = [], {}
    for key, sql in FILTER_SQL.items():
        val = args.get(key)
        if val not in (None, ""):
            conds.append(sql)
            params[key] = f"%{val}%" if key == "county" else val
    if args.get("dry_only") in ("1", "true"):
        conds.append("ps.sfha_pct = 0")
    if args.get("for_sale") in ("1", "true"):
        conds.append(f"""EXISTS (
            SELECT 1 FROM listings l
            WHERE l.parcel_id = ps.id AND ({LISTING_ACTIVE}))""")
    return conds, params


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/counties")
def counties():
    global _county_cache
    if _county_cache is None:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT c.county_fips, c.name, c.state_abbr,
                       c.drive_minutes, c.cabin_relevant,
                       COALESCE(s.n, 0) AS candidates,
                       s.avg_score, s.max_score,
                       COALESCE(s.forsale, 0) AS forsale,
                       ST_AsGeoJSON(ST_SimplifyPreserveTopology(c.geom, 0.004), 5) AS gj
                FROM counties_in_scope c
                LEFT JOIN (
                    SELECT ps.county_fips, COUNT(*) AS n,
                           ROUND(AVG(ps.score), 1) AS avg_score,
                           MAX(ps.score) AS max_score,
                           COUNT(*) FILTER (WHERE EXISTS (
                               SELECT 1 FROM listings l
                               WHERE l.parcel_id = ps.id
                                 AND (l.listing_kind <> 'tax_sale'
                                      OR l.status IN ('No Bid','Deed','Suspended'))
                           )) AS forsale
                    FROM parcel_scores ps GROUP BY ps.county_fips
                ) s USING (county_fips)
            """)).mappings().all()
        _county_cache = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": json.loads(r["gj"]),
                "properties": {
                    "fips": r["county_fips"], "name": r["name"],
                    "state": r["state_abbr"],
                    "drive": float(r["drive_minutes"] or 0),
                    "relevant": r["cabin_relevant"],
                    "candidates": r["candidates"],
                    "avg_score": float(r["avg_score"] or 0),
                    "max_score": float(r["max_score"] or 0),
                    "forsale": r["forsale"],
                },
            } for r in rows],
        }
    return jsonify(_county_cache)


@app.get("/api/parcels")
def parcels():
    try:
        w, s, e, n = (float(x) for x in request.args["bbox"].split(","))
    except (KeyError, ValueError):
        return jsonify({"error": "bbox=w,s,e,n required"}), 400
    if (e - w) > MAX_BBOX_DEG or (n - s) > MAX_BBOX_DEG:
        return jsonify({"error": "zoom in", "kind": "zoom"}), 400
    conds, params = parse_filters(request.args)
    params |= {"w": w, "s": s, "e": e, "n": n, "lim": PARCEL_LIMIT}
    where = (" AND " + " AND ".join(conds)) if conds else ""
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT ps.id, ps.score, ps.acres, ps.county_name, ps.state_abbr,
                   ps.parcel_local_id, ps.slope_mean, ps.pct_septic_ok,
                   ps.sfha_pct, ps.road_dist_m,
                   l.listing_kind || '/' || COALESCE(l.status, '?') AS listing,
                   ST_AsGeoJSON(ST_SimplifyPreserveTopology(cp.geom, 0.00005), 5) AS gj
            FROM parcel_scores ps
            JOIN candidate_parcels cp ON cp.id = ps.id
            LEFT JOIN listings l ON l.parcel_id = ps.id AND ({LISTING_ACTIVE})
            WHERE cp.geom && ST_MakeEnvelope(:w, :s, :e, :n, 4326)
            {where}
            ORDER BY ps.score DESC
            LIMIT :lim
        """), params).mappings().all()
    feats = [{
        "type": "Feature",
        "geometry": json.loads(r["gj"]),
        "properties": {k: (float(v) if hasattr(v, "quantize") else v)
                       for k, v in r.items() if k != "gj"},
    } for r in rows]
    return jsonify({"type": "FeatureCollection", "features": feats,
                    "truncated": len(feats) >= PARCEL_LIMIT})


@app.get("/api/shortlist")
def shortlist():
    conds, params = parse_filters(request.args)
    params["lim"] = min(int(request.args.get("limit", 50)), 200)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT ps.id, ps.score, ps.acres, ps.county_name, ps.state_abbr,
                   ps.parcel_local_id, ps.drive_minutes,
                   ST_X(ST_Centroid(cp.geom)) AS lon,
                   ST_Y(ST_Centroid(cp.geom)) AS lat
            FROM parcel_scores ps
            JOIN candidate_parcels cp ON cp.id = ps.id
            {where}
            ORDER BY ps.score DESC LIMIT :lim
        """), params).mappings().all()
    return jsonify([{k: (float(v) if hasattr(v, "quantize") else v)
                     for k, v in r.items()} for r in rows])


@app.get("/api/parcel/<int:pid>")
def parcel_detail(pid: int):
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT ps.*, cp.owner_name AS owner, cp.situs_address AS address,
                   ST_AsGeoJSON(cp.geom, 6) AS gj,
                   ST_X(ST_Centroid(cp.geom)) AS lon,
                   ST_Y(ST_Centroid(cp.geom)) AS lat
            FROM parcel_scores ps
            JOIN candidate_parcels cp ON cp.id = ps.id
            WHERE ps.id = :pid
        """), {"pid": pid}).mappings().first()
        if r is None:
            return jsonify({"error": "not found"}), 404
        listings = conn.execute(text("""
            SELECT source, listing_kind, status, price, acres, title,
                   address, url, fetched_at::date::text AS fetched
            FROM listings WHERE parcel_id = :pid
        """), {"pid": pid}).mappings().all()
    out = {k: (float(v) if hasattr(v, "quantize") else v)
           for k, v in r.items() if k != "gj"}
    out["geometry"] = json.loads(r["gj"])
    out["listings"] = [dict(l) for l in listings]
    return jsonify(out)


@app.get("/api/weights")
def get_weights():
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT component, weight, rationale FROM scoring_weights "
            "ORDER BY weight DESC")).mappings().all()
    return jsonify([{"component": r["component"], "weight": float(r["weight"]),
                     "rationale": r["rationale"]} for r in rows])


@app.post("/api/weights")
def set_weights():
    global _county_cache
    body = request.get_json(silent=True) or {}
    updates = {k: float(v) for k, v in body.items()
               if k in ("flood", "slope", "septic", "size", "drive", "seclusion")}
    if not updates:
        return jsonify({"error": "no valid components"}), 400
    with engine.begin() as conn:
        for comp, w in updates.items():
            conn.execute(text(
                "UPDATE scoring_weights SET weight = :w "
                "WHERE component = :c"), {"w": w, "c": comp})
    _county_cache = None   # scores changed; choropleth stats are stale
    return jsonify({"ok": True, "updated": updates})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)
