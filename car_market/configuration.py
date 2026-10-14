"""Validated, portable search configuration and immutable database binding."""

import json
import math
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .model_profiles import bound_scope, validate_profile_path
from .search_profile import GENERIC_PROFILE, fingerprint, normalized

ADAPTERS = {
    "autoscout24": {
        "country": "NL",
        "hosts": {"www.autoscout24.nl", "autoscout24.nl"},
        "pagination": "mercedes_autoscout_next",
    },
    "autoscout24_de": {
        "country": "DE",
        "hosts": {"www.autoscout24.de", "autoscout24.de"},
        "pagination": "mercedes_autoscout_next",
    },
    "marktplaats": {
        "country": "NL",
        "hosts": {"www.marktplaats.nl", "marktplaats.nl"},
        "pagination": "marktplaats",
        "paginations": ["marktplaats", "marktplaats_api"],
    },
    "gaspedaal": {
        "country": "NL",
        "hosts": {"www.gaspedaal.nl", "gaspedaal.nl"},
        "pagination": "page",
    },
    "autotrack": {
        "country": "NL",
        "hosts": {"www.autotrack.nl", "autotrack.nl"},
        "pagination": "autotrack",
    },
}


def strings(value, name, *, required=False):
    if (
        not isinstance(value, list)
        or (required and not value)
        or any(not isinstance(v, str) or not normalized(v) for v in value)
    ):
        raise ValueError(name + " must be a list of nonempty strings")


