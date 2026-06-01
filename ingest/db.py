"""SQLAlchemy engine factory for the cabin_land database."""
from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine

load_dotenv()


def get_url() -> str:
    user = os.getenv("PGUSER", "postgres")
    pw = os.getenv("PGPASSWORD", "")
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    db = os.getenv("PGDATABASE", "cabin_land")
    return f"postgresql+psycopg://{user}:{pw}@{host}:{port}/{db}"


def get_engine() -> Engine:
    return create_engine(get_url(), future=True)
