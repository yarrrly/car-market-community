"""Read-only vehicle cards; identity is exclusively the stored vehicle_id."""

import json
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from .provenance import timestamp as verification_time
from .provenance import value_hash

SCALARS = {
    "title": "title",
    "year": "year",
    "mileage_km": "mileage",
    "fuel": "fuel",
    "transmission": "transmission",
    "seller": "seller",
    "city": "city",
}
STABLE = {"year", "fuel", "transmission"}
PRICE_TYPES = {"advertised_price", "FIXED"}
# JSON arrows preserve booleans as booleans (detail_complete uses `is True`).
# The CASE is lazy: JSON functions never see malformed provider payloads.
DETAILS_JSON = """CASE WHEN json_valid(o.details_json) THEN json_object(
    'description', o.details_json -> '$.description',
    'images', o.details_json -> '$.images',
    'raw', json_object(
        'field_evidence', o.details_json -> '$.raw.field_evidence',
        'field_conflicts', o.details_json -> '$.raw.field_conflicts',
        'description_may_be_truncated', o.details_json -> '$.raw.description_may_be_truncated',
        'description_truncated', o.details_json -> '$.raw.description_truncated',
        'detail_complete', o.details_json -> '$.raw.detail_complete',
        'historical_only', o.details_json -> '$.raw.historical_only',
        'price_warning', o.details_json -> '$.raw.price_warning'
    )) ELSE '{{}}' END"""
SELECT_AD = (
    """SELECT l.*,l.id AS listing_id,o.id AS observation_id,
    o.observed_at,o.fetch_id,"""
    + DETAILS_JSON
    + """ AS details_json,o.title,o.year,o.mileage_km,
    o.fuel,o.transmission,o.seller,o.city,o.price_eur,o.price_type
    FROM listings l LEFT JOIN {observations} o ON o.listing_id=l.id"""
)


