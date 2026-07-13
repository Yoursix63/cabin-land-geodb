"""
Build the parcel_cells table: candidate parcel -> covering H3 res-10
cells, computed vectorized in Rust via h3ronpy (SQL h3_polygon_to_cells
was ~50ms/parcel single-backend; this does 385K parcels in minutes).

Containment mode "Covers": every cell whose hexagon overlaps the parcel,
so even sub-cell parcels map to at least one cell.

Run:
    python -m ingest.parcel_cells
"""
from __future__ import annotations

import time

import numpy as np
import pyarrow as pa
from h3ronpy import ContainmentMode
from h3ronpy.vector import wkb_to_cells
from sqlalchemy import text

from .db import get_engine
from .staging import bulk_load
from .slope_3dep import RES

BATCH = 20_000

STAGING_COLS = {"parcel_id": "bigint", "cell_hex": "text"}

MERGE_SQL = """
    TRUNCATE parcel_cells;
    INSERT INTO parcel_cells (parcel_id, cell)
    SELECT DISTINCT parcel_id, cell_hex::h3index
    FROM _staging;
"""


def iter_rows():
    engine = get_engine()
    with engine.connect() as conn:
        n_total, = conn.execute(
            text("SELECT COUNT(*) FROM candidate_parcels")).one()
        print(f"Mapping {n_total:,} candidate parcels to res-{RES} cells")
        result = conn.execution_options(yield_per=BATCH).execute(text(
            "SELECT id, ST_AsBinary(geom) FROM candidate_parcels"))
        done = 0
        t0 = time.time()
        for chunk in result.partitions():
            ids = np.array([r[0] for r in chunk], dtype="int64")
            wkbs = pa.array([bytes(r[1]) for r in chunk])
            cells = wkb_to_cells(
                wkbs, RES,
                containment_mode=ContainmentMode.Covers,
                flatten=False,
            )
            for pid, cell_list in zip(ids, cells):
                lst = cell_list.as_py() if cell_list is not None else None
                if lst:
                    for c in lst:
                        yield int(pid), f"{c:x}"
            done += len(ids)
            if done % 100_000 < BATCH:
                print(f"  {done:,}/{n_total:,} parcels, "
                      f"{time.time() - t0:.0f}s")


def main() -> None:
    t0 = time.time()
    copied = bulk_load(STAGING_COLS, iter_rows(), MERGE_SQL)
    print(f"parcel_cells built: {copied:,} mappings "
          f"in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
