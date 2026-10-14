"""Own publication state and its original clock survive incomplete later cards."""

import re


def availability(raw):
    status = str(raw.get("availability_status") or raw.get("status") or "").casefold()
    if raw.get("sold") is True or status in {"sold", "verkocht", "verkauft"}:
        return "sold"
    if raw.get("reserved") is True or status == "reserved":
        return "reserved"
    if (
        raw.get("availableNow") is True
        or raw.get("reserved") is False
        or status == "available"
    ):
        return "available"
    return None


def own_damage(payload):
    raw = payload.get("raw") or {}
    vehicle = raw.get("vehicle") if isinstance(raw.get("vehicle"), dict) else {}
    flags = [raw.get("damaged"), vehicle.get("isCurrentlyDamaged")]
    if any(flag is True for flag in flags):
        return True
    content = (
        str(payload.get("title") or "") + " " + str(payload.get("description") or "")
    ).casefold()
    pattern = r"\b(?:motorschaden|getriebeschaden|unfallfahrzeug|schadeauto|ongevalschade|niet\s+rijdbaar|nicht\s+fahrbereit|motor\s+(?:defect|kapot))\b"
    for match in re.finditer(pattern, content):
        before = content[max(0, match.start() - 28) : match.start()]
        if not re.search(
            r"\b(?:kein(?:e|en)?|geen|no|without|frei\s+von)\s+(?:\w+\s+){0,2}$", before
        ):
            return True
    return False if any(flag is False for flag in flags) else None


def retain_state(
    item,
    previous,
    observed_at,
    fetch_id,
    *,
    previous_observed_at=None,
    previous_fetch_id=None,
):
    previous_raw = previous.get("raw") or {}
    for key, value in (
        ("availability_evidence", availability(item.raw)),
        (
            "damage_evidence",
            own_damage(
                {"title": item.title, "description": item.description, "raw": item.raw}
            ),
        ),
    ):
        if value is not None:
            item.raw[key] = {
                "value": value,
                "observed_at": observed_at,
                "fetch_id": fetch_id,
                "source": item.source,
            }
        elif key in previous_raw:
            item.raw[key] = dict(previous_raw[key])
        elif previous_observed_at:
            old_value = (
                availability(previous_raw)
                if key == "availability_evidence"
                else own_damage(previous)
            )
            if old_value is not None:
                item.raw[key] = {
                    "value": old_value,
                    "observed_at": previous_observed_at,
                    "fetch_id": previous_fetch_id,
                    "source": item.source,
                }