def _rows(conn, sql, parameters=()):
    cursor = conn.execute(sql, parameters)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _time(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return (
        result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
    )


def _json(value):
    try:
        result = json.loads(value or "{}", parse_constant=lambda _: None)
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _valid(field, value):
    if field in {"year", "mileage_km"}:
        return (
            _finite(value)
            and value == int(value)
            and (1886 <= value <= 2100 if field == "year" else value >= 0)
        )
    return isinstance(value, str) and bool(value.strip())


def _fresh(value, cutoff, clock):
    timestamp = _time(value)
    return timestamp is not None and cutoff <= timestamp <= clock


def _live(row, cutoff, clock):
    raw = row["details"].get("raw")
    return (
        row["availability"] == "observed"
        and row["scope"] != "historical_only"
        and not (isinstance(raw, dict) and raw.get("historical_only"))
        and _fresh(row["last_seen"], cutoff, clock)
    )


def _origin(row, field, fetches, cutoff, clock, *, conflict=False):
    raw = row["details"].get("raw")
    raw = raw if isinstance(raw, dict) else {}
    evidence = raw.get("field_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    supplied = evidence.get(field)
    supplied = (
        supplied
        if isinstance(supplied, dict)
        and (not conflict or supplied.get("conflict") is True)
        else None
    )
    supplied = supplied or {}
    verification = (
        row.get("verifications", {}).get(field) if row.get("is_latest") else None
    )
    if verification and bool(verification["evidence"].get("conflict")) == conflict:
        column = {"price": "price_eur", "mileage": "mileage_km"}.get(field, field)
        value = row.get(column) if column in row else row["details"].get(field)
        try:
            matches = value_hash(value) == verification["value_hash"]
            if conflict:
                conflict_fields = _raw_conflicts(raw)
                conflict_field = field if field in conflict_fields else column
                matches = (
                    matches
                    and conflict_field in conflict_fields
                    and verification["evidence"].get("conflict_hash")
                    == value_hash(conflict_fields[conflict_field])
                )
        except (ValueError, TypeError, OverflowError):
            matches = False
        original_time = _time(
            supplied.get("observed_at") if supplied else row["observed_at"]
        )
        if matches and (
            original_time is None
            or (
                original_time > clock
                and verification["verified_at"] > _time(row["observed_at"])
            )
            or verification["verified_at"] >= original_time
        ):
            # References stay with the original fact; new confirmation changes only the date and fetch.
            confirmed = verification["evidence"]
            supplied = {
                **supplied,
                "observed_at": confirmed["observed_at"],
                "fetch_id": confirmed.get("fetch_id", verification["fetch_id"]),
                "inherited": False,
                "legacy_origin_time_unverified": False,
            }
    fetch_id = supplied.get("fetch_id", row["fetch_id"])
    if not isinstance(fetch_id, int):
        fetch_id = None
    fetch = fetches.get(fetch_id, {})
    # Supplied lineage never borrows the time of a later search-card refresh.
    timestamp = supplied.get("observed_at") if supplied else row["observed_at"]
    if (
        timestamp is None
        and supplied
        and supplied.get("fetch_id") is not None
        and not supplied.get("legacy_origin_time_unverified")
    ):
        timestamp = fetch.get("fetched_at")
    return {
        "source": supplied.get("source", row["source"]),
        "listing_id": supplied.get("listing_id", row["listing_id"]),
        "external_id": supplied.get("external_id", row["external_id"]),
        "url": supplied.get("url", row["url"]),
        "fetch_id": fetch_id,
        "observed_at": timestamp,
        "fetched_at": fetch.get("fetched_at"),
        "fetch_url": fetch.get("url"),
        "observation_id": row["observation_id"],
        "inherited": bool(supplied.get("inherited")),
        "legacy_origin_time_unverified": bool(
            supplied.get("legacy_origin_time_unverified")
        ),
        "fresh": not supplied.get("legacy_origin_time_unverified")
        and _fresh(timestamp, cutoff, clock),
        "active": _live(row, cutoff, clock),
    }


def _key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _append_unique(records, record, positions=None):
    def identity(item):
        origin = item.get("origin", item)
        # Repeated inheritance points at one statement, not new field evidence.
        return _key(
            {
                key: value
                for key, value in origin.items()
                if key not in {"observation_id", "inherited"}
            }
        ), item.get("value")

    incoming = identity(record)
    if positions is None:
        # Preserve the helper's two-argument behavior for standalone callers.
        index = next(
            (i for i, existing in enumerate(records) if identity(existing) == incoming),
            None,
        )
    else:
        index = positions.get(incoming)
    if index is not None:
        old_origin = records[index].get("origin", records[index])
        new_origin = record.get("origin", record)
        if old_origin.get("inherited") and not new_origin.get("inherited"):
            records[index] = record
        return
    if positions is not None:
        positions[incoming] = len(records)
    records.append(record)


def _statement_key(statement):
    origin = statement["origin"]
    return (
        _time(origin["observed_at"]) or datetime.min.replace(tzinfo=UTC),
        not origin["inherited"],
        str(origin["source"]),
        str(origin["listing_id"]),
    )


def _select(field, statements, conflicts):
    fresh = [s for s in statements if s["origin"]["active"] and s["origin"]["fresh"]]
    current_conflicts = [
        c for c in conflicts if c["origin"]["active"] and c["origin"]["fresh"]
    ]
    blocking = current_conflicts or (conflicts if not fresh else [])
    if blocking:
        return None, [], blocking
    if field in STABLE and len({_key(s["value"]) for s in fresh}) > 1:
        return None, [], [{"kind": "contradictory_statements", "statements": fresh}]
    # Mileage is chronological, not a vote or the largest odometer number.
    pool = statements if field == "mileage_km" else (fresh or statements)
    if not pool:
        return None, [], []
    selected = max(pool, key=_statement_key)
    if field == "mileage_km":
        latest = [
            s
            for s in pool
            if _time(s["origin"]["observed_at"])
            == _time(selected["origin"]["observed_at"])
        ]
        if len({_key(s["value"]) for s in latest}) > 1:
            return (
                None,
                [],
                [{"kind": "contradictory_statements", "statements": latest}],
            )
    origins = [
        s["origin"]
        for s in pool
        if s["value"] == selected["value"]
        and (
            field != "mileage_km"
            or _time(s["origin"]["observed_at"])
            == _time(selected["origin"]["observed_at"])
        )
    ]
    return selected["value"], origins, []


def _raw_conflicts(raw):
    conflicts = raw.get("field_conflicts", {})
    if isinstance(conflicts, dict):
        return conflicts
    if isinstance(conflicts, list):
        return {field: None for field in conflicts if isinstance(field, str)}
    return {}


def _media(rows, fetches, cutoff, clock):
    descriptions, photos, evidence = {}, {}, {}
    reference_keys = {}
    description_ranks = {}

    def intern(origin):
        key = _key(
            {
                name: value
                for name, value in origin.items()
                if name not in {"observation_id", "inherited"}
            }
        )
        reference = reference_keys.get(key)
        if reference is None:
            reference = "e" + str(len(evidence) + 1)
            reference_keys[key] = reference
            evidence[reference] = origin
        elif evidence[reference]["inherited"] and not origin["inherited"]:
            evidence[reference] = origin
        return reference

    for row in rows:
        detail = row["details"]
        raw = detail.get("raw")
        raw = raw if isinstance(raw, dict) else {}
        conflicts = _raw_conflicts(raw)
        text = detail.get("description")
        if isinstance(text, str) and text.strip() and "description" not in conflicts:
            text = text.strip()
            # Whitespace differences do not create duplicate seller texts.
            key = " ".join(text.split())
            truncated = bool(
                raw.get("description_may_be_truncated")
                or raw.get("description_truncated")
                or text.rstrip().endswith(("...", "…"))
            )
            origin = _origin(row, "description", fetches, cutoff, clock)
            origin.update(
                detail_complete=raw.get("detail_complete") is True and not truncated,
                description_may_be_truncated=truncated,
            )
            item = descriptions.setdefault(key, {"text": text, "evidence_ids": {}})
            item["evidence_ids"][intern(origin)] = None
            rank = (
                origin["active"] and origin["fresh"],
                origin["active"],
                origin["fresh"],
                _time(origin["observed_at"]) or datetime.min.replace(tzinfo=UTC),
                row["is_latest"],
                not origin["inherited"],
            )
            description_ranks[key] = max(rank, description_ranks.get(key, rank))
        images = detail.get("images")
        if isinstance(images, list) and "images" not in conflicts:
            origin = _origin(row, "images", fetches, cutoff, clock)
            for url in images:
                if isinstance(url, str) and url.strip():
                    url = url.strip()
                    item = photos.setdefault(url, {"url": url, "evidence_ids": {}})
                    item["evidence_ids"][intern(origin)] = None
    texts = list(descriptions.values())
    for item in texts:
        item["evidence_ids"] = list(item["evidence_ids"])
        origins = [evidence[reference] for reference in item["evidence_ids"]]
        # A later assessment of one publication replaces its previous completeness,
        # while the original evidence remains in media_evidence.
        publications = {}
        for origin in origins:
            publication = _key([origin["source"], origin["listing_id"]])
            rank = (
                _time(origin["observed_at"]) or datetime.min.replace(tzinfo=UTC),
                not origin["inherited"],
                origin["observation_id"] or 0,
            )
            if publication not in publications or rank > publications[publication][0]:
                publications[publication] = rank, origin
        current_origins = [pair[1] for pair in publications.values()]
        item["detail_complete"] = any(o["detail_complete"] for o in current_origins)
        item["description_may_be_truncated"] = all(
            o["description_may_be_truncated"] for o in current_origins
        )
    texts.sort(key=lambda d: (-len(d["text"].split()), -len(d["text"]), d["text"]))
    texts.sort(
        key=lambda d: description_ranks[" ".join(d["text"].split())], reverse=True
    )
    for item in photos.values():
        item["evidence_ids"] = list(item["evidence_ids"])
    return texts, list(photos.values()), evidence


def _card(vehicle, latest, history, fetches, checks, cutoff, clock):
    card = {
        **vehicle,
        "identity_status": "confirmed"
        if vehicle["kenteken"] or vehicle["vin"]
        else "provisional",
        **dict.fromkeys(SCALARS),
        "field_evidence": {field: [] for field in SCALARS},
        "field_alternatives": {field: [] for field in SCALARS},
        "field_conflicts": {field: [] for field in SCALARS},
        "offers": [],
        "checks": checks,
        "listing_count": len(latest),
        "active_offer_count": 0,
        "is_live": False,
        "price_min_eur": None,
        "price_max_eur": None,
        "description": None,
        "description_evidence": [],
        "media_evidence": {},
        "detail_complete": False,
        "description_may_be_truncated": False,
        "descriptions": [],
        "photos": [],
        "images": [],
    }
    statements, conflicts = defaultdict(list), defaultdict(list)
    statement_positions = defaultdict(dict)
    for row in latest + history:
        raw = row["details"].get("raw")
        raw = raw if isinstance(raw, dict) else {}
        field_conflicts = _raw_conflicts(raw)
        for field, detail_field in SCALARS.items():
            if not row["is_latest"] and field != "mileage_km":
                continue
            conflict_field = detail_field if detail_field in field_conflicts else field
            if conflict_field in field_conflicts:
                if row["is_latest"]:
                    conflicts[field].append(
                        {
                            "kind": "source_field_conflict",
                            "alternatives": field_conflicts[conflict_field],
                            "origin": _origin(
                                row, detail_field, fetches, cutoff, clock, conflict=True
                            ),
                        }
                    )
                continue
            value = row[field]
            if not _valid(field, value):
                continue
            statement = {
                "value": value,
                "origin": _origin(row, detail_field, fetches, cutoff, clock),
            }
            _append_unique(statements[field], statement, statement_positions[field])
    for field in SCALARS:
        value, origins, blocked = _select(field, statements[field], conflicts[field])
        card[field] = value
        card["field_evidence"][field] = origins
        card["field_alternatives"][field] = statements[field]
        # Nonblocking old conflicts remain visible for review.
        card["field_conflicts"][field] = blocked or conflicts[field]
    prices = []
    for row in latest:
        raw = row["details"].get("raw")
        raw = raw if isinstance(raw, dict) else {}
        conflict_fields = _raw_conflicts(raw)
        price_origin = _origin(row, "price", fetches, cutoff, clock)
        price = row["price_eur"] if _finite(row["price_eur"]) else None
        eligible = bool(
            _live(row, cutoff, clock)
            and price_origin["fresh"]
            and price is not None
            and price > 0
            and row["price_type"] in PRICE_TYPES
            and "price" not in conflict_fields
            and "price_eur" not in conflict_fields
            and not raw.get("historical_only")
            and not raw.get("price_warning")
        )
        offer = {
            key: row[key]
            for key in (
                "listing_id",
                "source",
                "external_id",
                "url",
                "first_seen",
                "last_seen",
                "availability",
                "scope",
                "observation_id",
                "observed_at",
                "fetch_id",
                "price_type",
            )
        }
        offer.update(
            price_eur=price,
            is_live=_live(row, cutoff, clock),
            current_price=eligible,
            price_evidence=price_origin,
            price_warning=raw.get("price_warning"),
            field_conflicts=conflict_fields,
        )
        card["offers"].append(offer)
        if eligible:
            prices.append(price)
    card["active_offer_count"] = sum(o["is_live"] for o in card["offers"])
    card["is_live"] = bool(card["active_offer_count"])
    if prices:
        card["price_min_eur"], card["price_max_eur"] = min(prices), max(prices)
    descriptions, photos, media_evidence = _media(
        latest + history, fetches, cutoff, clock
    )
    card.update(
        descriptions=descriptions,
        photos=photos,
        images=[p["url"] for p in photos],
        media_evidence=media_evidence,
    )
    if descriptions:
        selected = descriptions[0]
        card.update(
            description=selected["text"],
            description_evidence=[
                media_evidence[reference] for reference in selected["evidence_ids"]
            ],
            detail_complete=selected["detail_complete"],
            description_may_be_truncated=selected["description_may_be_truncated"],
        )
    return card


def _catalog(conn, config, vehicle_id=None):
    config = config or {}
    clock_value = config.get("as_of", config.get("now"))
    clock = _time(clock_value) if clock_value is not None else datetime.now(UTC)
    hours = config.get("freshness_hours", config.get("interval_hours", 6) * 2 + 1)
    if clock is None or not _finite(hours) or hours < 0:
        raise ValueError(
            "as_of must be an ISO timestamp; freshness_hours must be nonnegative"
        )
    cutoff = clock - timedelta(hours=hours)
    parameters = () if vehicle_id is None else (vehicle_id,)
    vehicles = _rows(
        conn,
        """SELECT id,kenteken,vin,created_at,EXISTS(
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_verifications'
        ) AS has_verifications FROM vehicles"""
        + (" WHERE id=?" if parameters else "")
        + " ORDER BY id",
        parameters,
    )
    if not vehicles:
        return []
    has_verifications = vehicles[0]["has_verifications"]
    for vehicle in vehicles:
        vehicle.pop("has_verifications")
    clause = " WHERE l.vehicle_id=?" if parameters else ""
    latest_sql = SELECT_AD.format(observations="latest_observations") + clause
    if has_verifications:
        # One bulk JOIN instead of separate evidence reads for each field/listing.
        latest_sql = (
            "SELECT current.*,proof.verifications_json FROM ("
            + latest_sql
            + """
        ) current LEFT JOIN (
          SELECT fv.listing_id,json_group_array(json_object(
            'field',fv.field,'verified_at',fv.verified_at,'fetch_id',fv.fetch_id,
            'value_hash',fv.value_hash,'evidence_json',fv.evidence_json
          )) AS verifications_json FROM field_verifications fv
          JOIN listings vl ON vl.id=fv.listing_id"""
            + (" WHERE vl.vehicle_id=?" if parameters else "")
            + " GROUP BY fv.listing_id) proof ON proof.listing_id=current.listing_id ORDER BY current.listing_id"
        )
        latest = _rows(conn, latest_sql, parameters * 2)
    else:
        latest = _rows(conn, latest_sql + " ORDER BY l.id", parameters)
    current_ids = {
        row["observation_id"] for row in latest if row["observation_id"] is not None
    }
    history = (
        _rows(
            conn,
            SELECT_AD.format(observations="observations")
            + " WHERE o.id NOT IN (SELECT value FROM json_each(?))"
            + (" AND l.vehicle_id=?" if parameters else "")
            + " ORDER BY l.id,o.observed_at,o.id",
            (json.dumps(sorted(current_ids)), *parameters),
        )
        if config.get("include_history", True)
        else []
    )
    fetch_ids = set()
    for row in latest + history:
        row["is_latest"] = (
            row["observation_id"] is None or row["observation_id"] in current_ids
        )
        row["details"] = _json(row.pop("details_json"))
        verification_rows = json.loads(row.pop("verifications_json", None) or "[]")
        row["verifications"] = {}
        for verification in verification_rows:
            verified_at = verification_time(verification["verified_at"])
            origin = _json(verification.pop("evidence_json"))
            origin_time = verification_time(origin.get("observed_at"))
            if (
                verified_at is None
                or verified_at > clock
                or origin_time is None
                or origin_time != verified_at
                or any(
                    origin.get(flag, False) is not False
                    for flag in ("inherited", "legacy_origin_time_unverified")
                )
                or type(origin.get("conflict", False)) is not bool
                or any(
                    field in origin
                    and (
                        type(origin[field]) is not type(row[field])
                        or origin[field] != row[field]
                    )
                    for field in ("source", "listing_id", "external_id")
                )
            ):
                continue
            verification.update(verified_at=verified_at, evidence=origin)
            field = verification["field"]
            prior = row["verifications"].get(field)
            if prior is None or verified_at > prior["verified_at"]:
                row["verifications"][field] = verification
            for fetch_id in (verification["fetch_id"], origin.get("fetch_id")):
                if isinstance(fetch_id, int):
                    fetch_ids.add(fetch_id)
        if row["fetch_id"] is not None:
            fetch_ids.add(row["fetch_id"])
        raw = row["details"].get("raw")
        evidence = raw.get("field_evidence") if isinstance(raw, dict) else None
        if isinstance(evidence, dict):
            fetch_ids.update(
                e["fetch_id"]
                for e in evidence.values()
                if isinstance(e, dict) and isinstance(e.get("fetch_id"), int)
            )
    fetches = (
        {
            row["id"]: row
            for row in _rows(
                conn,
                "SELECT id,source,url,fetched_at FROM fetches WHERE id IN "
                "(SELECT value FROM json_each(?))",
                (json.dumps(sorted(fetch_ids)),),
            )
        }
        if fetch_ids
        else {}
    )
    checks = defaultdict(list)
    for row in _rows(
        conn,
        """SELECT vehicle_id,provider,checked_at,fetch_id,data_json FROM (
        SELECT c.*,ROW_NUMBER() OVER (
            PARTITION BY vehicle_id,provider ORDER BY checked_at DESC,id DESC
        ) AS provider_rank FROM vehicle_checks c"""
        + (" WHERE vehicle_id=?" if parameters else "")
        + ") WHERE provider_rank=1 ORDER BY vehicle_id,provider",
        parameters,
    ):
        try:
            data = json.loads(row["data_json"], parse_constant=lambda _: None)
        except (ValueError, TypeError):
            data = None
        checks[row["vehicle_id"]].append(
            {
                "provider": row["provider"],
                "checked_at": row["checked_at"],
                "fetch_id": row["fetch_id"],
                "data": data,
                "fresh": _fresh(row["checked_at"], cutoff, clock),
            }
        )
    groups, histories = defaultdict(list), defaultdict(list)
    for row in latest:
        groups[row["vehicle_id"]].append(row)
    for row in history:
        histories[row["vehicle_id"]].append(row)
    cards = [
        _card(
            v,
            groups[v["id"]],
            histories[v["id"]],
            fetches,
            checks[v["id"]],
            cutoff,
            clock,
        )
        for v in vehicles
    ]
    if (config or {}).get("scope", {}).get(
        "model_profile"
    ) == "mercedes_c180_c205" and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='model_scope_verifications'"
    ).fetchone():
        proofs = defaultdict(list)
        for row in conn.execute(
            "SELECT s.*,l.vehicle_id FROM model_scope_verifications s JOIN listings l ON l.id=s.listing_id WHERE julianday(s.verified_at)<=julianday(?)",
            (clock.isoformat(),),
        ):
            proofs[row["vehicle_id"]].append(
                {
                    "listing_id": row["listing_id"],
                    "scope": row["scope"],
                    "verified_at": row["verified_at"],
                    "fetch_id": row["fetch_id"],
                    "evidence": json.loads(row["evidence_json"]),
                }
            )
        for card in cards:
            card["model_scope_verifications"] = proofs[card["id"]]
    return cards


def catalog(conn, config=None):
    """One JSON-compatible card per vehicles.id, using bounded bulk SELECTs.

    config accepts as_of (ISO timestamp/datetime; now is also accepted) and
    freshness_hours (default 13, or twice interval_hours plus one).
    No schema, transaction or row_factory changes. For a consistent snapshot
    during concurrent writes, the caller must hold its own read transaction.
    Old media and mileage observations are retained with their original dates;
    other scalar statements come exclusively from latest_observations.
    Set include_history=False to omit historical media/mileage while retaining
    all current scalar statements and their conflict/provenance rules.
    """
    return _catalog(conn, config)


def get_vehicle(conn, id, config=None):
    """Return a card or None; optional alias chains terminate safely even on cycles."""
    exists = conn.execute("SELECT id FROM vehicles WHERE id=?", (id,)).fetchone()
    if exists:
        return _catalog(conn, config, id)[0]
    has_aliases = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vehicle_aliases'"
    ).fetchone()
    if not has_aliases:
        return None
    matches = conn.execute(
        """WITH RECURSIVE resolved(id) AS (
        SELECT ? UNION SELECT a.vehicle_id FROM vehicle_aliases a
        JOIN resolved r ON a.alias_id=r.id
    ) SELECT v.id FROM vehicles v JOIN resolved r ON r.id=v.id ORDER BY v.id""",
        (id,),
    ).fetchall()
    if len(matches) != 1:
        return None
    return _catalog(conn, config, matches[0][0])[0]
