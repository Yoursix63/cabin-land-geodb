"""
Slope fabric from USGS 3DEP: fetch slope-degree tiles from the 3DEP
ImageServer, bin 10 m pixels onto H3 res-10 cells, aggregate, and load
hex_slope. Then per-parcel slope metrics via manage.py `metrics slope`.

CRS note (hard-won): the ImageServer's "Slope Degrees" rendering rule is
only correct when the OUTPUT is in a projected CRS. Requesting
imageSR=4326 yields ~90 deg everywhere (rise in meters over run in
degrees). We fetch in EPSG:5070 (CONUS Albers, meters) and transform
pixel centers to WGS84 locally for H3 binning.

Tiles are cached in data/raw/slope/ (gitignored); delete to force
refetch.

Usage:
    python -m ingest.slope_3dep            # full cabin-relevant AOI
    python -m ingest.slope_3dep 54031      # only tiles touching one county
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from h3ronpy.vector import coordinates_to_cells
from pyproj import Transformer
from shapely import prepared, unary_union, wkb
from sqlalchemy import text

from .db import get_engine
from .http import make_session
from .staging import bulk_load

URL = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
       "3DEPElevation/ImageServer/exportImage")

RES = 10                 # H3 resolution of the fabric
PIXEL_M = 10.0           # requested pixel size, meters
TILE_PX = 2048           # pixels per tile edge
TILE_M = TILE_PX * PIXEL_M
STEEP_DEG = 15.0
NODATA_MIN = -1.0        # slope must be >= 0; server nodata is large-negative

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "raw" / "slope"

SESSION = make_session()
TO_ALBERS = Transformer.from_crs(4326, 5070, always_xy=True)
TO_WGS84 = Transformer.from_crs(5070, 4326, always_xy=True)

STAGING_COLS = {
    "h3":         "text",
    "px_count":   "integer",
    "slope_mean": "numeric",
    "slope_p90":  "numeric",
    "pct_gt15":   "numeric",
}

# Full-AOI runs replace the table; county-scoped runs upsert so they
# don't wipe cells outside their area.
MERGE_SQL_REPLACE = """
    TRUNCATE hex_slope;
    INSERT INTO hex_slope (h3, px_count, slope_mean, slope_p90, pct_gt15)
    SELECT h3::h3index, px_count, slope_mean, slope_p90, pct_gt15
    FROM _staging;
"""

MERGE_SQL_UPSERT = """
    INSERT INTO hex_slope (h3, px_count, slope_mean, slope_p90, pct_gt15)
    SELECT h3::h3index, px_count, slope_mean, slope_p90, pct_gt15
    FROM _staging
    ON CONFLICT (h3) DO UPDATE SET
        px_count    = EXCLUDED.px_count,
        slope_mean  = EXCLUDED.slope_mean,
        slope_p90   = EXCLUDED.slope_p90,
        pct_gt15    = EXCLUDED.pct_gt15,
        computed_at = now();
"""

# Per-parcel slope metrics via the precomputed parcel_cells mapping
# (built by ingest/parcel_cells.py — doing the polygon->cells conversion
# in SQL was a 6-hour runaway; see docs/DECISIONS.md). px_count-weighted
# mean; p90 is the max over member cells (screening should not
# understate slope).
METRICS_SQL = """
    INSERT INTO parcel_metrics (parcel_id, slope_mean, slope_p90,
                                pct_steep, slope_computed_at)
    SELECT
        pc.parcel_id,
        ROUND((SUM(hs.slope_mean * hs.px_count)
               / NULLIF(SUM(hs.px_count), 0))::numeric, 2),
        MAX(hs.slope_p90),
        ROUND((SUM(hs.pct_gt15 * hs.px_count)
               / NULLIF(SUM(hs.px_count), 0))::numeric, 2),
        now()
    FROM parcel_cells pc
    JOIN hex_slope hs ON hs.h3 = pc.cell
    GROUP BY pc.parcel_id
    ON CONFLICT (parcel_id) DO UPDATE SET
        slope_mean        = EXCLUDED.slope_mean,
        slope_p90         = EXCLUDED.slope_p90,
        pct_steep         = EXCLUDED.pct_steep,
        slope_computed_at = EXCLUDED.slope_computed_at;
