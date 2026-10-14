"""Read-only completeness and contradiction diagnostics, not proof of accuracy."""

import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime

from .db import now
from .provenance import timestamp, value_hash

FIELDS = {
    "price": "price_eur",
    "mileage": "mileage_km",
    "year": "year",
    "transmission": "transmission",
    "fuel": "fuel",
    "seller": "seller",
    "city": "city",
    "kenteken": "confirmed_kenteken",
    "vin": "confirmed_vin",
    "description": "description",
    "images": "images",
}


def _latest_verifications(conn, records):
    if (
        not records
        or not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_verifications'"
        ).fetchone()
    ):
        return {}
    result = defaultdict(dict)
    for row in conn.execute(
        "SELECT fv.*,f.source AS fetch_source,f.fetched_at,f.status,f.error,"
        "f.content_hash FROM field_verifications fv "
        "LEFT JOIN fetches f ON f.id=fv.fetch_id "
        "WHERE fv.listing_id IN (SELECT value FROM json_each(?))",
        (json.dumps([row["listing_id"] for row in records]),),
    ):
        if row["field"] in FIELDS:
            result[row["listing_id"]][row["field"]] = row
    return result


def _fresh_verification(record, field, item, details, clock, stale_hours):
    if (
        item["payload_error"]
        or item["country_proof_missing"]
        or any(
            type(details.get(key)) is not type(item[key])
            or details.get(key) != item[key]
            for key in ("source", "external_id")
        )
    ):
        return False
    try:
        proof = json.loads(record["evidence_json"])
    except (TypeError, ValueError):
        return False
    verified_at = timestamp(record["verified_at"])
    if (
        not isinstance(proof, dict)
        or verified_at is None
        or not 0 <= (clock - verified_at).total_seconds() <= stale_hours * 3600
        or timestamp(proof.get("observed_at")) != verified_at
        or timestamp(record["fetched_at"]) != verified_at
        or record["fetch_source"] != item["source"]
        or proof.get("source") != item["source"]
        or record["status"] != 200
        or record["error"] is not None
        or not isinstance(record["content_hash"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", record["content_hash"])
        or any(
            proof.get(flag, False) is not False
            for flag in ("inherited", "conflict", "legacy_origin_time_unverified")
        )
        or any(
            key in proof
            and (type(proof[key]) is not type(item[key]) or proof[key] != item[key])
            for key in ("listing_id", "external_id")
        )
        or (
            "fetch_id" in proof
            and (
                type(proof["fetch_id"]) is not int
                or proof["fetch_id"] != record["fetch_id"]
            )
        )
    ):
        return False
    value = item[FIELDS[field]]
    if value is None or value == "" or value == [] or field not in details:
        return False
    try:
        if (
            value_hash(value) != record["value_hash"]
            or value_hash(details[field]) != record["value_hash"]
        ):
            return False
    except (ValueError, TypeError, OverflowError):
        return False
    origin = item["field_evidence"].get(field, {})
    original_time = timestamp(origin.get("observed_at", item["observed_at"]))
    if original_time is not None and verified_at < original_time:
        observation_time = timestamp(item["observed_at"])
        if (
            original_time <= clock
            or observation_time is None
            or verified_at <= observation_time
        ):
            return False
    return True


def report(conn, config):
    german = config.get("scope", {}).get("country") == "DE"
    required_fields = ("price", "mileage", "year", "transmission", "fuel")
    if not german:
        required_fields += ("kenteken",)
    groups = defaultdict(list)
    vehicles = defaultdict(list)
    records = conn.execute(
        "SELECT m.*,v.vin AS confirmed_vin,o.details_json,o.fetch_id,f.url AS evidence_url,"
        "f.fetched_at,f.content_hash,f.raw_path FROM market_current m "
        "JOIN latest_observations o ON o.listing_id=m.listing_id "
        "JOIN vehicles v ON v.id=m.vehicle_id LEFT JOIN fetches f ON f.id=o.fetch_id "
        "WHERE m.scope IN ('target','needs_confirmation','model_conflict')"
    ).fetchall()
    clock = datetime.now(UTC)
    stale_hours = config.get("freshness_hours", config.get("interval_hours", 6) * 2 + 1)
    verifications = _latest_verifications(conn, records)
    for row in records:
        item = dict(row)
        item["payload_error"] = None
        try:
            details = json.loads(item["details_json"])
            if not isinstance(details, dict):
                raise TypeError("Expected an observation object")
        except (TypeError, ValueError):
            details = {}
            item["payload_error"] = "invalid_observation_payload"
        item.update(
            description=details.get("description"), images=details.get("images")
        )
        raw = details.get("raw", {})
        if not isinstance(raw, dict):
            raw = {}
            item["payload_error"] = "invalid_observation_raw"
        lineage = raw.get("field_evidence", {})
        item["field_evidence"] = (
            {key: value for key, value in lineage.items() if isinstance(value, dict)}
            if isinstance(lineage, dict)
            else {}
        )
        if item["field_evidence"] != lineage:
            item["payload_error"] = "invalid_field_lineage"
        item["detail_complete"] = bool(raw.get("detail_complete"))
        item["country_proof_missing"] = (
            config.get("scope", {}).get("model_profile") == "configured_vehicle"
            and raw.get("market_country") != config["scope"]["country"]
        ) or (german and raw.get("market_country") != "DE")
        seen = timestamp(item["last_seen"])
        age = (clock - seen).total_seconds() if seen else None
        item["invalid_timestamp"] = age is None or age < 0
        year = item["year"]
        item["invalid_calendar_year"] = year is not None and (
            not isinstance(year, (int, float))
            or not math.isfinite(year)
            or int(year) != year
            or not 1886 <= year <= clock.year + 1
        )
        item["stale"] = item["invalid_timestamp"] or age > stale_hours * 3600
        item["fresh_fields"] = set()
        for field, origin in item["field_evidence"].items():
            if not isinstance(origin, dict) or origin.get(
                "legacy_origin_time_unverified"
            ):
                continue
            proof_time = timestamp(origin.get("observed_at"))
            proof_age = (clock - proof_time).total_seconds() if proof_time else None
            if proof_age is not None and 0 <= proof_age <= stale_hours * 3600:
                item["fresh_fields"].add(field)
        # A same-value own capture can reconfirm a field without another market
        # observation. Keep its original lineage and use only bounded accepted
        # confirmations matching the latest value/publication and actual clock.
        for field, verification in verifications.get(item["listing_id"], {}).items():
            if _fresh_verification(
                verification, field, item, details, clock, stale_hours
            ):
                item["fresh_fields"].add(field)
        groups[item["source"]].append(item)
        if item["confirmed_kenteken"] or item["confirmed_vin"]:
            vehicles[item["vehicle_id"]].append(item)
    sources = []
    configured = {q["source"] for q in config.get("queries", [])}
    configured.update(c["source"] for c in config.get("channels", []))
    for source in sorted(configured | groups.keys()):
        ads = groups[source]
        fields = {}
        for field, column in FIELDS.items():
            if german and field == "kenteken":
                continue
            present = sum(
                a[column] is not None and a[column] != "" and a[column] != []
                for a in ads
            )
            fields[field] = {
                "present": present,
                "missing": len(ads) - present,
                "present_percent": round(100 * present / len(ads), 1) if ads else None,
                "inherited_values": sum(
                    bool(a["field_evidence"].get(field, {}).get("inherited"))
                    for a in ads
                ),
                "values_without_field_lineage": sum(
                    a[column] is not None
                    and a[column] != ""
                    and a[column] != []
                    and field not in a["field_evidence"]
                    for a in ads
                ),
                "fresh_confirmations": sum(
                    a[column] is not None
                    and a[column] != ""
                    and a[column] != []
                    and field in a["fresh_fields"]
                    for a in ads
                ),
            }
        health = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM source_health WHERE source=? ORDER BY query_key",
                (source,),
            )
        ]
        sources.append(
            {
                "source": source,
                "observed_ads": len(ads),
                "target_ads": sum(a["scope"] == "target" for a in ads),
                "unconfirmed_ads": sum(a["scope"] == "needs_confirmation" for a in ads),
                "model_conflict_ads": sum(a["scope"] == "model_conflict" for a in ads),
                "stale_ads": sum(a["stale"] for a in ads),
                "invalid_timestamp_ads": sum(a["invalid_timestamp"] for a in ads),
                "invalid_calendar_year_ads": sum(
                    a["invalid_calendar_year"] for a in ads
                ),
                "examples_invalid_calendar_year": [
                    {"listing_id": a["listing_id"], "url": a["url"], "year": a["year"]}
                    for a in ads
                    if a["invalid_calendar_year"]
                ][:5],
                "invalid_observation_payload_ads": sum(
                    bool(a["payload_error"]) for a in ads
                ),
                "removed_ads": sum(a["availability"] == "removed" for a in ads),
                "detail_complete_ads": sum(a["detail_complete"] for a in ads),
                "country_proof_missing_ads": sum(
                    a["country_proof_missing"] for a in ads
                ),
                "ads_with_retained_evidence": sum(
                    bool(a["content_hash"] and a["raw_path"]) for a in ads
                ),
                "retained_evidence_basis": "Stored raw path and hash metadata; file bytes are not verified by this diagnostic.",
                "fields": fields,
                "last_query_runs": health,
                "examples_missing_core_fields": [
                    {
                        "listing_id": a["listing_id"],
                        "url": a["url"],
                        "missing": [
                            field
                            for field in required_fields
                            if a[FIELDS[field]] is None
                        ],
                    }
                    for a in ads
                    if any(a[FIELDS[field]] is None for field in required_fields)
                ][:5],
            }
        )
    conflicts = []
    for vehicle_id, ads in vehicles.items():
        current = [a for a in ads if not a["stale"] and a["availability"] == "observed"]
        for field in ("year", "transmission", "fuel"):
            values = {a[field] for a in current if a[field] is not None}
            if len(values) > 1:
                conflicts.append(
                    {
                        "vehicle_id": vehicle_id,
                        "kenteken": ads[0]["confirmed_kenteken"],
                        "field": field,
                        "values": sorted(values),
                        "evidence": [
                            {
                                "source": a["source"],
                                "url": a["url"],
                                "value": a[field],
                                "observed_at": a["observed_at"],
                            }
                            for a in current
                            if a[field] is not None
                        ],
                    }
                )
    return {
        "generated_at": now(),
        "sources": sources,
        "identity_field_disagreements": conflicts,
        "stale_after_hours": stale_hours,
        "accuracy_verified": False,
        "limitation": "Filled fields are not verified facts. Historical observations may be stale. Source accuracy requires sampled original pages; disagreements require review, not automatic correction.",
    }
