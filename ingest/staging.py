"""
COPY-based bulk loading: stream rows into an UNLOGGED staging table,
then merge with one set-based statement. 10-30x faster than row-wise
executemany upserts for large feature sets.

Usage:
    n = bulk_load(
        staging_cols={"fld_ar_id": "text", "geom_json": "text", ...},
        rows=iter_of_tuples_in_col_order,
        merge_sql=\"\"\"
            INSERT INTO target (...)
            SELECT ... FROM _staging ...
        \"\"\",
    )

merge_sql may contain multiple statements; it runs in the same
transaction as the COPY, and the staging table is dropped afterwards.
"""
from __future__ import annotations

from typing import Iterable, Sequence

import psycopg

from .db import get_conninfo

STAGING_TABLE = "_staging"


def bulk_load(
    staging_cols: dict[str, str],
    rows: Iterable[Sequence],
    merge_sql: str,
    staging_table: str = STAGING_TABLE,
) -> int:
    """COPY rows into staging, run merge_sql, drop staging. Returns rows copied."""
    col_names = ", ".join(staging_cols)
    col_ddl = ", ".join(f"{name} {typ}" for name, typ in staging_cols.items())
    copied = 0
    with psycopg.connect(get_conninfo()) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {staging_table}")
        conn.execute(f"CREATE UNLOGGED TABLE {staging_table} ({col_ddl})")
        with conn.cursor() as cur:
            with cur.copy(
                f"COPY {staging_table} ({col_names}) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(row)
                    copied += 1
        conn.execute(merge_sql)
        conn.execute(f"DROP TABLE {staging_table}")
        conn.commit()
    return copied
