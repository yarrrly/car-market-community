"""Isolated research profiles without changing canonical source facts."""

import json
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime
from pathlib import Path

BMW_PROFILE = "bmw_218i_active_tourer"
MERCEDES_PROFILE = "mercedes_c180_c205"
_ACTIVE = ContextVar("market_model_profile", default=None)


def config_profile(config):
    scope = config.get("scope", {})
    profile = scope.get("model_profile", BMW_PROFILE)
    if profile not in {BMW_PROFILE, MERCEDES_PROFILE, "configured_vehicle"}:
        raise ValueError("Unsupported model profile")
    if profile == MERCEDES_PROFILE:
        if scope.get("generation", "C205") != "C205":
            raise ValueError("Mercedes profile supports C205 only")
        if scope.get("transmissions", ["automatic"]) != ["automatic"]:
            raise ValueError("Mercedes profile supports automatic only")
        if scope.get("first_registration_from", "2016-01-01") != "2016-01-01":
            raise ValueError("Mercedes profile requires first registration from 2016")
    return profile


@contextmanager
def use_profile(profile, country):
    if profile not in {BMW_PROFILE, MERCEDES_PROFILE} or country not in {"NL", "DE"}:
        raise ValueError("Unsupported research scope")
    token = _ACTIVE.set((profile, country))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def is_mercedes(item=None):
    active = _ACTIVE.get()
    return bool(
        (active and active[0] == MERCEDES_PROFILE)
        or (item and item.raw.get("model_profile") == MERCEDES_PROFILE)
    )


def bound_scope(conn):
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='monitor_state'"
    ).fetchone():
        return None
    row = conn.execute(
        "SELECT value FROM monitor_state WHERE key='market_scope'"
    ).fetchone()
    return json.loads(row[0]) if row else None


def validate_profile_path(path, country, profile):
    from .germany import validate_market_path

    path = Path(path).resolve()
    validate_market_path(path, country)
    for expected, root in [
        (BMW_PROFILE, Path.home() / "src/first-car-nl"),
        (BMW_PROFILE, Path.home() / "src/first-car-de"),
        (MERCEDES_PROFILE, Path.home() / "src/mercedes-c180"),
    ]:
        if path.is_relative_to(root.resolve()) and profile != expected:
            raise ValueError("Database directory belongs to another model profile")
    for reserved_country in ("NL", "DE"):
        root = Path.home() / "src/mercedes-c180" / reserved_country.lower()
        if path.is_relative_to(root.resolve()) and country != reserved_country:
            raise ValueError("Database directory belongs to another country")
    if not path.exists():
        return
    wal = Path(str(path) + "-wal")
    mode = "mode=ro" if wal.exists() and wal.stat().st_size else "mode=ro&immutable=1"
    with sqlite3.connect(path.as_uri() + "?" + mode, uri=True) as conn:
        scope = bound_scope(conn)
        if scope and scope.get("country", "NL") != country:
            raise ValueError("Database belongs to another country")
        if scope and scope.get("model_profile", BMW_PROFILE) != profile:
            raise ValueError("Database belongs to another model profile")
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if (
            profile in {MERCEDES_PROFILE, "configured_vehicle"}
            and not scope
            and "listings" in tables
            and conn.execute("SELECT 1 FROM listings LIMIT 1").fetchone()
        ):
            raise ValueError("Cannot relabel populated legacy data as Mercedes")


def bind_profile(db, config):
    country = config.get("scope", {}).get("country", "NL")
    profile = config_profile(config)
    validate_profile_path(db.path, country, profile)
    old = bound_scope(db.conn)
    if old and old.get("model_profile", BMW_PROFILE) != profile:
        raise ValueError("Existing model binding is immutable")
    db.conn.execute(
        "CREATE TABLE IF NOT EXISTS monitor_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
    )
    scope = {"country": country, "model_profile": profile}
    if profile == MERCEDES_PROFILE:
        scope.update(
            generation="C205",
            first_registration_from="2016-01-01",
            transmissions=["automatic"],
            fuel="petrol",
        )
    db.conn.execute(
        "INSERT OR REPLACE INTO monitor_state VALUES ('market_scope',?)",
        (json.dumps(scope, sort_keys=True),),
    )
    if profile == MERCEDES_PROFILE:
        db.conn.execute(
            "CREATE TABLE IF NOT EXISTS model_scope_verifications(listing_id INTEGER PRIMARY KEY REFERENCES listings(id),scope TEXT NOT NULL,verified_at TEXT NOT NULL,fetch_id INTEGER REFERENCES fetches(id),evidence_json TEXT NOT NULL)"
        )
        db.conn.execute("DROP VIEW IF EXISTS purchase_candidates")
        db.conn.execute("""CREATE VIEW purchase_candidates AS SELECT m.* FROM market_current m
          JOIN latest_observations o ON o.listing_id=m.listing_id
          WHERE m.scope='target' AND m.availability='observed'
          AND m.transmission='automatic' AND m.year>=2016
          AND m.price_eur>0 AND m.price_type IN ('advertised_price','FIXED')
          AND json_extract(o.details_json,'$.raw.price_warning') IS NULL""")
    db.conn.commit()


