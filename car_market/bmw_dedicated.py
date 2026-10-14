"""BMW dealer detail phases using the existing durable, run-wide policy."""

import json

from .bmw_detail_policy import DetailDeferred, deferred_record
from .germany_mobile import DEALER_SOURCE, mobile_dealer_ad_id, parse_mobile_dealer


def persist_frontier(collector, key, ledger):
    state_key = "bmw_dedicated:" + key
    previous = collector.db.conn.execute(
        "SELECT value FROM monitor_state WHERE key=?", (state_key,)
    ).fetchone()
    if previous:
        try:
            old = json.loads(previous[0])
        except (ValueError, TypeError):
            old = {}
        # A refused search must not erase the last retained census and its clocks.
        if isinstance(old, dict) and old.get("source") == ledger.get("source"):
            if "frontier" in ledger and not ledger["frontier"]:
                ledger["retained_frontier"] = (
                    old.get("frontier") or old.get("retained_frontier") or []
                )
            if "census" in ledger:
                current = {entry["stock_id"] for entry in ledger["census"]}
                previous = {
                    entry["stock_id"]: entry
                    for entry in [
                        *old.get("retained_census", []),
                        *old.get("census", []),
                    ]
                }
                unresolved = [
                    entry
                    for identity, entry in previous.items()
                    if identity not in current and not ledger.get("census_complete")
                ]
                if unresolved:
                    ledger["retained_census"] = unresolved
                else:
                    ledger.pop("retained_census", None)
                retained = {entry["stock_id"] for entry in unresolved}
                details = {
                    entry["stock_id"]: entry
                    for entry in [
                        *old.get("retained_details", []),
                        *old.get("details", []),
                    ]
                }
                retained_details = [
                    entry for identity, entry in details.items() if identity in retained
                ]
                if retained_details:
                    ledger["retained_details"] = retained_details
                else:
                    ledger.pop("retained_details", None)
    collector.db.conn.execute(
        "INSERT OR REPLACE INTO monitor_state VALUES (?,?)",
        (state_key, json.dumps(ledger, ensure_ascii=False)),
    )
    collector.db.conn.commit()


class WivedaDetailTransport:
    """Count the two ordinary HTTP captures within one accepted native bundle.

    BMWDetailPolicy bounds this adapter and checks each actual target host.
    Inventory HTML/payload requests continue through the original getter.
    """

    def __init__(self, getter):
        self.getter = getter
        self.http_requests = 0
        self.max_requests = 2
        self.before_request = None
        self.captures = []

    def get(self, url):
        if self.http_requests >= self.max_requests:
            raise ValueError("public_http_request_budget")
        if self.before_request:
            self.before_request(url)
        self.http_requests += 1
        response = self.getter.get(url, refresh=True)
        self.captures.append(response)
        return response


