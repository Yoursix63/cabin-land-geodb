"""
Project CLI for the cabin-land geodatabase.

    python manage.py migrate              apply pending sql/NNN_*.sql migrations
    python manage.py status               row counts + source freshness
    python manage.py verify               data sanity checks
    python manage.py refresh-candidates   rebuild the candidate_parcels matview
    python manage.py load counties        rebuild + load county scope
    python manage.py load wv              load WV parcels
    python manage.py load va [FIPS...]    load VA parcels (optionally specific counties)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import click
from sqlalchemy import text

from ingest.db import get_engine

ROOT = Path(__file__).resolve().parent
SQL_DIR = ROOT / "sql"

MIGRATIONS_TABLE = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        filename    TEXT PRIMARY KEY,
        applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
"""


@click.group()
def cli() -> None:
    """Cabin-land geodatabase management."""


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------
@cli.command()
@click.option("--fake", is_flag=True,
              help="Record migrations as applied without executing them.")
def migrate(fake: bool) -> None:
    """Apply pending sql/NNN_*.sql migrations in filename order."""
    engine = get_engine()
    files = sorted(p for p in SQL_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    with engine.begin() as conn:
        conn.execute(text(MIGRATIONS_TABLE))
        applied = {r[0] for r in conn.execute(
            text("SELECT filename FROM schema_migrations")).all()}
    pending = [p for p in files if p.name not in applied]
    if not pending:
        click.echo("Up to date.")
        return
    for path in pending:
        if fake:
            click.echo(f"fake-applying {path.name}")
        else:
            click.echo(f"applying {path.name} ...")
            # Raw psycopg: no placeholder parsing, so '%' in SQL comments
            # and operators is safe, unlike SQLAlchemy exec_driver_sql.
            import psycopg

            from ingest.db import get_conninfo
            with psycopg.connect(get_conninfo()) as pg:
                pg.execute(path.read_text(encoding="utf-8"))
                pg.commit()
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO schema_migrations (filename) VALUES (:f) "
                     "ON CONFLICT DO NOTHING"),
                {"f": path.name})
    click.echo(f"{len(pending)} migration(s) {'recorded' if fake else 'applied'}.")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
@cli.command()
def status() -> None:
    """Row counts and per-source freshness."""
    engine = get_engine()
    with engine.connect() as conn:
        total, = conn.execute(text("SELECT COUNT(*) FROM parcels")).one()
        click.echo(f"parcels: {total:,}")
        for st, n_c, n_p in conn.execute(text("""
            SELECT c.state_abbr, COUNT(DISTINCT p.county_fips), COUNT(*)
            FROM parcels p JOIN counties_in_scope c USING (county_fips)
            GROUP BY c.state_abbr ORDER BY c.state_abbr
        """)).all():
            click.echo(f"  {st}: {n_p:,} parcels across {n_c} jurisdictions")
        try:
            cand, = conn.execute(
                text("SELECT COUNT(*) FROM candidate_parcels")).one()
            click.echo(f"candidate_parcels: {cand:,}")
        except Exception:
            click.echo("candidate_parcels: (not built — run migrate)")
        click.echo("\nsource freshness:")
        rows = conn.execute(text("""
            SELECT source_kind, COUNT(*),
                   MIN(last_loaded_at)::date, MAX(last_loaded_at)::date
            FROM parcel_source GROUP BY source_kind ORDER BY source_kind
        """)).all()
        for kind, n, oldest, newest in rows:
            click.echo(f"  {kind}: {n} counties, loaded {oldest} .. {newest}")
        missing = conn.execute(text("""
            SELECT c.name, c.state_abbr FROM counties_in_scope c
            LEFT JOIN parcel_source ps USING (county_fips)
            WHERE ps.county_fips IS NULL ORDER BY c.state_abbr, c.name
        """)).all()
        if missing:
            click.echo("\nno parcels loaded for:")
            for name, st in missing:
                click.echo(f"  {st} {name}")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
