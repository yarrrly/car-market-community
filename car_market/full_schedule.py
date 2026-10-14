"""Bound expensive full attempts even when collection fails or restarts."""

import hashlib
import json
import re
from datetime import UTC, datetime

from .provenance import timestamp

KEY = "full_collection_attempt"


def has_missing_queries(conn, config):
    return conn is None or any(
        not conn.execute(
            "SELECT 1 FROM source_runs WHERE source=? AND query_key=? LIMIT 1",
            (query["source"], query["key"]),
        ).fetchone()
        for query in config.get("queries", [])
    )


def query_signature(config):
    payload = json.dumps(config.get("queries", []), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_attempt(conn, config, at):
    """Call under the writer lock, before any full-pass network requests."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS monitor_state(key TEXT PRIMARY KEY,value TEXT)"
    )
    value = json.dumps({"started_at": at, "query_signature": query_signature(config)})
    conn.execute("INSERT OR REPLACE INTO monitor_state VALUES (?,?)", (KEY, value))
    conn.commit()


def remaining(conn, config, *, clock=None, missing_queries=None):
    """Read persisted attempt timing; old databases fall back to run history."""
    clock = clock or datetime.now(UTC)
    row = None
    if (
        conn
        and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_state'"
        ).fetchone()
    ):
        row = conn.execute(
            "SELECT value FROM monitor_state WHERE key=?", (KEY,)
        ).fetchone()
    had_record = bool(row)
    if row:
        try:
            state = json.loads(row[0])
        except (ValueError, TypeError):
            state = None
        if (
            isinstance(state, dict)
            and timestamp(state.get("started_at"))
            and isinstance(state.get("query_signature"), str)
            and re.fullmatch(r"[0-9a-f]{64}", state["query_signature"])
        ):
            if state.get("query_signature") != query_signature(config):
                return 0
            started = timestamp(state["started_at"])
        else:
            row = None
    if not row:
        if conn is None:
            return 0
        if not had_record and (
            has_missing_queries(conn, config)
            if missing_queries is None
            else missing_queries
        ):
            return 0
        last = conn.execute(
            "SELECT started_at FROM runs WHERE kind IN ('live','full') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        started = timestamp(last[0]) if last else None
    if started is None:
        return 0
    age = max(0, (clock - started).total_seconds())
    return max(0, config.get("interval_hours", 6) * 3600 - age)