"""


def load_aoi(county_fips: list[str]):
    """Union of cabin-relevant county geometries (WGS84 shapely)."""
    where = "cabin_relevant"
    params = {}
    if county_fips:
        where += " AND county_fips = ANY(:fips)"
        params["fips"] = county_fips
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT ST_AsBinary(geom) FROM counties_in_scope WHERE {where}"
        ), params).all()
    return unary_union([wkb.loads(bytes(r[0])) for r in rows])


def tile_grid(aoi):
    """Yield (i, j, x0, y0) Albers tile origins intersecting the AOI."""
    prep = prepared.prep(aoi)
    minx, miny, maxx, maxy = aoi.bounds
    ax0, ay0 = TO_ALBERS.transform(minx, miny)
    ax1, ay1 = TO_ALBERS.transform(maxx, maxy)
    # Albers of a lat/lng box isn't axis-aligned; pad one tile all around.
    ax0, ay0 = ax0 - TILE_M, ay0 - TILE_M
    ax1, ay1 = ax1 + TILE_M, ay1 + TILE_M
    from shapely.geometry import box
    ni = int((ax1 - ax0) // TILE_M) + 1
    nj = int((ay1 - ay0) // TILE_M) + 1
    for i in range(ni):
        for j in range(nj):
            x0, y0 = ax0 + i * TILE_M, ay0 + j * TILE_M
            # tile corners back to WGS84 for the AOI test
            xs = [x0, x0 + TILE_M, x0 + TILE_M, x0]
            ys = [y0, y0, y0 + TILE_M, y0 + TILE_M]
            lons, lats = TO_WGS84.transform(xs, ys)
            env = box(min(lons), min(lats), max(lons), max(lats))
            if prep.intersects(env):
                yield i, j, x0, y0


def fetch_tile(i: int, j: int, x0: float, y0: float) -> Path | None:
    """Download one slope tile (cached). Returns path or None on failure."""
    path = CACHE / f"slope_5070_{i}_{j}.tif"
    if path.exists() and path.stat().st_size > 0:
        return path
    r = SESSION.get(URL, params={
        "bbox": f"{x0},{y0},{x0 + TILE_M},{y0 + TILE_M}",
        "bboxSR": "5070",
        "imageSR": "5070",
        "size": f"{TILE_PX},{TILE_PX}",
        "format": "tiff",
        "pixelType": "F32",
        "renderingRule": '{"rasterFunction":"Slope Degrees"}',
        "f": "image",
    }, timeout=300)
    r.raise_for_status()
    if not r.content.startswith((b"II", b"MM")):     # error JSON, not TIFF
        raise RuntimeError(f"non-TIFF response ({r.content[:120]!r})")
    path.write_bytes(r.content)
    return path


def tile_to_cells(path: Path) -> pd.DataFrame | None:
    """Bin one tile's pixels to res-10 cells: partial aggregates."""
    with rasterio.open(path) as src:
        a = src.read(1, masked=True)
        transform = src.transform
    valid = ~a.mask if a.mask is not np.False_ else np.ones(a.shape, bool)
    valid &= (a.data >= NODATA_MIN) & (a.data <= 90.0)
    if not valid.any():
        return None
    rows, cols = np.nonzero(valid)
    slopes = a.data[rows, cols].astype("float64")
    xs, ys = rasterio.transform.xy(transform, rows, cols)
    lons, lats = TO_WGS84.transform(np.asarray(xs), np.asarray(ys))
    cells = np.asarray(coordinates_to_cells(lats, lons, RES))
    df = pd.DataFrame({"cell": cells, "slope": slopes})
    g = df.groupby("cell")["slope"]
    out = g.agg(
        px_count="count",
        slope_sum="sum",
        slope_p90=lambda s: s.quantile(0.9),
    )
    out["steep_count"] = df.assign(st=df["slope"] > STEEP_DEG) \
                           .groupby("cell")["st"].sum()
    return out.reset_index()


def main() -> None:
    county_fips = [a for a in sys.argv[1:] if a[:2] in ("51", "54")]
    CACHE.mkdir(parents=True, exist_ok=True)

    print("Building AOI ...")
    aoi = load_aoi(county_fips)
    tiles = list(tile_grid(aoi))
    print(f"{len(tiles)} tiles of {TILE_PX}px @ {PIXEL_M:.0f}m cover the AOI")

    partials: list[pd.DataFrame] = []
    failed = 0
    t0 = time.time()
    for n, (i, j, x0, y0) in enumerate(tiles, 1):
        try:
            path = fetch_tile(i, j, x0, y0)
            part = tile_to_cells(path)
        except Exception as exc:
            failed += 1
            print(f"  tile {i},{j} FAILED: {exc}")
            continue
        if part is not None:
            partials.append(part)
        if n % 10 == 0 or n == len(tiles):
            print(f"  {n}/{len(tiles)} tiles, "
                  f"{sum(len(p) for p in partials):,} partial rows, "
                  f"{time.time() - t0:.0f}s")

    print("Merging partials ...")
    allp = pd.concat(partials, ignore_index=True)
    final = allp.groupby("cell").agg(
        px_count=("px_count", "sum"),
        slope_sum=("slope_sum", "sum"),
        slope_p90=("slope_p90", "max"),
        steep_count=("steep_count", "sum"),
    )
    final["slope_mean"] = final["slope_sum"] / final["px_count"]
    final["pct_gt15"] = 100.0 * final["steep_count"] / final["px_count"]

    print(f"Loading {len(final):,} hex cells into hex_slope ...")
    rows = (
        (f"{cell:x}", int(r.px_count), round(float(r.slope_mean), 2),
         round(float(r.slope_p90), 2), round(float(r.pct_gt15), 2))
        for cell, r in final.iterrows()
    )
    merge = MERGE_SQL_UPSERT if county_fips else MERGE_SQL_REPLACE
    copied = bulk_load(STAGING_COLS, rows, merge)
    print(f"  staged {copied:,} rows")

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO layer_loads (layer, scope, feature_count, notes)
            VALUES ('slope_3dep', :scope, :n, :notes)
            ON CONFLICT (layer, scope) DO UPDATE SET
                loaded_at = now(),
                feature_count = EXCLUDED.feature_count,
                notes = EXCLUDED.notes
        """), {
            "scope": ",".join(county_fips) if county_fips else "aoi",
            "n": len(final),
            "notes": f"{len(tiles)} tiles, {failed} failed",
        })

    if failed:
        print(f"WARNING: {failed} tiles failed — rerun to fill gaps (cached "
              f"tiles are skipped).")
    print("Done. Now run: python manage.py metrics slope")


if __name__ == "__main__":
    main()
