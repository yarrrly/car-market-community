"""Compose owned Wiveda Mercedes details on the collector's writer thread."""

import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from .bmw_detail_policy import BMWDetailPolicy, DetailDeferred, detail_fact_conflicts
from .fetch import Response
from .germany_enumeration import _blocked_response
from .germany_retained import _raw_body
from .mercedes_autoscout import _UUID
from .mercedes_de_sources import _public, wiveda_mercedes_payload_url
from .provenance import timestamp

SOURCE = "dealer:wiveda.de"


def owned_id(url, *, payload=False):
    if not _public(SOURCE, url):
        return None
    parsed = urlsplit(url)
    suffix = "/_payload.json" if payload else ""
    match = re.fullmatch(r"/fahrzeug/(" + _UUID + ")" + re.escape(suffix), parsed.path)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if not match or (
        payload
        and (
            set(query) - {"_b", "page"}
            or len(query.get("_b", [])) != 1
            or not query["_b"][0]
        )
    ):
        return None
    return match[1]


def detail_matches(collector, requested, final, body, identity):
    expected = owned_id(requested) or owned_id(requested, payload=True)
    actual = owned_id(final, payload=True)
    if not expected or actual != expected or identity != expected:
        return False
    page = collector._parse(SOURCE, body, final)
    return bool(
        len(page.listings) == 1
        and page.listings[0].external_id == expected
        and owned_id(page.listings[0].url) == expected
        and page.listings[0].raw.get("market_country") == "DE"
    )


def _access_denied(body, url):
    return _blocked_response(Response(url, url, 200, body, "")) or bool(
        re.search(
            r"(?is)<title[^>]*>\s*(?:login|log in|sign in|anmelden)\b", body[:4096]
        )
    )


def _facts(original, identity):
    if original.source != SOURCE or original.external_id != identity:
        return {}
    facts = {
        key: getattr(original, key)
        for key in ("price", "mileage", "year", "fuel", "transmission", "vin")
        if getattr(original, key) is not None
    }
    facts.update(
        {
            "raw." + key: original.raw[key]
            for key in (
                "market_country",
                "model_brand",
                "model_family",
                "model_variant",
                "body_type",
                "generation",
            )
            if original.raw.get(key) is not None
        }
    )
    return facts


def route_from_capture(collector, url, row, clock):
    identity = owned_id(url)
    observed = timestamp(row["fetched_at"]) if row else None
    if not (
        identity
        and row
        and row["status"] == 200
        and row["error"] in (None, "detail_unparsed")
        and owned_id(row["url"]) == identity
        and owned_id(row["final_url"]) == identity
        and observed
        and 0
        <= (clock - observed).total_seconds()
        < collector.config.get("detail_cache_hours", 6) * 3600
        and not collector._detail_revision_refresh(SOURCE, row, clock)
    ):
        return None
    try:
        html = _raw_body(collector.db.path.parent, row["raw_path"], row["content_hash"])
        if _access_denied(html, row["final_url"]):
            return None
        return wiveda_mercedes_payload_url(html, row["final_url"])
    except (ValueError, OSError):
        return None


