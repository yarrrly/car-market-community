"""Pause the same refused AutoWereld search using original persisted clocks."""

import math
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit


def query_backoff_until(connection, query, config, *, as_of):
    """Return a fixed expiry for an own query's 403/429; never renew history."""
    if query.get("source") != "autowereld":
        return None
    uri = query.get("url")
    if not isinstance(uri, str):
        return None
    try:
        parsed = urlsplit(uri)
        own = (
            parsed.scheme == "https"
            and parsed.hostname in {"autowereld.nl", "www.autowereld.nl"}
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )
    except ValueError:
        return None
    if not own:
        return None
    hours = config.get(
        "query_error_backoff_hours", config.get("detail_error_backoff_hours", 6)
    )
    if isinstance(hours, bool):
        raise TypeError("query_error_backoff_hours must be a finite nonnegative number")
    try:
        hours = float(hours)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "query_error_backoff_hours must be a finite nonnegative number"
        ) from error
    if not math.isfinite(hours) or hours < 0:
        raise ValueError(
            "query_error_backoff_hours must be a finite nonnegative number"
        )
    if hours == 0:
        return None
    try:
        evaluation = datetime.fromisoformat(as_of)
    except (TypeError, ValueError):
        return None
    if evaluation.tzinfo is None:
        return None
    rows = connection.execute(
        "SELECT fetched_at FROM fetches WHERE source=? AND status IN (403,429) "
        "AND (url=? OR final_url=?) AND julianday(fetched_at)<=julianday(?) "
        "AND julianday(fetched_at)>julianday(?) "
        "ORDER BY julianday(fetched_at) DESC,id DESC",
        # SQLite's date arithmetic is coarser than capture microseconds. Admit
        # one extra second, then decide the exact boundary with aware datetime.
        (
            "autowereld",
            uri,
            uri,
            evaluation.isoformat(),
            (evaluation - timedelta(hours=hours, seconds=1)).isoformat(),
        ),
    )
    for row in rows:
        try:
            captured = datetime.fromisoformat(row["fetched_at"])
        except (TypeError, ValueError):
            continue
        if captured.tzinfo is None or captured > evaluation:
            continue
        expiry = captured + timedelta(hours=hours)
        if evaluation < expiry:
            return expiry.astimezone(UTC).isoformat()
    return None
