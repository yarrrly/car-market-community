"""Replay an owned native Carwow card route without accepting VDP fields."""

import re
from urllib.parse import parse_qs, urlsplit

from .fetch import Response
from .germany_enumeration import _blocked_response
from .germany_retained import _raw_body
from .mercedes_de_sources import _public
from .model_profiles import MERCEDES_PROFILE
from .provenance import timestamp

SOURCE = "carwow_de"


def owned_id(url, *, card=False):
    if not _public(SOURCE, url):
        return None
    parsed = urlsplit(url)
    if card:
        ids = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
        if parsed.path != "/stock_cars/deal-card" or len(ids) != 1:
            return None
        identity = ids[0]
    else:
        match = re.fullmatch(r"/deals/([a-f0-9]{24,32})", parsed.path)
        identity = match[1] if match else ""
    return identity if re.fullmatch(r"[a-f0-9]{24,32}", identity) else None


def request_matches(collector, source, url, original):
    scope = collector.config.get("scope", {})
    identity = owned_id(original.url, card=True)
    return bool(
        source == original.source == SOURCE
        and collector.country == scope.get("country") == "DE"
        and scope.get("model_profile") == MERCEDES_PROFILE
        and original.raw.get("model_profile", MERCEDES_PROFILE) == MERCEDES_PROFILE
        and identity == original.external_id
        and identity
        and original.raw.get("native_card_url", original.url) == original.url
        and (url == original.url or owned_id(url) == identity)
    )


def route_from_capture(collector, url, row, original, clock):
    identity = owned_id(url, card=True)
    observed = timestamp(row["fetched_at"]) if row else None
    if not (
        request_matches(collector, SOURCE, url, original)
        and identity
        and row
        and row["source"] == SOURCE
        and row["url"] == row["final_url"] == url
        and row["status"] == 200
        and row["error"] in (None, "detail_unparsed")
        and observed
        and 0
        <= (clock - observed).total_seconds()
        < collector.config.get("detail_cache_hours", 6) * 3600
        and not collector._detail_revision_refresh(SOURCE, row, clock)
    ):
        return None
    try:
        body = _raw_body(collector.db.path.parent, row["raw_path"], row["content_hash"])
        response = Response(row["url"], row["final_url"], 200, body, row["fetched_at"])
        if _blocked_response(response) or re.search(
            r"(?is)<title[^>]*>\s*(?:login|log in|sign in|anmelden)\b", body[:4096]
        ):
            return None
        page = collector._parse(SOURCE, body, row["final_url"])
        # The existing parser supplies the literal href. Never build an endpoint.
        if (
            page.listings
            or owned_id(page.search_filters.get("native_detail_url", "")) != identity
        ):
            return None
        response.secondary_fetch_id = row["id"]
        response.secondary_route_evidence = True
        return response
    except (ValueError, OSError):
        return None