def collect_details(collector, run, jobs):
    """Keep route evidence separate from the payload's accepted field clock."""
    used_before = getattr(collector, "_secondary_detail_attempts", 0) + sum(
        stats.get("attempted", 0) for stats in collector.detail_stats.values()
    )
    policy = BMWDetailPolicy(
        collector,
        run,
        limit=max(0, collector.config.get("detail_limit", 4000) - used_before),
    )
    result = {
        "cached": 0,
        "backoff": 0,
        "errors": 0,
        "enriched": 0,
        "completed": 0,
        "operations": 0,
        "deferred": 0,
        "held": 0,
    }
    for url, (source, _, original) in jobs:
        stats = collector.detail_stats.setdefault(
            source, {"attempted": 0, "parsed": 0, "errors": 0}
        )
        before, proof = policy.used, None
        satisfied_cache = False
        try:
            identity = owned_id(url)
            if not (
                identity
                and source == SOURCE
                and original.source == SOURCE
                and original.external_id == identity
                and owned_id(original.url) == identity
            ):
                raise ValueError("wiveda_detail_owner_mismatch")
            if not collector.specialized_details_enabled:
                raise DetailDeferred("details_disabled")
            if not collector._secondary_detail_eligible(original):
                raise DetailDeferred("detail_conclusively_excluded")
            clock = datetime.now(UTC)
            policy._host_backoff(SOURCE, url, clock)
            row = collector.db.conn.execute(
                "SELECT * FROM fetches WHERE source=? AND (url=? OR final_url=?) ORDER BY julianday(fetched_at) DESC,id DESC LIMIT 1",
                (SOURCE, url, url),
            ).fetchone()
            route = route_from_capture(collector, url, row, clock)
            if route is None:
                proof = policy.fetch(
                    SOURCE,
                    url,
                    request=lambda target=url: collector._secondary_get(
                        target, refresh=True
                    ),
                )
                response = proof.response
                if response.status != 200 or response.error or not response.body:
                    raise ValueError(
                        response.error or "wiveda_html_http_" + str(response.status)
                    )
                observed = timestamp(response.timestamp)
                if observed is None or observed > datetime.now(UTC):
                    raise ValueError("wiveda_capture_clock_unverified")
                if _access_denied(response.body, response.final_url):
                    raise ValueError("wiveda_access_response_held")
                if owned_id(response.final_url) != identity:
                    raise ValueError("wiveda_html_owner_mismatch")
                route = wiveda_mercedes_payload_url(response.body, response.final_url)
            facts = _facts(original, identity)
            proof = policy.fetch(
                SOURCE,
                route,
                identity=identity,
                facts=facts,
                request=lambda target=route: collector._secondary_get(
                    target, refresh=True
                ),
            )
            response = proof.response
            if response.status != 200 or response.error or not response.body:
                raise ValueError(
                    response.error or "wiveda_payload_http_" + str(response.status)
                )
            observed = timestamp(response.timestamp)
            if observed is None or observed > datetime.now(UTC):
                raise ValueError("wiveda_capture_clock_unverified")
            if response.url != route or response.final_url != route:
                raise ValueError("wiveda_payload_route_redirect")
            if not detail_matches(
                collector, url, response.final_url, response.body, identity
            ):
                raise ValueError("wiveda_detail_identity_mismatch")
            page = collector._parse(SOURCE, response.body, response.final_url)
            item = page.listings[0]
            if proof.cached and detail_fact_conflicts(facts, vars(item)):
                raise DetailDeferred("detail_cached_facts_conflict")
            if proof.cached:
                accepted = getattr(response, "detail_policy_item", None) or {}
                if (
                    accepted.get("source") != SOURCE
                    or accepted.get("external_id") != identity
                ):
                    raise DetailDeferred("detail_cached_publication_binding")
                # fetch() requires this exact accepted fetch ID/clock; no replay renewal.
                result["cached"] += 1
                stats["cached"] = stats.get("cached", 0) + 1
                satisfied_cache = True
            else:
                found = collector._observe(
                    run, item, response.timestamp, proof.fetch_id
                )
                if found is None:
                    raise ValueError("detail_not_accepted")
                policy.accept_observation(
                    item, response.timestamp, proof.fetch_id, found, None
                )
                result["enriched"] += 1
                stats["parsed"] += 1
        except DetailDeferred as error:
            if error.reason == "detail_budget_deferred":
                result["deferred"] += 1
                stats["deferred"] = stats.get("deferred", 0) + 1
            elif "backoff" not in error.reason:
                result["held"] += 1
                stats["held"] = stats.get("held", 0) + 1
            if "backoff" in error.reason:
                result["backoff"] += 1
                stats["backoff"] = stats.get("backoff", 0) + 1
            collector.failures.append(
                error.reason + ":" + source + ":" + original.external_id
            )
        except Exception as error:  # noqa: BLE001 — retain a failed native response and continue other providers
            result["errors"] += 1
            stats["errors"] += 1
            collector.failures.append("detail:" + source + ":" + str(error))
            if (
                proof
                and proof.requests
                and proof.response.status == 200
                and not proof.response.error
            ):
                collector.db.conn.execute(
                    "UPDATE fetches SET error=? WHERE id=?",
                    (str(error), proof.fetch_id),
                )
                collector.db.conn.commit()
        finally:
            attempts = policy.used - before
            result["operations"] += attempts
            stats["attempted"] += attempts
            result["completed"] += int(bool(attempts) and not satisfied_cache)
    return result
