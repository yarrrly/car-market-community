"""Bounded latest confirmations of directly observed field values."""

import hashlib
import math
from datetime import UTC, datetime

from .text_encoding import json_text


def value_hash(value):
    def normalize(item):
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if not math.isfinite(item):
                raise ValueError("Non-finite field evidence")
            return float(item)
        if isinstance(item, dict):
            return {key: normalize(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(val) for val in item]
        return item

    content = json_text(normalize(value), sort_keys=True, allow_nan=False)
    return hashlib.sha256(content.encode()).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result.astimezone(UTC) if result.tzinfo else None


def verify_fields(conn, listing_id, payload):
    clock = datetime.now(UTC)
    raw = payload.get("raw") or {}
    for field, origin in (raw.get("field_evidence") or {}).items():
        if (
            not isinstance(origin, dict)
            or origin.get("inherited")
            or origin.get("legacy_origin_time_unverified")
        ):
            continue
        verified_at = origin.get("observed_at")
        incoming = timestamp(verified_at)
        if incoming is None or incoming > clock:
            continue
        existing = conn.execute(
            "SELECT verified_at FROM field_verifications WHERE listing_id=? AND field=?",
            (listing_id, field),
        ).fetchone()
        stored_time = timestamp(existing[0]) if existing else None
        if stored_time is not None and incoming < stored_time <= clock:
            continue
        conn.execute(
            "INSERT INTO field_verifications(listing_id,field,verified_at,fetch_id,value_hash,evidence_json) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(listing_id,field) DO UPDATE SET "
            "verified_at=excluded.verified_at,fetch_id=excluded.fetch_id,"
            "value_hash=excluded.value_hash,evidence_json=excluded.evidence_json",
            (
                listing_id,
                field,
                verified_at,
                origin.get("fetch_id"),
                value_hash(payload.get(field)),
                json_text(origin, sort_keys=True),
            ),
        )
