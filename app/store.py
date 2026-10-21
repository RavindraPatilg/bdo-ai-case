"""Analysis storage: SQLite, keyed by organisation number.

A saved analysis must be retrievable without a new LLM call. SQLite
keeps deployment simple (a single file); production would swap this for
PostgreSQL behind the same interface.
"""
import logging
import sqlite3
from threading import Lock
from typing import Optional

from .models import Analysis
from .config import settings

logger = logging.getLogger("app.store")

_lock = Lock()
_conn: Optional[sqlite3.Connection] = None
DB_PATH = settings.database_path


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS analyses ("
            "orgnr TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        _conn.commit()
    return _conn


def save_analysis(analysis: Analysis) -> None:
    with _lock:
        _db().execute(
            "INSERT OR REPLACE INTO analyses (orgnr, payload, created_at) VALUES (?, ?, ?)",
            (analysis.orgnr, analysis.model_dump_json(),
             analysis.generated_at.isoformat()),
        )
        _db().commit()


def get_analysis(orgnr: str) -> Optional[Analysis]:
    with _lock:
        row = _db().execute(
            "SELECT payload FROM analyses WHERE orgnr = ?", (orgnr,)
        ).fetchone()
    if not row:
        return None
    try:
        return Analysis.model_validate_json(row[0])
    except Exception as exc:
        # A corrupted row (or one saved by an older format) must not
        # break the read path: log it and treat as a cache miss.
        logger.warning("cache_row_invalid orgnr=%s error=%s", orgnr, exc)
        return None