def _text(value):
    if isinstance(value, dict):
        return str(
            value.get("formatted") or value.get("name") or value.get("label") or ""
        )
    return str(value or "")


def _dict(value):
    return value if isinstance(value, dict) else {}


def own_country(raw):
    explicit = []
    for key in ["market_country", "country_code"]:
        if raw.get(key):
            explicit.append(_text(raw[key]).upper())
    for parent in [
        raw.get("location"),
        raw.get("seller"),
        raw.get("dealer"),
        raw.get("address"),
    ]:
        if not isinstance(parent, dict):
            continue
        address = (
            parent.get("address") if isinstance(parent.get("address"), dict) else parent
        )
        country = (
            address.get("countryCode")
            or address.get("country_code")
            or address.get("country")
        )
        if country:
            if isinstance(country, dict):
                country = (
                    country.get("code")
                    or country.get("isoCode")
                    or country.get("countryCode")
                    or _text(country)
                )
            explicit.append(_text(country).upper())
    normalized = {
        "D": "DE",
        "DEU": "DE",
        "DEUTSCHLAND": "DE",
        "GERMANY": "DE",
        "NLD": "NL",
        "NEDERLAND": "NL",
        "NETHERLANDS": "NL",
    }
    countries = {normalized.get(x, x) for x in explicit}
    if len(countries) == 1:
        return countries.pop(), {"kind": "own_structured_country", "claims": explicit}
    if countries:
        return None, {"kind": "conflicting_own_country", "claims": explicit}
    for key in ["seller", "dealer", "dealerAddress", "sellerAddress", "address"]:
        parent = raw.get(key)
        if not isinstance(parent, dict):
            continue
        address = (
            parent.get("address") if isinstance(parent.get("address"), dict) else parent
        )
        postal = _text(
            address.get("postalCode")
            or address.get("postal_code")
            or address.get("postcode")
        )
        if re.fullmatch(r"[1-9]\d{3}\s?[A-Z]{2}", postal.upper()):
            return "NL", {
                "kind": "own_advertiser_nl_postal",
                "path": key,
                "postal_code": postal,
            }
    return None, {"kind": "country_unconfirmed"}


def evidence_clock_valid(proof, *, required=False):
    stamp = next(
        (
            proof.get(k)
            for k in ("checked_at", "observed_at", "fetched_at", "verified_at")
            if proof.get(k)
        ),
        None,
    )
    if stamp is None:
        return not required
    from .provenance import timestamp

    clock = timestamp(stamp)
    return clock is not None and clock <= datetime.now(UTC)