def collect_bmw_mobile_dealer(collector, run, query):
    """Keep every native search ID even when detail work is disabled or deferred."""
    from .germany_mobile import DEALER_SEARCH

    limit = query.get("max_details", 30)
    if type(limit) is not int or not 0 <= limit <= 100:
        raise ValueError("max_details must be an integer between 0 and 100")
    if query["url"] != DEALER_SEARCH:
        raise ValueError("Only the observed public dealer search is callable")
    policy = collector.specialized_detail_policy(run)
    response = collector.fetcher.get(query["url"])
    fetch_id = collector.fetch_record(run, DEALER_SOURCE, response)
    errors, accepted = [], []
    page = parse_mobile_dealer(response.body or "", response.final_url)
    if response.status != 200 or response.error or response.final_url != query["url"]:
        errors.append(response.error or "dealer_search_http:" + str(response.status))
        page.links, page.total = [], None
    if page.pages != 1:
        errors.append("dealer_pagination_unobserved")
    frontier = [
        {
            "id": mobile_dealer_ad_id(url),
            "url": url,
            "search_fetch_id": fetch_id,
            "search_observed_at": response.timestamp,
            "status": "pending",
        }
        for url in page.links
    ]
    ledger = {"source": DEALER_SOURCE, "expected": page.total, "frontier": frontier}
    persist_frontier(collector, query["key"], ledger)
    attempted = cached = 0
    for entry in frontier:
        before = policy.used
        try:
            proof = policy.fetch(
                DEALER_SOURCE,
                entry["url"],
                identity=entry["id"],
                query_remaining=max(0, limit - attempted),
            )
            detail = parse_mobile_dealer(proof.response.body, proof.response.final_url)
            if (
                proof.response.status != 200
                or proof.response.error
                or proof.response.final_url != entry["url"]
                or len(detail.listings) != 1
                or detail.listings[0].external_id != entry["id"]
            ):
                raise ValueError("own_detail_unverified:" + entry["url"])
            item = detail.listings[0]
            if not proof.cached:
                listing_id = collector._observe(
                    run, item, proof.response.timestamp, proof.fetch_id, query["key"]
                )
                policy.accept_observation(
                    item,
                    proof.response.timestamp,
                    proof.fetch_id,
                    listing_id,
                    query["key"],
                )
            else:
                listing_id = getattr(proof.response, "detail_policy_listing_id", None)
                if (
                    not getattr(proof.response, "detail_policy_accepted", False)
                    or not collector.db.conn.execute(
                        "SELECT id FROM listings WHERE id=? AND source=? AND external_id=?",
                        (listing_id, item.source, item.external_id),
                    ).fetchone()
                ):
                    raise DetailDeferred("accepted_detail_identity_unverified")
                # Presence in this native search is current; the cached facts are not.
                collector.db.conn.execute(
                    "INSERT INTO query_membership(source,query_key,listing_id,last_run_id) VALUES (?,?,?,?) "
                    "ON CONFLICT(source,query_key,listing_id) DO UPDATE SET "
                    "last_run_id=excluded.last_run_id,missed_complete_runs=0",
                    (item.source, query["key"], listing_id, run),
                )
            accepted.append(item)
            cached += int(proof.cached)
            entry.update(
                status="accepted",
                fetch_id=proof.fetch_id,
                observed_at=proof.response.timestamp,
                cached=proof.cached,
            )
        except DetailDeferred as error:
            entry.update(deferred_record(error, entry["url"]))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            entry.update(status="pending", reason=str(error))
            errors.append(str(error))
        finally:
            attempted += policy.used - before
            persist_frontier(collector, query["key"], ledger)
    deferred = [entry for entry in frontier if entry["status"] != "accepted"]
    if len(accepted) != page.total:
        errors.append(f"own_detail_count_mismatch:{len(accepted)}/{page.total}")
    return {
        "source": DEALER_SOURCE,
        "status": "complete_dealer_subset"
        if not errors and not deferred
        else "partial",
        "expected_count": page.total,
        "discovered_count": len(frontier),
        "received_count": len(accepted),
        "target_count": sum(item.scope == "target" for item in accepted),
        "national_coverage_verified": False,
        "mobile_publications_verified": 0,
        "detail_HTTP_requests": attempted,
        "detail_cache_hits": cached,
        "detail_frontier": deferred,
        "errors": errors,
    }


def bmw_wiveda_context(collector, item=None):
    """Only the current legacy BMW DE scope may use its native detail boundary."""
    from .mercedes_collect import MercedesCollector
    from .model_profiles import BMW_PROFILE, bound_scope, is_mercedes

    scope = collector.config.get("scope", {})
    if (
        not isinstance(scope, dict)
        or collector.country != "DE"
        or scope.get("country", collector.country) != "DE"
        or scope.get("model_profile", BMW_PROFILE) != BMW_PROFILE
        or isinstance(collector, MercedesCollector)
    ):
        return False
    if is_mercedes(item):
        return False
    try:
        bound = bound_scope(collector.db.conn)
        if bound and (
            bound.get("country", collector.country) != "DE"
            or bound.get("model_profile", BMW_PROFILE) != BMW_PROFILE
        ):
            return False
    except (ValueError, TypeError, AttributeError):
        return False
    if item is not None:
        if item.raw.get("model_profile", BMW_PROFILE) != BMW_PROFILE:
            return False
        native = item.raw.get("native_vehicle")
        brands = [item.raw.get("model_brand")]
        if isinstance(native, dict):
            brands.append(native.get("brand"))
        if any(brand is not None and str(brand).upper() != "BMW" for brand in brands):
            return False
    return True


