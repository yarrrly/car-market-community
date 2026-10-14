"""Refusal/reservation identity for proven Gaspedaal -> own AutoWereld publications.

Historical linkage is not a fresh field observation or a physical-vehicle merge.
"""

import hashlib
import json
import re
import zlib
from datetime import timedelta
from urllib.parse import urljoin, urlsplit

from .autowereld_details import _own_id, parse_autowereld_detail
from .db import now
from .germany_retained import _raw_body
from .model_profiles import BMW_PROFILE, is_mercedes, own_country
from .parse import parse_page
from .provenance import timestamp


def _uri(url, hosts):
    try:
        value = urlsplit(url)
        if (
            value.scheme != "https"
            or value.hostname not in hosts
            or value.username is not None
            or value.password is not None
            or value.port not in (None, 443)
            or value.query
            or value.fragment
        ):
            return None
        return value
    except (TypeError, ValueError):
        return None


def _redirect(url):
    parsed = _uri(url, {"api.gaspedaal.nl"})
    return bool(parsed and re.fullmatch(r"/redirect/vehicle/\d+", parsed.path))


def _publication(url):
    parsed = _uri(url, {"autowereld.nl", "www.autowereld.nl"})
    return bool(parsed and parsed.path.endswith("/details.html") and _own_id(url))


def _advertises(raw, url):
    portals = raw.get("portalen")
    return isinstance(portals, list) and any(
        isinstance(portal, dict) and portal.get("klikUrl") == url for portal in portals
    )


def _conflicted(payload):
    raw = payload.get("raw") or {}
    if not isinstance(raw, dict):
        return True
    country, proof = own_country(raw)
    return bool(
        payload.get("scope") not in {"target", "needs_confirmation"}
        or raw.get("field_conflicts")
        or raw.get("nl_publication_conflicts")
        or raw.get("market_context_country") not in (None, "NL")
        or country not in (None, "NL")
        or proof.get("kind") == "conflicting_own_country"
        or raw.get("model_profile") not in (None, BMW_PROFILE)
    )


def _owned(collector, original, clock, pages=None):
    """Bind current source/ID/URI to its accepted, byte-verified own capture."""
    row = collector.db.conn.execute(
        "SELECT l.source,l.external_id,l.url,l.scope,o.details_json,o.observed_at,"
        "f.id AS capture_id,f.source AS capture_source,f.url AS capture_url,f.final_url,f.status,f.error,"
        "f.fetched_at,f.raw_path,f.content_hash FROM listings l "
        "JOIN latest_observations o ON o.listing_id=l.id JOIN fetches f ON f.id=o.fetch_id "
        "WHERE l.source=? AND l.external_id=?",
        (original.source, original.external_id),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["details_json"])
        captured = timestamp(row["fetched_at"])
        if (
            row["scope"] not in {"target", "needs_confirmation"}
            or payload.get("source") != original.source
            or payload.get("external_id") != original.external_id
            or payload.get("url") != original.url
            or row["url"] != original.url
            or row["capture_source"] != original.source
            or row["status"] != 200
            or row["error"] is not None
            or captured is None
            or captured > clock
            or timestamp(row["observed_at"]) != captured
            or _conflicted(payload)
            or _conflicted(vars(original))
        ):
            return None
        if original.source == "autowereld" and (
            row["final_url"] != original.url or not _publication(original.url)
        ):
            return None
        if original.source == "gaspedaal":
            # Parsed bytes can be shared; admission must bind every record and
            # its current capture independently, including on a cache hit.
            for uri, fragment in (
                (original.url, original.external_id),
                (row["capture_url"], ""),
                (row["final_url"], ""),
            ):
                publication = urlsplit(uri)
                if (
                    publication.scheme not in {"http", "https"}
                    or publication.hostname not in {"gaspedaal.nl", "www.gaspedaal.nl"}
                    or publication.fragment != fragment
                    or publication.username is not None
                    or publication.password is not None
                    or publication.port
                    not in (None, 443 if publication.scheme == "https" else 80)
                ):
                    return None
        cache_key = (
            original.source,
            row["capture_id"],
            row["fetched_at"],
            row["final_url"],
            row["raw_path"],
            row["content_hash"],
        )
        if pages is not None and cache_key in pages:
            page = pages[cache_key]
        else:
            body = _raw_body(
                collector.db.path.parent.resolve(), row["raw_path"], row["content_hash"]
            )
            page = None
        if page is None and original.source == "gaspedaal":
            page = parse_page("gaspedaal", body, row["final_url"])
        elif page is None:
            page = parse_autowereld_detail(body, row["final_url"])
        if pages is not None and page is not None:
            pages[cache_key] = page
        own = (
            [
                item
                for item in page.listings
                if item.external_id == original.external_id and item.url == original.url
            ]
            if page
            else []
        )
        if len(own) != 1:
            return None
        if original.source == "autowereld":
            for candidate in (payload, vars(original), vars(own[0])):
                proof = (candidate.get("raw") or {}).get("identity_evidence")
                if proof is not None and (
                    not isinstance(proof, dict)
                    or str(proof.get("nr")) != _own_id(original.url)
                    or proof.get("url") != original.url
                    or not isinstance(proof.get("canonical"), str)
                    or urljoin(original.url, proof["canonical"]) != original.url
                ):
                    return None
        for field in ("kenteken", "vin"):
            values = {
                value
                for value in (
                    getattr(original, field),
                    payload.get(field),
                    getattr(own[0], field),
                )
                if value
            }
            if len(values) > 1:
                return None
        return own[0], payload
    except (
        OSError,
        EOFError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        zlib.error,
    ):
        return None


