"""Semantic own-publication facts for the legacy Dutch BMW observation boundary."""

import re
from urllib.parse import urljoin

# These are native records or existing bound evidence, not a dealer-HQ inference.
_BINDING_FIELDS = (
    "id",
    "uuid",
    "url",
    "webPage",
    "vehicle",
    "location",
    "is_sold",
    "sold",
    "reserved",
    "availability_status",
    "market_country",
    "market_context_country",
    "country_evidence",
    "model_brand",
    "model_family",
    "model_variant",
    "publication_reference",
    "own_publication_reference",
    "own_identifiers",
    "native_vehicle",
    "nl_publication_conflicts",
    "autokopen_search",
)


def publication_facts(payload):
    """Use one projection in raw retention and normalized observation equality."""
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    return {
        **{
            key: payload.get(key)
            for key in ("source", "external_id", "url", "scope", "vin")
        },
        "native": {key: raw[key] for key in _BINDING_FIELDS if key in raw},
    }


def qualify_publication(item):
    """Guard current owned publication facts without borrowing another record."""
    if item.source == "autokopen":
        from .autokopen_scope import qualify_search

        return qualify_search(item)
    if item.source not in {"autoscout24", "autotrader"} or not isinstance(
        item.raw.get("vehicle"), dict
    ):
        return item
    from .mercedes_autoscout import _UUID, autoscout_identity
    from .publisher_identity import _route_uuid

    def own_uri_id(uri, *, publisher=False):
        if item.source == "autoscout24":
            return autoscout_identity(uri, "NL")
        # AutoTrader keeps its own URI and can declare the same AS publisher UUID.
        return _route_uuid(uri, "autotrader") or (
            _route_uuid(uri, "autoscout24") if publisher else None
        )

    # Older untyped projections do not assert this native UUID contract.
    if not re.fullmatch(_UUID, str(item.external_id), re.IGNORECASE):
        return item
    raw = item.raw
    conflicts = {}
    expected = str(item.external_id).lower()
    declared = raw.get("id")
    declared_uri = raw.get("url")
    native_uris = [
        (name, raw[name]) for name in ("url", "webPage") if raw.get(name) is not None
    ]
    if (
        not isinstance(declared, str)
        or declared.lower() != expected
        or own_uri_id(item.url) != expected
        # The legacy own-PDP parser binds native ID to its requested owned URI.
        # Optional URL fields may be absent, but every declared URI must agree.
        or any(
            not isinstance(uri, str)
            or own_uri_id(urljoin(item.url, uri), publisher=name == "webPage")
            != expected
            for name, uri in native_uris
        )
    ):
        conflicts["identity"] = {
            "expected_id": expected,
            "native_id": declared,
            "native_uri": declared_uri,
            "native_webPage": raw.get("webPage"),
            "parsed_uri": item.url,
        }
    location = raw.get("location")
    country = location.get("countryCode") if isinstance(location, dict) else None
    if country != "NL":
        conflicts["country"] = {"path": "location.countryCode", "value": country}
    model = raw["vehicle"].get("model")
    # The retained own BMW records declare model218/220 independently of motor text.
    # An explicit other engine/model claim is a disagreement, never a slug mapping.
    if isinstance(model, str) and re.fullmatch(
        r"(?:216|220|223|225|230)(?:\s*(?:i|d|e|xe))?|218\s*d",
        model.strip(),
        re.IGNORECASE,
    ):
        conflicts["model"] = {"path": "vehicle.model", "value": model}
    if conflicts:
        raw["nl_publication_conflicts"] = conflicts
        item.scope = (
            "needs_confirmation"
            if set(conflicts) == {"country"} and country in (None, "")
            else "excluded"
        )
    return item
