"""Accepted unchanged AutoUncle detail proofs for the Mercedes NL cache."""

import hashlib
import json
import math
import re
import sqlite3
from urllib.parse import urlsplit

from .bmw_detail_policy import detail_fact_conflicts
from .model_profiles import (
    MERCEDES_PROFILE,
    bound_scope,
    config_profile,
    own_country,
    qualify_mercedes,
)
from .parse import Listing
from .provenance import timestamp, value_hash

FIELDS = (
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
)
MODEL_FIELDS = (
    "model_brand",
    "model_family",
    "model_variant",
    "body_type",
    "generation",
    "first_registration",
    "model_year",
)


def _supplied(value):
    return value is not None and value != "" and value != [] and value != {}


def _valid_item(item):
    raw = item.raw
    missing = (raw.get("model_scope_evidence") or {}).get("missing", [])
    if (
        item.scope == "excluded"
        or raw.get("field_conflicts")
        or raw.get("nl_publication_conflicts")
        or any(
            reason in missing
            for reason in ("future_registration", "invalid_registration", "own_field_conflict")
        )
    ):
        return False
    for field in ("price", "mileage", "year"):
        value = getattr(item, field)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or (value < 0 if field == "mileage" else value <= 0)
        ):
            return False
    return True


def accepted_capture(collector, row, own, original, requested_url, body, clock):
    """Read existing exact accepted facts; never write or retime a cache hit.

    The original OPD-01 captures retained all directly verified native values
    although market observation dedup kept a different, older body SHA. This
    fallback is deliberately limited to that source/profile detail boundary.
    """
    try:
        scope = bound_scope(collector.db.conn)
        if (
            collector.country != "NL"
            or config_profile(collector.config) != MERCEDES_PROFILE
            or not scope
            or scope.get("country") != "NL"
            or scope.get("model_profile") != MERCEDES_PROFILE
            or scope.get("generation") != "C205"
            or scope.get("fuel") != "petrol"
            or collector.config.get("scope", {}).get("fuel") not in (None, "petrol")
            or own.source != original.source
            or own.source != row["source"]
            or own.source != "autouncle"
            or not _valid_item(own)
            or not _valid_item(original)
            or not collector._secondary_detail_eligible(original)
            or row["status"] != 200
            or row["error"]
            or not isinstance(row["raw_path"], str)
            or not row["raw_path"]
            or not isinstance(row["content_hash"], str)
            or re.fullmatch(r"[0-9a-f]{64}", row["content_hash"]) is None
            or hashlib.sha256(body.encode()).hexdigest() != row["content_hash"]
        ):
            return None
        urls = (
            requested_url,
            row["url"],
            row["final_url"],
            own.url,
            own.external_id,
            original.url,
            original.external_id,
        )
        if len(set(urls)) != 1:
            return None
        uri = urlsplit(requested_url)
        if (
            uri.scheme != "https"
            or uri.hostname not in {"autouncle.nl", "www.autouncle.nl"}
            or uri.port not in (None, 443)
            or uri.username is not None
            or uri.password is not None
            or uri.query
            or uri.fragment
            or re.fullmatch(r"/nl-nl/d/[1-9]\d*-[^/]+", uri.path) is None
        ):
            return None
        from .mercedes_nl_sources import _own_detail_uri

        if not _own_detail_uri(own.raw, requested_url) or own_country(own.raw)[0] != "NL":
            return None
        queued_country = own_country(original.raw)[0]
        if queued_country not in (None, "NL"):
            return None
        for raw in (own.raw, original.raw):
            if raw.get("model_profile") not in (None, MERCEDES_PROFILE) or any(
                raw.get(key) not in (None, "NL")
                for key in ("market_country", "market_context_country")
            ):
                return None
        observed = timestamp(row["fetched_at"])
        if (
            observed is None
            or clock is None
            or not 0
            <= (clock - observed).total_seconds()
            < collector.config.get("detail_cache_hours", 0) * 3600
        ):
            return None
        if own.year is not None and (
            own.year < 1886 or own.year > clock.year or int(own.year) != own.year
        ):
            return None
        for value in (
            collector.parser_refresh_since,
            collector.source_parser_refresh_since.get("autouncle"),
        ):
            cutoff = timestamp(value)
            if value is not None and (cutoff is None or cutoff > clock or observed < cutoff):
                return None
        current = collector.db.conn.execute(
            "SELECT l.id,l.url,l.scope,o.details_json,o.price_eur,o.mileage_km,o.year,o.transmission,o.fuel,o.kenteken,o.vin,o.observed_at,"
            "f.source AS prior_source,f.status AS prior_status,f.error AS prior_error,f.fetched_at AS prior_capture_clock "
            "FROM listings l JOIN latest_observations o ON o.listing_id=l.id "
            "LEFT JOIN fetches f ON f.id=o.fetch_id "
            "WHERE l.source=? AND l.external_id=?",
            (own.source, own.external_id),
        ).fetchone()
        if (
            not current
            or current["url"] != own.url
            or current["scope"] not in {"target", "needs_confirmation"}
        ):
            return None
        prior_time = timestamp(current["observed_at"])
        if (
            prior_time is None
            or prior_time > clock
            or timestamp(current["prior_capture_clock"]) != prior_time
            or current["prior_source"] != own.source
            or current["prior_status"] != 200
            or current["prior_error"]
        ):
            return None
        payload = json.loads(current["details_json"])
        raw = payload.get("raw") or {}
        if (
            payload.get("source") != own.source
            or payload.get("external_id") != own.external_id
            or payload.get("url") != own.url
            or raw.get("model_profile") != MERCEDES_PROFILE
            or own_country(raw)[0] != "NL"
            or raw.get("field_conflicts")
            or raw.get("nl_publication_conflicts")
            or payload.get("price_type") != own.price_type
            or not _valid_item(
                qualify_mercedes(Listing(**json.loads(current["details_json"])), "NL")
            )
        ):
            return None
        for field, column in (
            ("price", "price_eur"),
            ("mileage", "mileage_km"),
            ("year", "year"),
            ("transmission", "transmission"),
            ("fuel", "fuel"),
            ("kenteken", "kenteken"),
            ("vin", "vin"),
        ):
            if payload.get(field) != current[column]:
                return None
        for item in (own, original):
            facts = {
                field: getattr(item, field) for field in FIELDS if _supplied(getattr(item, field))
            }
            if item is original:
                # Search descriptions/images may be partial. The current native
                # VDP still has to match their complete persisted counterparts.
                facts = {
                    field: value
                    for field, value in facts.items()
                    if field not in {"title", "description", "images", "links", "seller", "city"}
                }
            facts.update(
                {
                    "raw." + key: item.raw[key]
                    for key in MODEL_FIELDS
                    if _supplied(item.raw.get(key))
                }
            )
            if detail_fact_conflicts(facts, payload):
                return None
        # Preserve the existing exact-body observation receipt after the same
        # owned/current-facts guards. A newer unobserved same-SHA fetch cannot
        # renew that accepted receipt's TTL or parser revision clock.
        accepted = collector._accepted_detail_capture(own.source, own.external_id, row)
        if accepted:
            capture = collector.db.conn.execute(
                "SELECT * FROM fetches WHERE id=?", (accepted["fetch_id"],)
            ).fetchone()
            accepted_time = timestamp(accepted["observed_at"])
            cutoffs = (
                timestamp(collector.parser_refresh_since),
                timestamp(collector.source_parser_refresh_since.get("autouncle")),
            )
            if (
                capture
                and capture["source"] == own.source
                and capture["url"] == capture["final_url"] == requested_url
                and capture["status"] == 200
                and not capture["error"]
                and capture["content_hash"] == row["content_hash"]
                and isinstance(capture["raw_path"], str)
                and capture["raw_path"]
                and accepted_time is not None
                and timestamp(capture["fetched_at"]) == accepted_time
                and 0
                <= (clock - accepted_time).total_seconds()
                < collector.config.get("detail_cache_hours", 0) * 3600
                and all(cutoff is None or accepted_time >= cutoff for cutoff in cutoffs)
            ):
                return {
                    "fetch_id": accepted["fetch_id"],
                    "observed_at": accepted["observed_at"],
                    "content_hash": row["content_hash"],
                    "listing_id": current["id"],
                    "proof_kind": "exact_native_observation",
                }
        fields = {field: getattr(own, field) for field in FIELDS if _supplied(getattr(own, field))}
        if len(fields) < 2:
            return None
        proofs = {
            proof["field"]: proof
            for proof in collector.db.conn.execute(
                "SELECT * FROM field_verifications WHERE listing_id=?", (current["id"],)
            )
        }
        for field, value in fields.items():
            proof = proofs.get(field)
            if (
                not proof
                or proof["fetch_id"] != row["id"]
                or timestamp(proof["verified_at"]) != observed
                or proof["value_hash"] != value_hash(value)
            ):
                return None
            origin = json.loads(proof["evidence_json"])
            if (
                not isinstance(origin, dict)
                or origin.get("source") != own.source
                or origin.get("external_id") not in (None, own.external_id)
                or origin.get("fetch_id") != row["id"]
                or timestamp(origin.get("observed_at")) != observed
                or origin.get("inherited")
                or origin.get("legacy_origin_time_unverified")
            ):
                return None
        return {
            "fetch_id": row["id"],
            "observed_at": row["fetched_at"],
            "content_hash": row["content_hash"],
            "listing_id": current["id"],
            "proof_kind": "exact_native_field_verifications",
        }
    except (AttributeError, KeyError, TypeError, ValueError, sqlite3.OperationalError):
        return None