def generation_proof(item, body):
    raw = item.raw
    native = raw.get("vehicle") if isinstance(raw.get("vehicle"), dict) else {}
    registry = raw.get("rdw_model_evidence")
    if (
        isinstance(registry, dict)
        and registry.get("kenteken") == item.kenteken
        and evidence_clock_valid(registry)
    ):
        checked = registry.get("data") or {}
        name = str(checked.get("handelsbenaming", ""))
        wrong_brand = str(checked.get("merk", "")).upper() not in {
            "MERCEDES-BENZ",
            "MERCEDES BENZ",
        }
        declared_engines = {
            int(v) for v in re.findall(r"\bC\s*(\d{3})\b", name, re.IGNORECASE)
        }
        wrong_family = bool(
            re.search(
                r"\b(?:[ABEGSV]\s*\d{2,3}|CLA|CLS|GLC|GLE|GLS|CLE)\b",
                name,
                re.IGNORECASE,
            )
        )
        if (
            wrong_brand
            or wrong_family
            or any(v != 180 for v in declared_engines)
            or re.search(r"\bC\s*180\s*(?:d|cdi)\b", name, re.IGNORECASE)
        ):
            return "excluded", {
                "kind": "own_plate_rdw_model_conflict",
                "fetch_id": registry.get("fetch_id"),
            }
        # RDW D.2 is an approval type, not a Mercedes commercial generation.
        # Own C205 advertisements in retained NL captures are registered as type 204.
    generations = [
        raw.get("generation"),
        raw.get("model_generation"),
        native.get("generation"),
        native.get("modelGeneration"),
        native.get("modelSeries"),
        _dict(_dict(native.get("rawData")).get("classification")).get(
            "modelGeneration"
        ),
    ]
    if any(
        re.search(r"\b(?:C|W|A|S)?204\b", _text(g), re.IGNORECASE) for g in generations
    ):
        return "excluded", {"kind": "own_generation", "value": "204"}
    vin = item.vin or native.get("vin") or native.get("vehicleIdentificationNumber")
    if isinstance(vin, str) and re.fullmatch(
        r"(?:WDD|W1K)204[A-HJ-NPR-Z0-9]{11}", vin.upper()
    ):
        return "excluded", {"kind": "own_vin_series", "series": "204"}
    for g in generations:
        if re.search(r"\bC?205\b", _text(g), re.IGNORECASE) or (
            body and re.search(r"\bW205\b", _text(g), re.IGNORECASE)
        ):
            return "C205", {"kind": "own_generation", "value": _text(g)}
    vin = item.vin or native.get("vin") or native.get("vehicleIdentificationNumber")
    if isinstance(vin, str) and re.fullmatch(
        r"(?:WDD|W1K)2053[A-HJ-NPR-Z0-9]{10}", vin.upper()
    ):
        return "C205", {
            "kind": "own_vin_series",
            "value": vin.upper(),
            "series": "2053",
        }
    if isinstance(vin, str) and re.fullmatch(
        r"(?:WDD|W1K)204[A-HJ-NPR-Z0-9]{11}", vin.upper()
    ):
        return "excluded", {"kind": "own_vin_series", "series": "204"}
    own_heading = item.title
    if re.search(r"\bC205\b", own_heading, re.IGNORECASE):
        return "C205", {"kind": "own_title_generation", "value": "C205"}
    retained = raw.get("retained_generation_evidence")
    if (
        body
        and isinstance(retained, dict)
        and retained.get("value") == "C205"
        and retained.get("external_id") == item.external_id
        and retained.get("source") == item.source
        and evidence_clock_valid(retained, required=True)
    ):
        return "C205", {"kind": "retained_own_generation", **retained}
    # First-registration year alone cannot distinguish late registered C204 stock.
    return None, {"kind": "generation_unconfirmed"}


def registration_parts(value):
    """Compare only supplied date precision; never synthesize a registration day."""
    text = _text(value).strip()
    patterns = [
        r"(\d{4})-(\d{2})-(\d{2})(?:T.*)?",
        r"(\d{4})(\d{2})(\d{2})(?:T.*)?",
        r"(\d{4})-(\d{2})",
        r"(\d{4})",
    ]
    parts = None
    for pattern in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            parts = tuple(map(int, match.groups()))
            break
    if parts is None:
        match = re.fullmatch(r"(\d{2})[./](\d{4})", text)
        if match:
            parts = (int(match[2]), int(match[1]))
    if parts is None:
        match = re.fullmatch(r"(\d{2})[./](\d{2})[./](\d{4})", text)
        if match:
            parts = (int(match[3]), int(match[2]), int(match[1]))
    if parts:
        try:
            date(*((*parts, 1, 1)[:3] if len(parts) == 1 else (*parts, 1)[:3]))
            return parts
        except ValueError:
            pass
    return None


