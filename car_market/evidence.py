"""Keep enrichment evidence distinct from the latest search-card observation."""

import json

from .provenance import value_hash

FIELD_ALIASES = {"mileage_km": "mileage", "price_eur": "price"}


def _conflicts(raw):
    fields = raw.get("field_conflicts", {})
    if isinstance(fields, dict):
        return dict(fields)
    return (
        {field: None for field in fields if isinstance(field, str)}
        if isinstance(fields, list)
        else {}
    )


def retain_enrichment(item, previous, timestamp, fetch_id, query_key, run):
    old = json.loads(previous["details_json"]) if previous else {}
    supplied = item.raw.get("field_evidence", {})
    inherited = set()
    conflicts = _conflicts(item.raw)
    retained_conflicts = set()
    if previous:
        for field, alternatives in _conflicts(old.get("raw", {})).items():
            value_field = FIELD_ALIASES.get(field, field)
            value = getattr(item, value_field, None)
            origin = supplied.get(value_field, {})
            if field not in conflicts and (
                value is None or value == "" or value == [] or origin.get("inherited")
            ):
                conflicts[field] = alternatives
                retained_conflicts.add(field)
                if origin.get("inherited") and value_field in {
                    "year",
                    "mileage",
                    "price",
                    "fuel",
                    "transmission",
                    "seller",
                    "city",
                    "kenteken",
                    "vin",
                }:
                    setattr(item, value_field, None)
        if retained_conflicts:
            item.raw["field_conflicts"] = conflicts
    conflicted_fields = set(conflicts)
    conflicted_fields.update(FIELD_ALIASES.get(field, field) for field in conflicts)
    old_description = old.get("description") or ""
    if previous:
        if "description" not in conflicted_fields and (
            not item.description or (query_key and len(item.description) < len(old_description))
        ):
            item.description = old_description or item.description
            if old_description:
                inherited.add("description")
        for field in (
            "year",
            "mileage",
            "transmission",
            "fuel",
            "seller",
            "city",
            "kenteken",
            "vin",
        ):
            if field in conflicted_fields:
                continue
            if field == "transmission" and item.source in {"trovit", "nederlandmobiel", "marktnet"}:
                from .dom import explicit_gearbox

                old_text = old.get("raw", {}).get("card_text")
                if old_text and not explicit_gearbox(old_text):
                    # An earlier "auto" substring guess must not survive the corrected parser.
                    continue
            if getattr(item, field) is None and old.get(field) is not None:
                setattr(item, field, old[field])
                inherited.add(field)
        for field in ("images", "links"):
            if field not in conflicted_fields and not getattr(item, field) and old.get(field):
                setattr(item, field, old[field])
                inherited.add(field)
        if (
            "price" not in conflicted_fields
            and item.price is None
            and previous["run_id"] == run
            and previous["price_eur"] is not None
        ):
            item.price = previous["price_eur"]
            item.price_type = previous["price_type"]
            inherited.add("price")
        if "description" in inherited:
            old_raw = old.get("raw", {})
            for flag in (
                "detail_complete",
                "description_may_be_truncated",
                "description_truncated",
            ):
                if flag in old_raw:
                    item.raw[flag] = old_raw[flag]
                else:
                    item.raw.pop(flag, None)
            if (
                old_raw.get("description_may_be_truncated")
                or old_raw.get("description_truncated")
                or old_description.rstrip().endswith(("...", "…"))
            ):
                item.raw["detail_complete"] = False
    evidence = {}
    for field in (
        "title",
        "price",
        "mileage",
        "year",
        "transmission",
        "fuel",
        "seller",
        "city",
        "kenteken",
        "vin",
        "description",
        "images",
        "links",
    ):
        value = getattr(item, field)
        if value is None or value == "" or value == []:
            continue
        if field in inherited:
            origin = old.get("raw", {}).get("field_evidence", {}).get(field)
            evidence[field] = (
                dict(origin)
                if origin
                else {
                    "observed_at": previous["observed_at"],
                    "fetch_id": previous["fetch_id"],
                    "source": item.source,
                    "legacy_origin_time_unverified": True,
                }
            )
            evidence[field]["inherited"] = True
        else:
            evidence[field] = (
                dict(supplied[field])
                if isinstance(supplied.get(field), dict)
                else {
                    "observed_at": timestamp,
                    "fetch_id": fetch_id,
                    "source": item.source,
                    "inherited": False,
                }
            )
    item.raw["field_evidence"] = evidence
    for field in conflicts:
        value_field = FIELD_ALIASES.get(field, field)
        if field in retained_conflicts:
            origin = old.get("raw", {}).get("field_evidence", {}).get(value_field, {})
            # Old provenance may describe the previous value rather than the conflicting one.
            origin = (
                dict(origin)
                if origin.get("conflict") is True
                else {
                    "observed_at": previous["observed_at"],
                    "fetch_id": previous["fetch_id"],
                    "source": item.source,
                }
            )
            origin["inherited"] = True
        else:
            origin = {
                "observed_at": timestamp,
                "fetch_id": fetch_id,
                "source": item.source,
                "inherited": False,
            }
        evidence[value_field] = {
            **origin,
            "conflict": True,
            "conflict_hash": value_hash(conflicts[field]),
        }
    if item.scope in ("target", "needs_confirmation"):
        item.qualify()
    return old
