"""Hold incomplete, owned Autokopen BMW search facts for ordinary detail planning."""

import re
from urllib.parse import urljoin, urlsplit

from .model_profiles import BMW_PROFILE, is_mercedes, own_country
from .parse import json_documents, walk

# The portable package owns the configured selector/context. Canonical research
# sources do not ship it, but still leave serialized configured records alone.
try:
    from .search_profile import is_configured
except ModuleNotFoundError as error:
    if error.name != __package__ + ".search_profile":
        raise

    def is_configured(item):
        return item.raw.get("model_profile") == "configured_vehicle"


_FIELDS = (
    "id",
    "slug",
    "title",
    "brand",
    "model",
    "variant",
    "vehicle_type",
    "fuel",
    "fuelSlug",
    "body_type",
    "bodyType",
    "market_country",
    "country_code",
    "countryCode",
    "country",
    "location",
    "address",
)


def search_records(body):
    """Index only native search envelopes; repeated IDs remain ambiguous."""
    records = {}
    for document in json_documents(body):
        for envelope in walk(document):
            if not isinstance(envelope.get("initialResults"), list) or not isinstance(
                envelope.get("meta"), dict
            ):
                continue
            for record in envelope["initialResults"]:
                if isinstance(record, dict) and isinstance(record.get("id"), str):
                    records.setdefault(record["id"], []).append(
                        {key: record[key] for key in _FIELDS if key in record}
                    )
    return records


def qualify_search(item):
    """Apply current own facts again after enrichment; never promote from old facts."""
    evidence = item.raw.get("autokopen_search")
    if (
        item.source != "autokopen"
        or not isinstance(evidence, dict)
        or is_mercedes(item)
        or is_configured(item)
        or item.raw.get("model_profile") not in (None, BMW_PROFILE)
        or item.scope not in {"unknown", "target", "needs_confirmation", "excluded"}
        or item.raw.get("field_conflicts")
        or item.raw.get("market_context_country") == "DE"
    ):
        return item
    record = evidence.get("record")
    if not isinstance(record, dict):
        return item
    identity = item.external_id
    conflicts = {}
    uri = urlsplit(str(evidence.get("dom_uri") or ""))
    if (
        not isinstance(identity, str)
        or not identity.isdigit()
        or record.get("id") != identity
        or evidence.get("dom_id") != identity
        or uri.scheme != "https"
        or uri.netloc not in {"autokopen.nl", "www.autokopen.nl"}
        or uri.query
        or uri.fragment
        or not re.fullmatch(r"/auto/detail/[^/]+-" + re.escape(identity), uri.path)
        or not isinstance(record.get("slug"), str)
        or urljoin(evidence["dom_uri"], "/auto/detail/" + record["slug"])
        != evidence["dom_uri"]
        or item.url != evidence["dom_uri"]
    ):
        conflicts["identity"] = {
            "native_id": record.get("id"),
            "native_slug": record.get("slug"),
            "dom_uri": evidence.get("dom_uri"),
        }
    brand = record.get("brand")
    if isinstance(brand, str) and brand.strip().upper() != "BMW":
        conflicts["brand"] = brand
    if record.get("vehicle_type") not in (None, "car"):
        conflicts["vehicle_type"] = record["vehicle_type"]
    country_claim = {
        key: record[key]
        for key in ("market_country", "country_code", "location", "address")
        if key in record
    }
    country_claims = [("own_record", *own_country(country_claim))]
    for key in ("countryCode", "country"):
        if record.get(key):
            country_claims.append((key, *own_country({"country_code": record[key]})))
    if any(
        country not in (None, "NL") or proof.get("kind") == "conflicting_own_country"
        for _, country, proof in country_claims
    ):
        conflicts["country"] = {
            "claims": [
                {"field": field, "country": country, "proof": proof}
                for field, country, proof in country_claims
            ]
        }
    values = [
        record.get(key)
        for key in ("title", "model", "variant", "body_type", "bodyType")
    ]
    values.append(evidence.get("dom_title"))
    # This is the current same-ID DOM card, never an inherited PDP description.
    values.append(item.raw.get("card_text"))
    text = " | ".join(value for value in values if isinstance(value, str)).lower()
    # Currency/energy-label typography is not an engine designation.
    text = re.sub(r"€\s*[\d.,]+", "", text)
    engines = re.findall(r"\b(\d{3})\s*(xe|i|d|e)\b", text)
    if any(engine != ("218", "i") for engine in engines):
        conflicts["engine"] = engines
    if re.search(
        r"\b(?:coup[eé]|cabrio(?:let)?|sedan|touring)\b|\bgran\s*[- ]?\s*tour", text
    ):
        conflicts["body"] = "explicit_other_body"
    model = record.get("model")
    if isinstance(model, str) and re.match(
        r"(?:[1345678]\s*[- ]?\s*serie(?:s)?|[xz]\s*\d)\b", model, re.IGNORECASE
    ):
        conflicts["model"] = model
    native_fuels = {key: record.get(key) for key in ("fuel", "fuelSlug")}
    if evidence.get("dom_fuel") in {"diesel", "hybrid", "electric"} or any(
        isinstance(value, str)
        and re.search(r"diesel|hybr|elektr|electric", value, re.IGNORECASE)
        for value in native_fuels.values()
    ):
        conflicts["fuel"] = native_fuels
    if conflicts:
        item.raw["nl_publication_conflicts"] = conflicts
        item.scope = "excluded"
    elif (
        brand == "BMW"
        and record.get("vehicle_type") == "car"
        and all(
            record.get(key) is None or isinstance(record[key], str)
            for key in (
                "title",
                "model",
                "variant",
                "fuel",
                "fuelSlug",
                "body_type",
                "bodyType",
            )
        )
        and not (
            re.search(r"\b218\s*i\b", text)
            and re.search(r"active\s*[- ]?\s*tour", text)
        )
    ):
        # BMW alone permits only a bounded own-detail attempt, not target eligibility.
        item.scope = "needs_confirmation"
    return item