def _binding(collector, url, original, pages=None):
    clock = timestamp(now())
    if (
        clock is None
        or collector.country != "NL"
        or collector.config.get("scope", {}).get("model_profile", BMW_PROFILE)
        != BMW_PROFILE
        or is_mercedes(original)
        or original.source not in {"gaspedaal", "autowereld"}
    ):
        return None
    owned = _owned(collector, original, clock, pages)
    if owned is None:
        return None
    if original.source == "autowereld":
        return (url, clock) if url == original.url and _publication(url) else None
    native, payload = owned
    for raw in (native.raw, original.raw, payload.get("raw") or {}):
        identity = raw.get("advertentieId")
        if (
            type(identity) not in (str, int)
            or not str(identity).isdigit()
            or str(identity) != original.external_id
        ):
            return None
    if not _redirect(url):
        return None
    for item in (native, original):
        if not _advertises(item.raw, url):
            return None
    if not _advertises(payload.get("raw") or {}, url):
        return None
    # Exact requested-URI index only. Distinct recorded destinations stay ambiguous;
    # a label or the redirect vehicle ID cannot construct an AutoWereld destination.
    destinations = set()
    for row in collector.db.conn.execute("SELECT * FROM fetches WHERE url=?", (url,)):
        observed = timestamp(row["fetched_at"])
        if observed is None or observed > clock:
            return None
        if (
            row["source"] != "autowereld"
            or row["status"] not in (200, 403, 429)
            or row["error"] not in (None, "http_" + str(row["status"]))
            or not _publication(row["final_url"])
        ):
            return None
        destinations.add(row["final_url"])
    if len(destinations) != 1:
        return None
    destination = destinations.pop()
    row = collector.db.conn.execute(
        "SELECT o.details_json FROM listings l JOIN latest_observations o ON o.listing_id=l.id "
        "WHERE l.source='autowereld' AND l.external_id=? AND l.url=?",
        (destination, destination),
    ).fetchone()
    if row is None:
        return None
    from .parse import Listing

    try:
        direct = Listing(**json.loads(row[0]))
    except (ValueError, TypeError):
        return None
    if _owned(collector, direct, clock, pages) is None:
        return None
    for field in ("kenteken", "vin"):
        if (
            getattr(original, field)
            and getattr(direct, field)
            and (getattr(original, field) != getattr(direct, field))
        ):
            return None
    return destination, clock


def _in_error_backoff(collector, destination, clock):
    hours = collector.config.get("detail_error_backoff_hours", 6)
    if hours <= 0:
        return False
    for row in collector.db.conn.execute(
        "SELECT source,url,final_url,status,error,fetched_at FROM fetches "
        "WHERE (url=? OR final_url=?) AND source='autowereld' AND status IN (403,429) "
        "AND julianday(fetched_at)>=julianday(?) AND julianday(fetched_at)<=julianday(?)",
        (
            destination,
            destination,
            (clock - timedelta(hours=hours, seconds=1)).isoformat(),
            (clock + timedelta(seconds=1)).isoformat(),
        ),
    ):
        captured = timestamp(row["fetched_at"])
        if (
            captured is not None
            and row["final_url"] == destination
            and row["error"] in (None, "http_" + str(row["status"]))
            and 0 <= (clock - captured).total_seconds() <= hours * 3600
        ):
            return True
    return False


def prepare(collector, jobs):
    """Resolve read-only identity/refusal decisions on the writer before workers."""
    policies, pages = {}, {}
    for url, (_, _, original) in jobs:
        binding = _binding(collector, url, original, pages)
        if binding is None:
            continue
        destination, clock = binding
        key = "gaspedaal_autowereld:" + hashlib.sha256(destination.encode()).hexdigest()
        blocked = original.source == "gaspedaal" and _in_error_backoff(
            collector, destination, clock
        )
        policies[url] = key, blocked
    return policies
