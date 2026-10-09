"""PostgreSQL access: connection pool, migrations, and a thin transaction wrapper."""
from __future__ import annotations

import atexit
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from cas.config import settings

MIGRATIONS = Path(__file__).parent / "migrations"

_lock = threading.Lock()
_pool: ConnectionPool | None = None
_embedded = None


def _embedded_url() -> str:
    """Start (or attach to) the embedded development PostgreSQL under data_dir."""
    global _embedded
    try:
        import pgserver
    except ImportError as exc:  # pragma: no cover - configuration error
        raise RuntimeError(
            "CAS_DATABASE_URL is not set and the embedded PostgreSQL (pgserver, a dev dependency) "
            "is not installed. Set CAS_DATABASE_URL or install with `pip install -e '.[dev]'`."
        ) from exc
    logging.getLogger("pgserver").setLevel(logging.WARNING)
    pgdata = settings.data_dir / "pg"
    pgdata.mkdir(parents=True, exist_ok=True)
    _embedded = pgserver.get_server(str(pgdata), cleanup_mode="stop")
    return _embedded.get_uri()


def database_url() -> str:
    return settings.database_url or _embedded_url()


def get_pool() -> ConnectionPool:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ConnectionPool(
                database_url(), min_size=2, max_size=20, open=True,
                kwargs={"row_factory": dict_row, "autocommit": False},
            )
            atexit.register(close_pool)
        return _pool


def close_pool() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def migrate() -> list[str]:
    """Apply any SQL migration that has not run yet. Returns the names applied."""
    applied: list[str] = []
    with get_pool().connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(727274)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
        conn.commit()
    return applied


def _adapt(params: Any) -> Any:
    """Send Python dicts and lists as jsonb; every JSON column in the schema is jsonb."""
    if params is None:
        return None
    if isinstance(params, dict):
        return {k: Jsonb(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}
    return [Jsonb(v) if isinstance(v, (dict, list)) else v for v in params]


class Tx:
    """One open database transaction."""

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def execute(self, sql: str, params: Any = None) -> psycopg.Cursor:
        return self.conn.execute(sql, _adapt(params))

    def one(self, sql: str, params: Any = None) -> dict | None:
        return self.execute(sql, params).fetchone()

    def all(self, sql: str, params: Any = None) -> list[dict]:
        return self.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Any = None) -> Any:
        row = self.execute(sql, params).fetchone()
        return None if row is None else next(iter(row.values()))

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        with self.conn.transaction():
            yield


@contextmanager
def transaction() -> Iterator[Tx]:
    """Commit on normal exit, roll back on any exception."""
    with get_pool().connection() as conn:
        try:
            yield Tx(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
