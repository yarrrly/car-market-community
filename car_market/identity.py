"""Identity identifiers must be structurally valid before exact matching."""

import re


def normalized_vin(value):
    text = str(value).strip().upper() if value is not None else ""
    return text if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", text) and len(set(text)) > 1 else None


def unsupported_legacy_registrations(conn):
    """Return exact audited records lacking own registration and RDW proof."""
    known = {
        "dcf8d73f-3576-4af1-ba1c-f389fe46c1d8": "RG856VTE",
        "797baaf5-3dab-4834-ac05-236a0289b637": "S841KFT",
        "f8840886-99d3-44a7-827f-3435b5dc7820": "JXZ45NTE",
    }
    result = []
    for identity, registration in known.items():
        row = conn.execute(
            "SELECT * FROM vehicles WHERE id=? AND kenteken=?", (identity, registration)
        ).fetchone()
        if row is None:
            continue
        members = conn.execute(
            "SELECT id,source,external_id FROM listings WHERE vehicle_id=?", (identity,)
        ).fetchall()
        own = conn.execute(
            "SELECT 1 FROM observations o JOIN listings l ON l.id=o.listing_id WHERE l.vehicle_id=? AND o.kenteken IS NOT NULL LIMIT 1",
            (identity,),
        ).fetchone()
        checks = conn.execute(
            "SELECT 1 FROM vehicle_checks WHERE vehicle_id=? LIMIT 1", (identity,)
        ).fetchone()
        if not members or any(member["source"] != "trovit" for member in members) or own or checks:
            continue
        result.append(
            {
                "original_vehicle": dict(row),
                "members": [dict(member) for member in members],
                "own_registration_observations": 0,
                "vehicle_checks": 0,
            }
        )
    return result