def validate(config):
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("Expected a schema_version=1 configuration")
    scope = config.get("scope", {})
    if (
        not isinstance(scope, dict)
        or scope.get("model_profile") != GENERIC_PROFILE
        or scope.get("country") not in {"NL", "DE"}
    ):
        raise ValueError("scope must specify configured_vehicle and country NL or DE")
    selector = scope.get("selector", {})
    if (
        not isinstance(selector, dict)
        or not isinstance(selector.get("make"), str)
        or not normalized(selector["make"])
    ):
        raise ValueError("selector.make is required")
    allowed = {
        "make",
        "make_aliases",
        "model_terms",
        "exclude_terms",
        "generation_terms",
        "fuels",
        "transmissions",
        "year_from",
        "year_to",
    }
    if set(selector) - allowed:
        raise ValueError(
            "Unknown selector fields: " + ", ".join(sorted(set(selector) - allowed))
        )
    groups = selector.get("model_terms")
    if not isinstance(groups, list) or not groups:
        raise ValueError("model_terms must contain at least one group of aliases")
    for group in groups:
        strings(group, "model_terms group", required=True)
    for key in [
        "make_aliases",
        "exclude_terms",
        "generation_terms",
        "fuels",
        "transmissions",
    ]:
        if key in selector:
            strings(selector[key], key)
    if set(selector.get("fuels", [])) - {
        "petrol",
        "diesel",
        "electric",
        "hybrid",
        "lpg",
        "hydrogen",
    }:
        raise ValueError("Unsupported fuel")
    if set(selector.get("transmissions", [])) - {"manual", "automatic"}:
        raise ValueError("Unsupported transmission")
    for key in ["year_from", "year_to"]:
        if selector.get(key) is not None and (
            type(selector[key]) is not int or not 1886 <= selector[key] <= 2100
        ):
            raise ValueError(key + " must be a valid year")
    if selector.get("year_from", 1886) > selector.get("year_to", 2100):
        raise ValueError("year_from exceeds year_to")
    for key, low, high in [
        ("workers", 1, 8),
        ("max_pages", 1, 200),
        ("detail_limit", 0, 1000),
        ("detail_batch_limit", 0, 1000),
        ("fast_detail_limit", 0, 1000),
        ("rdw_limit", 0, 1000),
    ]:
        if key in config and (
            type(config[key]) is not int or not low <= config[key] <= high
        ):
            raise ValueError(f"{key} must be an integer in {low}..{high}")
    for key, low in [
        ("interval_hours", 0.05),
        ("fast_interval_minutes", 1),
        ("request_delay", 0.5),
        ("request_timeout", 1),
        ("detail_cache_hours", 0),
        ("detail_error_backoff_hours", 0),
        ("freshness_hours", 0),
        ("source_error_backoff_minutes", 1),
    ]:
        value = config.get(key, low)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < low
        ):
            raise ValueError(f"{key} must be finite and at least {low}")
    queries = config.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("At least one search URL is required")
    keys = set()
    if not any(q.get("enabled", True) for q in queries if isinstance(q, dict)):
        raise ValueError("At least one query must be enabled")
    for query in queries:
        if not isinstance(query, dict):
            raise ValueError("Every query must be an object")
        if type(query.get("enabled", True)) is not bool:
            raise ValueError("query.enabled must be boolean")
        source = query.get("source")
        adapter = ADAPTERS.get(source)
        if not adapter or adapter["country"] != scope["country"]:
            raise ValueError("Unsupported source for this country: " + str(source))
        parsed = urlsplit(query.get("url", ""))
        if (
            parsed.scheme != "https"
            or parsed.hostname not in adapter["hosts"]
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError(
                "Search URL must belong to the configured source over HTTPS"
            )
        if source == "autoscout24_de" and "DE" in parse_qs(parsed.query).get("cy", []):
            raise ValueError(
                "AutoScout24 Germany URLs use cy=D, not cy=DE. Copy the site's actual search URL."
            )
        if source in {"autoscout24", "autoscout24_de"}:
            country_filter = "D" if scope["country"] == "DE" else "NL"
            if parse_qs(parsed.query).get("cy") != [country_filter]:
                raise ValueError(
                    f"AutoScout24 search URLs need exactly cy={country_filter} "
                    "for this workspace. Use init to normalize a copied search URL."
                )
        if "fallback_url" in query:
            fallback = urlsplit(query["fallback_url"] or "")
            if (
                source not in {"autoscout24", "autoscout24_de"}
                or fallback.scheme != "https"
                or fallback.hostname != parsed.hostname
                or fallback.username
                or fallback.password
                or fallback.port not in (None, 443)
                or parse_qs(fallback.query).get("cy") != parse_qs(parsed.query).get("cy")
            ):
                raise ValueError(
                    "fallback_url must be an AutoScout24 search on the same host and country"
                )
        key = query.get("key")
        if not isinstance(key, str) or not key or (source, key) in keys:
            raise ValueError("Queries need unique nonempty (source, key) pairs")
        keys.add((source, key))
        allowed_pagination = adapter.get("paginations", [adapter["pagination"]])
        if query.get("pagination") not in allowed_pagination:
            raise ValueError("Unsupported pagination for " + source)
        if source == "marktplaats":
            api = parsed.path == "/lrp/api/search"
            if api != (query.get("pagination") == "marktplaats_api"):
                raise ValueError("Marktplaats /lrp/api/search needs marktplaats_api pagination; HTML searches need marktplaats pagination")
            if not api:
                if parsed.query or parsed.fragment:
                    raise ValueError(
                        "Marktplaats HTML query/fragment filters are not certified. "
                        "Use a /q/.../ search URL without query/fragment, or "
                        "/lrp/api/search with verified API filters."
                    )
                page = re.search(r"/p/(\d+)/?$", parsed.path)
                if page and int(page.group(1)) != 1:
                    raise ValueError(
                        "Marktplaats HTML enumeration must start at page 1; "
                        "use init to normalize a copied URL"
                    )
            if api:
                params = parse_qs(parsed.query, keep_blank_values=True)
                if params.get("offset", ["0"]) != ["0"]:
                    raise ValueError("Marktplaats enumeration must start at offset=0")
                limit = params.get("limit", ["30"])
                if len(limit) != 1 or not limit[0].isdigit() or not 1 <= int(limit[0]) <= 100:
                    raise ValueError("Marktplaats API limit must be an integer in 1..100")
        if query.get("infer_missing", False) is not False:
            raise ValueError("Unobserved cars cannot be declared removed")
        if (
            "partition" in query
            or "request_body" in query
            or "inventory_contract" in query
        ):
            raise ValueError("Model-specific legacy query contracts are not portable")
    fast = config.get("fast_sources", [])
    if not isinstance(fast, list) or set(fast) - {q["source"] for q in queries}:
        raise ValueError("fast_sources must name configured sources")
    return config


def load(path):
    return validate(json.loads(Path(path).read_text(encoding="utf-8")))


def expected_scope(config):
    scope = config["scope"]
    return {**scope, "selector_hash": fingerprint(scope["selector"])}


def preflight(path, config):
    path = Path(path).resolve()
    scope = expected_scope(config)
    validate_profile_path(path, scope["country"], GENERIC_PROFILE)
    if path.exists():
        wal = Path(str(path) + "-wal")
        mode = (
            "mode=ro" if wal.exists() and wal.stat().st_size else "mode=ro&immutable=1"
        )
        conn = sqlite3.connect(path.as_uri() + "?" + mode, uri=True)
        try:
            old = bound_scope(conn)
            if old and old != scope:
                raise ValueError(
                    "Database belongs to another search. Use a separate --workspace"
                )
        finally:
            conn.close()


def bind(db, config):
    preflight(db.path, config)
    scope = expected_scope(config)
    db.conn.execute(
        "CREATE TABLE IF NOT EXISTS monitor_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
    )
    db.conn.execute(
        "INSERT OR REPLACE INTO monitor_state VALUES ('market_scope',?)",
        (json.dumps(scope, sort_keys=True),),
    )
    db.conn.execute("DROP VIEW IF EXISTS purchase_candidates")
    db.conn.execute("""CREATE VIEW purchase_candidates AS
      SELECT m.* FROM market_current m JOIN latest_observations o ON o.listing_id=m.listing_id
      WHERE m.scope='target' AND m.availability='observed' AND m.price_eur>0
      AND m.price_type IN ('advertised_price','FIXED')
      AND json_extract(o.details_json,'$.raw.price_warning') IS NULL""")
    db.conn.commit()
