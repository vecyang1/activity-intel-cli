"""SQLite store: response cache + cross-process pace.

Both live in one file because they must be transactionally consistent, and
because the pace row is the only thing that makes politeness real: an
in-process throttle lets two concurrent runs each comply with a 1s gap and
jointly send two requests in the same instant. Slot reservation inside a
transaction is the fix.

Bodies are kept only until their TTL and then dropped (`purge_expired`). These
are other people's catalogues; caching them briefly to avoid re-fetching is
ordinary client behaviour, retaining them indefinitely is not.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key         TEXT PRIMARY KEY,
    host        TEXT NOT NULL,
    url         TEXT NOT NULL,
    body        TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS cache_expiry ON cache(expires_at);

-- One row per host. Holds the next free request slot so every process on this
-- machine shares one lane per host.
CREATE TABLE IF NOT EXISTS pace (
    host          TEXT PRIMARY KEY,
    next_slot_at  REAL NOT NULL
);

-- Append-only record of what actually left the machine. Not a quota (no source
-- here publishes one) but an audit trail: "did this run hit the network, and
-- how hard" must be answerable after the fact.
CREATE TABLE IF NOT EXISTS requests (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    host      TEXT NOT NULL,
    url       TEXT NOT NULL,
    sent_at   REAL NOT NULL,
    status    INTEGER
);
CREATE INDEX IF NOT EXISTS requests_sent ON requests(sent_at);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or config.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def cache_get(conn: sqlite3.Connection, url: str, *, now: float) -> str | None:
    row = conn.execute(
        "SELECT body, expires_at FROM cache WHERE key = ?", (_key(url),)
    ).fetchone()
    if row is None or row["expires_at"] <= now:
        return None
    return row["body"]


def cache_put(conn: sqlite3.Connection, url: str, host: str, body: str,
              ttl_s: float, *, now: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO cache(key, host, url, body, fetched_at, expires_at) "
        "VALUES (?,?,?,?,?,?)",
        (_key(url), host, url, body, now, now + ttl_s),
    )
    conn.commit()


def purge_expired(conn: sqlite3.Connection, *, now: float) -> int:
    cur = conn.execute("DELETE FROM cache WHERE expires_at <= ?", (now,))
    conn.commit()
    return cur.rowcount


def purge_all(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM cache")
    conn.commit()
    return cur.rowcount


def reserve_slot(conn: sqlite3.Connection, host: str, gap_s: float, *, now: float) -> float:
    """Claim the next send slot for ``host``; return how long the caller must wait.

    The whole read-modify-write runs inside one IMMEDIATE transaction so two
    processes cannot both read the same ``next_slot_at`` and both decide they
    may send now.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT next_slot_at FROM pace WHERE host = ?", (host,)
        ).fetchone()
        next_slot = row["next_slot_at"] if row else 0.0
        send_at = max(now, next_slot)
        conn.execute(
            "INSERT OR REPLACE INTO pace(host, next_slot_at) VALUES (?,?)",
            (host, send_at + gap_s),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return max(0.0, send_at - now)


def record_request(conn: sqlite3.Connection, host: str, url: str,
                   status: int | None, *, now: float) -> None:
    conn.execute(
        "INSERT INTO requests(host, url, sent_at, status) VALUES (?,?,?,?)",
        (host, url, now, status),
    )
    conn.commit()


def request_stats(conn: sqlite3.Connection, *, since: float) -> list[dict]:
    rows = conn.execute(
        "SELECT host, COUNT(*) AS n, SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors "
        "FROM requests WHERE sent_at >= ? GROUP BY host ORDER BY n DESC",
        (since,),
    ).fetchall()
    return [dict(r) for r in rows]


def cache_stats(conn: sqlite3.Connection, *, now: float) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN expires_at > ? THEN 1 ELSE 0 END) AS fresh "
        "FROM cache", (now,)
    ).fetchone()
    return {"total": row["total"] or 0, "fresh": row["fresh"] or 0}


def now() -> float:
    return time.time()


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