CHECKS: list[tuple[str, str, str]] = [
    # (label, sql returning one number, expectation)
    ("invalid geometries",
     "SELECT COUNT(*) FROM parcels WHERE NOT ST_IsValid(geom)", "0"),
    ("empty geometries",
     "SELECT COUNT(*) FROM parcels WHERE ST_IsEmpty(geom)", "0"),
    ("distinct SRIDs",
     "SELECT COUNT(DISTINCT ST_SRID(geom)) FROM parcels", "1"),
    ("parcels with NULL acres",
     "SELECT COUNT(*) FROM parcels WHERE acres IS NULL", "0"),
    ("counties in scope",
     "SELECT COUNT(*) FROM counties_in_scope", "59"),
    ("cabin-relevant counties",
     "SELECT COUNT(*) FROM counties_in_scope WHERE cabin_relevant", "40"),
]


@cli.command()
def verify() -> None:
    """Run data sanity checks; nonzero exit on hard failures."""
    engine = get_engine()
    failures = 0
    with engine.connect() as conn:
        for label, sql, expect in CHECKS:
            got = str(conn.execute(text(sql)).scalar())
            ok = got == expect
            failures += 0 if ok else 1
            click.echo(f"  [{'ok' if ok else '!!'}] {label}: {got}"
                       + ("" if ok else f" (expected {expect})"))
        # Centroid-in-county containment (informational, ~99.8% expected)
        pct = conn.execute(text("""
            SELECT ROUND(100.0 * COUNT(*) FILTER (
                WHERE ST_Within(ST_Centroid(p.geom), c.geom)) / COUNT(*), 2)
            FROM (SELECT * FROM parcels ORDER BY random() LIMIT 20000) p
            JOIN counties_in_scope c USING (county_fips)
        """)).scalar()
        click.echo(f"  [--] centroid containment (20K sample): {pct}%")
    if failures:
        click.echo(f"\n{failures} check(s) failed.")
        sys.exit(1)
    click.echo("\nAll checks passed.")


# ---------------------------------------------------------------------------
# refresh-candidates
# ---------------------------------------------------------------------------
@cli.command("refresh-candidates")
def refresh_candidates() -> None:
    """Rebuild candidate_parcels after scope or parcel changes."""
    engine = get_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "REFRESH MATERIALIZED VIEW CONCURRENTLY candidate_parcels")
    with engine.connect() as conn:
        n, = conn.execute(text("SELECT COUNT(*) FROM candidate_parcels")).one()
    click.echo(f"candidate_parcels refreshed: {n:,} rows")


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
@cli.group()
def metrics() -> None:
    """Recompute per-parcel suitability metrics.

    Run after any parcel reload + refresh-candidates: the metrics table
    is keyed on parcel id and new candidates start with no rows.
    """


@metrics.command("flood")
def metrics_flood() -> None:
    """Recompute sfha_pct for all candidate parcels (~10 min)."""
    import time

    import psycopg

    from ingest.db import get_conninfo
    from ingest.flood_nfhl import METRICS_SQL
    t0 = time.time()
    with psycopg.connect(get_conninfo()) as pg:
        pg.execute(METRICS_SQL)
        pg.commit()
    click.echo(f"flood metrics recomputed in {time.time() - t0:.0f}s")


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------
@cli.group()
def load() -> None:
    """Run data loaders."""


def _run_module(mod: str, *args: str) -> None:
    subprocess.run([sys.executable, "-m", mod, *args], cwd=ROOT, check=True)


@load.command()
def counties() -> None:
    """Rebuild county scope from Census+OSRM, then load into PostGIS."""
    _run_module("ingest.counties")
    _run_module("ingest.load_counties")


@load.command()
def wv() -> None:
    """Load WV parcels (statewide MapServer)."""
    _run_module("ingest.parcels_wv")


@load.command()
@click.argument("fips", nargs=-1)
def va(fips: tuple[str, ...]) -> None:
    """Load VA parcels (VGIN). Optional county FIPS args."""
    bad = [f for f in fips if not re.fullmatch(r"51\d{3}", f)]
    if bad:
        raise click.BadParameter(f"not VA county FIPS: {', '.join(bad)}")
    _run_module("ingest.parcels_va", *fips)


if __name__ == "__main__":
    cli()