def qualify_mercedes(item, country=None):
    raw = item.raw
    active = _ACTIVE.get()
    context_country = country or (
        active[1] if active else raw.get("market_context_country")
    )
    raw["model_profile"] = MERCEDES_PROFILE
    if context_country:
        raw["market_context_country"] = context_country
    native = raw.get("vehicle") if isinstance(raw.get("vehicle"), dict) else {}
    brand = _text(raw.get("model_brand") or native.get("make"))
    text = " ".join(
        [
            item.title,
            _text(raw.get("model_family")),
            _text(raw.get("model_variant")),
            _text(native.get("model")),
            _text(native.get("motorTypeName")),
        ]
    )
    own_brand = bool(
        re.search(r"mercedes(?:[- ]benz)?", brand or item.title, re.IGNORECASE)
    )
    exact = bool(
        re.search(
            r"\bc\s*[- ]?\s*180\b|\bc[- ]?klasse(?:\s+coup[eé])?\s*180\b",
            text,
            re.IGNORECASE,
        )
    )
    other_family = bool(
        re.search(
            r"\b(?:[ABEGSV]\s*\d{2,3}|GL[ABCDE]|GLE|GLC|GLS|CLS|CLA|SLK|SLC|CLE)\b",
            text,
            re.IGNORECASE,
        )
    )
    motors = {
        int(m)
        for m in re.findall(
            r"\bc\s*[- ]?\s*(\d{3})\b|\bc[- ]?klasse(?:\s+coup[eé])?\s*(\d{3})\b",
            text,
            re.IGNORECASE,
        )
        for m in m
        if m
    }
    other = bool(
        re.search(
            r"\b(?:c\s*(?:200|220|250|300|350|400|43|63)|cla\s*180|cle\s*180|c\s*180\s*(?:d|cdi|kompressor))\b",
            text,
            re.IGNORECASE,
        )
    )
    body_text = " ".join(
        [
            item.title,
            _text(raw.get("body_type")),
            _text(raw.get("bodyType")),
            _text(native.get("bodyType")),
            _text(native.get("body")),
            _text(raw.get("carrosserie")),
        ]
    )
    coupe = bool(re.search(r"\bcoup[eé]\b|\bC205\b", body_text, re.IGNORECASE))
    other_body = bool(
        re.search(
            r"\bcabrio(?:let)?\b|\b(?:sedan|saloon|limousine|estate|stationwagen|combi|kombi|t-modell?|s205|a205)\b",
            body_text,
            re.IGNORECASE,
        )
    )
    own, geo_proof = own_country(raw)
    raw["country_evidence"] = geo_proof
    if own:
        raw["market_country"] = own
    else:
        raw.pop("market_country", None)
    generation, gen_proof = generation_proof(item, coupe)
    reasons = []
    excluded = (
        not own_brand
        or other
        or other_family
        or any(m not in {180, 204, 205} for m in motors)
        or other_body
        or generation == "excluded"
    )
    if own and context_country and own != context_country:
        excluded = True
        reasons.append("foreign_stock_country")
    if not exact:
        reasons.append("motor_unconfirmed")
    if not coupe:
        reasons.append("coupe_unconfirmed")
    if not generation:
        reasons.append("generation_unconfirmed")
    if not own:
        reasons.append("country_unconfirmed")
    if item.fuel in {"diesel", "hybrid", "electric"}:
        excluded = True
    elif item.fuel != "petrol":
        reasons.append("fuel_unconfirmed")
    if item.transmission == "manual":
        excluded = True
    elif item.transmission != "automatic":
        reasons.append("automatic_unconfirmed")
    registration = raw.get("own_first_registration") or raw.get("first_registration")
    if not registration:
        registry = raw.get("rdw_model_evidence")
        if (
            isinstance(registry, dict)
            and registry.get("kenteken") == item.kenteken
            and evidence_clock_valid(registry)
        ):
            registration = (registry.get("data") or {}).get("datum_eerste_toelating")
    registry = raw.get("rdw_model_evidence")
    rdw_parts = None
    if (
        isinstance(registry, dict)
        and registry.get("kenteken") == item.kenteken
        and evidence_clock_valid(registry)
    ):
        rdw_parts = registration_parts(
            (registry.get("data") or {}).get("datum_eerste_toelating")
        )
    if rdw_parts:
        current = datetime.now(UTC).date()
        if rdw_parts[0] < 2016:
            excluded = True
        if rdw_parts > (current.year, current.month, current.day)[: len(rdw_parts)]:
            reasons.append("future_registration")
    parts = registration_parts(registration)
    if parts:
        current = datetime.now(UTC).date()
        if parts[0] < 2016:
            excluded = True
        clock_parts = (current.year, current.month, current.day)[: len(parts)]
        if parts > clock_parts:
            reasons.append("future_registration")
        raw["registration_scope_evidence"] = {
            "original": registration,
            "precision": ["year", "month", "day"][len(parts) - 1],
        }
    elif registration:
        reasons.append("invalid_registration")
    year_only = raw.get("mercedes_nl_evidence") or {}
    if not parts and (
        year_only.get("construction_year")
        or year_only.get("model_year")
        or raw.get("model_year")
        or raw.get("production_year")
        or raw.get("built_year")
    ):
        reasons.append("first_registration_unconfirmed")
    if item.year is None:
        reasons.append("first_registration_unconfirmed")
    elif item.year < 2016:
        excluded = True
    elif item.year > datetime.now(UTC).date().year:
        reasons.append("future_registration")
    if raw.get("own_availability_exclusion") is True:
        excluded = True
    if raw.get("offer_role") == "wanted" or re.match(
        r"^(?:suche|gesucht|kaufgesuch|ankauf)\b", item.title, re.IGNORECASE
    ):
        excluded = True
    if raw.get("field_conflicts") and any(
        k in raw["field_conflicts"]
        for k in ["year", "transmission", "fuel", "generation", "body", "country"]
    ):
        reasons.append("own_field_conflict")
    item.scope = (
        "excluded" if excluded else "needs_confirmation" if reasons else "target"
    )
    raw["model_scope_evidence"] = {
        "profile": MERCEDES_PROFILE,
        "generation": gen_proof,
        "missing": reasons,
    }
    if context_country == "DE":
        from .germany import classify_german_price

        item.kenteken = None
        classify_german_price(item)
    return item
