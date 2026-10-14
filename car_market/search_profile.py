"""Declarative vehicle selection using the publication's own fields."""

import hashlib
import json
import re
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

GENERIC_PROFILE = "configured_vehicle"
_SEARCH = ContextVar("configured_vehicle_search", default=None)


def normalized(value):
    if isinstance(value, dict):
        value = value.get("name") or value.get("formatted") or value.get("label") or ""
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    return re.sub(r"(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])", " ", value)


def contains(text, term):
    return bool(
        re.search(
            r"(?<![a-z0-9])" + re.escape(normalized(term)) + r"(?![a-z0-9])", text
        )
    )


def fingerprint(selector):
    return hashlib.sha256(
        json.dumps(selector, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@contextmanager
def search_context(scope):
    token = _SEARCH.set(scope)
    try:
        yield
    finally:
        _SEARCH.reset(token)


def is_configured(item=None):
    return _SEARCH.get() is not None or bool(
        item and item.raw.get("model_profile") == GENERIC_PROFILE
    )


def qualify(item, scope=None):
    scope = scope or _SEARCH.get()
    if scope is None:
        raise ValueError(
            "A configured vehicle must be qualified with its bound selector"
        )
    selector = scope["selector"]
    raw = item.raw
    raw["model_profile"] = GENERIC_PROFILE
    raw["market_context_country"] = scope["country"]
    from .model_profiles import own_country

    country, geo = own_country(raw)
    raw["country_evidence"] = geo
    if country:
        raw["market_country"] = country
    else:
        raw.pop("market_country", None)
    if scope["country"] == "DE":
        if item.kenteken:
            raw["registration_identifier"] = item.kenteken
        item.kenteken = None
    # Descriptions may advertise other cars; they cannot prove model or generation.
    vehicle = raw.get("vehicle") if isinstance(raw.get("vehicle"), dict) else {}
    brand = (
        raw.get("model_brand")
        or vehicle.get("make")
        or raw.get("brand")
        or raw.get("manufacturer")
    )
    title = normalized(item.title)
    model = normalized(
        " ".join(
            normalized(v)
            for v in [
                item.title,
                raw.get("model_family"),
                raw.get("model_variant"),
                raw.get("model"),
                vehicle.get("model"),
                vehicle.get("motorTypeName"),
                vehicle.get("variant"),
            ]
        )
    )
    brands = [selector["make"], *selector.get("make_aliases", [])]
    reasons, excluded = [], False
    if brand:
        if normalized(brand) not in {normalized(x) for x in brands}:
            excluded = True
            reasons.append("different_make")
    elif not any(contains(title, b) for b in brands):
        reasons.append("make_unconfirmed")
    groups = selector["model_terms"]
    for group in groups:
        if not any(contains(model, term) for term in group):
            reasons.append("model_unconfirmed")
            break
    if any(contains(model, t) for t in selector.get("exclude_terms", [])):
        excluded = True
        reasons.append("excluded_variant")
    if country and country != scope["country"]:
        excluded = True
        reasons.append("foreign_stock_country")
    elif not country:
        reasons.append("country_unconfirmed")
    if raw.get("offer_role") == "wanted" or re.match(
        r"^(suche|gesucht|kaufgesuch|ankauf|gezocht|wanted)\b", title
    ):
        excluded = True
        reasons.append("wanted_advertisement")
    for field, key in [("fuel", "fuels"), ("transmission", "transmissions")]:
        allowed = selector.get(key, [])
        value = getattr(item, field)
        if allowed and value is None:
            reasons.append(field + "_unconfirmed")
        elif allowed and value not in allowed:
            excluded = True
            reasons.append("different_" + field)
    for key, compare in [
        ("year_from", lambda a, b: a < b),
        ("year_to", lambda a, b: a > b),
    ]:
        limit = selector.get(key)
        if limit is not None:
            if item.year is None:
                reasons.append("year_unconfirmed")
            elif compare(item.year, limit):
                excluded = True
                reasons.append("outside_year_range")
    if item.year and item.year > datetime.now(UTC).year:
        reasons.append("future_year")
    generation = selector.get("generation_terms", [])
    if generation:
        classification = (vehicle.get("rawData") or {}).get("classification") or {}
        own = normalized(
            " ".join(
                normalized(v)
                for v in [
                    item.title,
                    raw.get("generation"),
                    raw.get("model_generation"),
                    vehicle.get("modelGeneration"),
                    classification.get("modelGeneration"),
                ]
            )
        )
        if not any(contains(own, term) for term in generation):
            reasons.append("generation_unconfirmed")
    item.scope = (
        "excluded" if excluded else "needs_confirmation" if reasons else "target"
    )
    raw["model_scope_evidence"] = {
        "selector_hash": fingerprint(selector),
        "reasons": sorted(set(reasons)),
        "kind": "own_publication_fields",
        "status": item.scope,
    }
    return item
