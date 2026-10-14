"""Durable error backoff for verified own Trovit cards with rotating signed URIs."""

import hashlib
import json
import re
from datetime import timedelta
from urllib.parse import urlsplit

from .db import now
from .provenance import timestamp
from .trovit_details import is_trovit_captcha_route


def _origin(url):
    try:
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        port = parsed.port
        return parsed.scheme, parsed.hostname, 443 if port is None else port
    except (TypeError, ValueError):
        return None


def _native_id(url):
    if _origin(url) != ("https", "rd.clk.thribee.com", 443):
        return None
    match = re.match(r"^/id\.([A-Za-z0-9_-]+)/", urlsplit(url).path)
    return match.group(1) if match else None


def _binding(collector, url, original):
    identity = _native_id(url)
    clock = timestamp(now())
    if identity is None or collector.country != "NL" or clock is None:
        return None
    original_raw = original.raw if isinstance(original.raw, dict) else {}
    if any(
        original_raw.get(field) not in (None, "NL")
        for field in ("market_country", "market_context_country")
    ):
        return None
    if original.source == "trovit" and str(original.external_id) != identity:
        return None
    owner = collector.db.conn.execute(
        "SELECT l.id,l.source,l.external_id,o.details_json,o.observed_at,f.source AS capture_source,"
        "f.fetched_at,f.content_hash,f.url AS capture_url,f.final_url AS capture_final,f.status,f.error "
        "FROM listings l JOIN latest_observations o ON o.listing_id=l.id "
        "JOIN fetches f ON f.id=o.fetch_id WHERE l.source='trovit' AND l.external_id=?",
        (identity,),
    ).fetchone()
    if owner is None:
        return None
    try:
        payload = json.loads(owner["details_json"])
        raw = payload.get("raw") or {}
        captured = timestamp(owner["fetched_at"])
        if (
            payload.get("source") != owner["source"]
            or str(payload.get("external_id")) != identity
            or not isinstance(raw.get("card_text"), str)
            or not raw["card_text"]
            or _native_id(payload.get("url")) != identity
            or owner["capture_source"] != "trovit"
            or owner["status"] != 200
            or owner["error"] is not None
            or not isinstance(owner["content_hash"], str)
            or re.fullmatch(r"[0-9a-f]{64}", owner["content_hash"]) is None
            or captured is None
            or captured > clock
            or timestamp(owner["observed_at"]) != captured
            or any(
                raw.get(field) not in (None, "NL")
                for field in ("market_country", "market_context_country")
            )
            or any(
                _origin(owner[field]) != ("https", "autos.trovit.nl", 443)
                for field in ("capture_url", "capture_final")
            )
            or any(
                "captcha" in urlsplit(owner[field]).path.lower()
                for field in ("capture_url", "capture_final")
            )
        ):
            return None
    except (TypeError, ValueError, AttributeError):
        return None
    key = "trovit_detail_attempt:" + hashlib.sha256(identity.encode()).hexdigest()
    return identity, key, clock, payload["url"]


def _valid_attempt(row, identity, clock):
    origin = _origin(row["final_url"]) if row else None
    actual_source = {
        ("https", "autos.trovit.nl", 443): "trovit",
        ("https", "rd.clk.thribee.com", 443): "dealer:rd.clk.thribee.com",
    }.get(origin)
    return bool(
        row
        and actual_source is not None
        and row["source"] == actual_source
        and _native_id(row["url"]) == identity
        and timestamp(row["fetched_at"]) is not None
        and timestamp(row["fetched_at"]) <= clock
    )


def _save_reference(collector, identity, key, row):
    if row is None:
        return
    value = {"source": "trovit", "external_id": identity, "fetch_id": None}
    if row is not None:
        value.update(
            {
                "fetch_id": row["id"],
                "content_hash": row["content_hash"],
                "fetched_at": row["fetched_at"],
            }
        )
    collector.db.conn.execute(
        "INSERT INTO monitor_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, sort_keys=True)),
    )
    collector.db._commit()


def in_error_backoff(collector, url, original):
    binding = _binding(collector, url, original)
    hours = collector.config.get("detail_error_backoff_hours", 6)
    if binding is None or hours <= 0:
        return False
    identity, key, clock, observed_uri = binding
    state = collector.db.conn.execute(
        "SELECT value FROM monitor_state WHERE key=?", (key,)
    ).fetchone()
    row, initialized = None, False
    if state:
        try:
            reference = json.loads(state[0])
            initialized = (
                reference.get("source") == "trovit"
                and reference.get("external_id") == identity
                and "fetch_id" in reference
            )
            if initialized and reference["fetch_id"] is None:
                initialized = False
            if initialized:
                if type(reference["fetch_id"]) is not int:
                    initialized = False
                else:
                    row = collector.db.conn.execute(
                        "SELECT * FROM fetches WHERE id=?", (reference["fetch_id"],)
                    ).fetchone()
                    initialized = _valid_attempt(row, identity, clock) and all(
                        field in reference and reference[field] == row[field]
                        for field in ("content_hash", "fetched_at")
                    )
        except (TypeError, ValueError, AttributeError):
            initialized = False
    if not initialized:
        row = None
        # The own native ID is already bound above; this is an indexed literal URI
        # namespace range, not canonicalization of an arbitrary advertisement URL.
        prefixes = {
            request.split("/id.", 1)[0] + "/id." + identity + "/"
            for request in (url, observed_uri)
        }
        parameters = []
        ranges = []
        for prefix in sorted(prefixes):
            ranges.append("(url>=? AND url<?)")
            parameters.extend((prefix, prefix[:-1] + "0"))
        parameters.extend(
            ((clock - timedelta(hours=hours)).isoformat(), clock.isoformat())
        )
        for candidate in collector.db.conn.execute(
            "SELECT * FROM fetches WHERE (" + " OR ".join(ranges) + ") "
            "AND source IN ('trovit','dealer:rd.clk.thribee.com') "
            "AND julianday(fetched_at)>julianday(?) AND julianday(fetched_at)<=julianday(?) "
            "ORDER BY julianday(fetched_at) DESC,id DESC",
            parameters,
        ):
            if _valid_attempt(candidate, identity, clock):
                row = candidate
                break
        _save_reference(collector, identity, key, row)
    captcha_route = (
        row is not None
        and row["status"] == 200
        and is_trovit_captcha_route(row["final_url"])
    )
    if row is None or not (
        row["error"] or row["status"] in (403, 429) or captcha_route
    ):
        return False
    age = (clock - timestamp(row["fetched_at"])).total_seconds()
    if not 0 <= age < hours * 3600:
        return False
    if (
        not captcha_route
        and row["status"] == 200
        and row["error"] in ("detail_unparsed", "detail_parse_error")
    ):
        cutoff = timestamp(collector.source_parser_refresh_since.get("trovit"))
        if cutoff and timestamp(row["fetched_at"]) < cutoff <= clock:
            return False
    return True


def remember_attempt(collector, url, original, fetch_id):
    binding = _binding(collector, url, original)
    if binding is None:
        return
    identity, key, clock, _observed_uri = binding
    row = collector.db.conn.execute(
        "SELECT * FROM fetches WHERE id=?", (fetch_id,)
    ).fetchone()
    if _valid_attempt(row, identity, clock):
        _save_reference(collector, identity, key, row)


def publication_key(collector, url, original):
    """Identify only verified own native aliases for within-pass request dedup."""
    binding = _binding(collector, url, original)
    return binding[1] if binding else None