def fetch_wiveda_detail(
    collector,
    run,
    item,
    *,
    key=None,
    query_remaining=None,
    requested_url=None,
    queued_source=None,
    listing_id=None,
):
    """Use the accepted BMW native pair and durable policy on the writer thread."""
    import re
    from urllib.parse import urlsplit

    from .bmw_detail_policy import DetailDeferred, detail_fact_conflicts
    from .db import now
    from .fetch import Response
    from .germany_dealer_inventory import (
        UUID,
        WIVEDA_SOURCE,
        parse_wiveda_bundle,
        wiveda_payload_url,
    )
    from .germany_dealers import _uri
    from .germany_enumeration import _blocked_response
    from .provenance import timestamp

    if not bmw_wiveda_context(collector, item):
        raise DetailDeferred("detail_profile_mismatch")
    requested_url = item.url if requested_url is None else requested_url
    queued_source = item.source if queued_source is None else queued_source
    parsed = urlsplit(item.url)
    identity = re.fullmatch(r"/fahrzeug/(" + UUID + r")", parsed.path)
    if not (
        collector.country == "DE"
        and queued_source == item.source == WIVEDA_SOURCE
        and requested_url == item.url == item.external_id
        and parsed.scheme == "https"
        and parsed.netloc == "wiveda.de"
        and not parsed.query
        and not parsed.fragment
        and identity
        and item.raw.get("dealer_stock_id", identity[1]) == identity[1]
    ):
        raise ValueError("wiveda_detail_owner_mismatch")
    if listing_id is not None:
        listing = collector.db.conn.execute(
            "SELECT source,external_id,url FROM listings WHERE id=?",
            (listing_id,),
        ).fetchone()
        if not listing or (
            listing["source"],
            listing["external_id"],
            listing["url"],
        ) != (
            WIVEDA_SOURCE,
            item.external_id,
            item.url,
        ):
            raise ValueError("wiveda_detail_registry_mismatch")
    if item.scope == "excluded":
        raise DetailDeferred("detail_conclusively_excluded")
    if item.scope not in {"target", "needs_confirmation"}:
        raise DetailDeferred("detail_scope_unverified")
    policy = collector.specialized_detail_policy(run)
    proof = None

    def verified_clock(response):
        stamp, clock = timestamp(response.timestamp), timestamp(now())
        if stamp is None or clock is None or stamp > clock:
            raise ValueError("wiveda_capture_clock_unverified")

    original_getter = collector.fetcher
    transport = WivedaDetailTransport(original_getter)

    def request():
        # Do not spend the last one-request slot on half a native VDP.
        if transport.max_requests - transport.http_requests < 2:
            raise ValueError("public_http_request_budget")
        html = transport.get(item.url)
        verified_clock(html)
        if html.status != 200 or html.error or not html.body:
            raise ValueError(html.error or "wiveda_http_" + str(html.status))
        if html.url != item.url or _uri(html.final_url, item.url) != item.url:
            raise ValueError("wiveda_own_route_redirect")
        if _blocked_response(html) or re.search(
            r"(?is)<title[^>]*>\s*(?:login|log in|sign in|anmelden)\b",
            html.body[:4096],
        ):
            raise ValueError("wiveda_access_response_held")
        payload_url = wiveda_payload_url(html.body, html.final_url)
        native = transport.get(payload_url)
        verified_clock(native)
        if native.status != 200 or native.error or not native.body:
            raise ValueError(native.error or "wiveda_http_" + str(native.status))
        if native.url != payload_url or native.final_url != payload_url:
            raise ValueError("wiveda_own_route_redirect")
        bundle = {
            "kind": "wiveda_native",
            "html_url": html.final_url,
            "html": html.body,
            "payload_url": native.final_url,
            "payload": json.loads(native.body),
        }
        response = Response(
            html.url,
            html.final_url,
            200,
            json.dumps(bundle),
            native.timestamp,
            transport="ordinary_HTTP_own_HTML_payload_bundle",
        )
        response.extra_responses = list(transport.captures)
        return response

    def capture(response):
        for raw in response.extra_responses:
            collector.fetch_record(run, WIVEDA_SOURCE, raw)
        return response, collector.fetch_record(run, WIVEDA_SOURCE, response)

    facts = {
        name: getattr(item, name)
        for name in ("price", "year", "mileage", "fuel", "transmission", "vin")
    }
    for name in ("brand", "model", "variant", "country"):
        facts["raw.native_vehicle." + name] = (
            item.raw.get("native_vehicle") or {}
        ).get(name)
    if item.raw.get("market_country") is not None:
        facts["raw.market_country"] = item.raw["market_country"]
    collector.fetcher = transport
    try:
        proof = policy.fetch(
            WIVEDA_SOURCE,
            item.url,
            identity=item.external_id,
            request=request,
            capture=capture,
            facts=facts,
            query_remaining=query_remaining,
        )
    finally:
        collector.fetcher = original_getter
    try:
        page = parse_wiveda_bundle(json.loads(proof.response.body), item.url)
        if (
            page.search_filters.get("parse_status") != "parsed_own_detail"
            or len(page.listings) != 1
            or page.listings[0].raw["dealer_stock_id"] != identity[1]
            or page.listings[0].external_id != item.external_id
        ):
            raise ValueError("wiveda_detail_identity_changed")
        updated = page.listings[0]
        if proof.cached:
            accepted = getattr(proof.response, "detail_policy_item", None) or {}
            if (accepted.get("source"), accepted.get("external_id")) != (
                WIVEDA_SOURCE,
                item.external_id,
            ):
                raise DetailDeferred("detail_cached_publication_binding")
            if detail_fact_conflicts(facts, vars(updated)):
                raise DetailDeferred("detail_cached_facts_conflict")
        else:
            found = collector._observe(
                run, updated, proof.response.timestamp, proof.fetch_id, key
            )
            if found is None:
                raise ValueError("detail_not_accepted")
            policy.accept_observation(
                updated, proof.response.timestamp, proof.fetch_id, found, key
            )
        return updated, proof
    except (ValueError, TypeError, KeyError, AttributeError):
        if not proof.cached:
            collector.db.conn.execute(
                "UPDATE fetches SET error='detail_unparsed' WHERE id=?",
                (proof.fetch_id,),
            )
            collector.db.conn.commit()
        raise
