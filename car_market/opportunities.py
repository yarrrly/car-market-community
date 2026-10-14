"""Transparent asking-price comparisons and a durable, deduplicated alert outbox."""

import json
import math
import pathlib
import re
import statistics
from datetime import UTC, datetime, timedelta

from .db import now
from .model_profiles import own_country

RISK = re.compile(
    r"duits kenteken|duitse kenteken|duitsland|importkosten|motor loopt|motorproble|motorschade|defect|schadeauto|schadevoertuig|lees tekst|lees de tekst|exportprijs|export prijs|meeneemprijs|handelaar|handelsprijs|zonder garantie|nl prijs",
    re.IGNORECASE,
)
DE_RISK = re.compile(
    r"motorschaden|getriebeschaden|unfallwagen|unfallschaden|bastlerfahrzeug|nicht fahrbereit|nur export|nur gewerbe|ohne gewährleistung",
    re.IGNORECASE,
)


def _time(value):
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return (
        timestamp.replace(tzinfo=UTC)
        if timestamp.tzinfo is None
        else timestamp.astimezone(UTC)
    )


def _window(config):
    config = config or {}
    clock = _time(config.get("as_of", config.get("now", now())))
    hours = config.get("freshness_hours", config.get("interval_hours", 6) * 2 + 1)
    if (
        clock is None
        or isinstance(hours, bool)
        or not isinstance(hours, (int, float))
        or not math.isfinite(hours)
        or hours < 0
    ):
        raise ValueError(
            "as_of must be an ISO timestamp; freshness_hours must be nonnegative"
        )
    return clock, clock - timedelta(hours=hours)


def _fresh(value, clock, cutoff):
    timestamp = _time(value)
    return timestamp is not None and cutoff <= timestamp <= clock


def _price_times(conn, records, clock):
    # v3 snapshots remain readable; v4 records direct confirmations separately.
    if (
        not records
        or not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_verifications'"
        ).fetchone()
    ):
        return {}
    from .provenance import timestamp as verification_time
    from .provenance import value_hash

    publications = {row["listing_id"]: row for row in records}
    hashes = {
        row["listing_id"]: {
            value_hash(json.loads(row["details_json"]).get("price", row["price_eur"])),
            value_hash(row["price_eur"]),
        }
        for row in records
    }
    times = {}
    for record in conn.execute(
        "SELECT listing_id,verified_at,value_hash,evidence_json FROM field_verifications WHERE field='price'"
    ):
        publication = publications.get(record["listing_id"])
        if (
            publication is None
            or record["value_hash"] not in hashes[record["listing_id"]]
        ):
            continue
        try:
            proof = json.loads(record["evidence_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(proof, dict):
            continue
        verified_at = verification_time(record["verified_at"])
        if (
            verified_at is None
            or verification_time(proof.get("observed_at")) != verified_at
        ):
            continue
        if any(
            proof.get(flag, False) is not False
            for flag in ("inherited", "conflict", "legacy_origin_time_unverified")
        ):
            continue
        if any(
            field in proof
            and (
                type(proof[field]) is not type(publication[field])
                or proof[field] != publication[field]
            )
            for field in ("source", "listing_id", "external_id")
        ):
            continue
        raw = json.loads(publication["details_json"]).get("raw") or {}
        evidence = raw.get("field_evidence") or {}
        origin = evidence.get("price", evidence.get("price_eur"))
        original_time = _time(
            origin.get("observed_at")
            if isinstance(origin, dict)
            else publication["observed_at"]
        )
        if (
            original_time is not None
            and verified_at < original_time
            and (
                original_time <= clock
                or verified_at <= _time(publication["observed_at"])
            )
        ):
            continue
        times.setdefault(record["listing_id"], []).append(record["verified_at"])
    return times


def inventory(conn, config=None):
    german = (config or {}).get("scope", {}).get("country") == "DE"
    market_country = "DE" if german else "NL"
    clock, cutoff = _window(config)
    records = [
        dict(x)
        for x in conn.execute(
            """SELECT m.*,v.vin AS confirmed_vin,o.details_json FROM market_current m
      JOIN latest_observations o ON o.listing_id=m.listing_id
      JOIN vehicles v ON v.id=m.vehicle_id
      WHERE m.scope='target' AND m.availability='observed'
      AND m.price_type IN ('advertised_price','FIXED') AND m.price_eur>0
      AND (? OR (m.price_eur BETWEEN 2000 AND 70000
      AND m.year BETWEEN 2014 AND 2030 AND m.mileage_km BETWEEN 1 AND 450000))""",
            (german,),
        )
    ]
    confirmations = _price_times(conn, records, clock)
    groups = {}
    for item in records:
        if not _fresh(item["last_seen"], clock, cutoff):
            continue
        detail = json.loads(item["details_json"])
        raw = detail.get("raw") or {}
        country, country_proof = own_country(raw)
        # Unknown country keeps the existing admission policy; explicit
        # foreign or conflicting own claims cannot supply this market's peers.
        if (country is not None and country != market_country) or country_proof.get(
            "kind"
        ) == "conflicting_own_country":
            continue
        if (
            raw.get("historical_only")
            or (german and raw.get("price_warning"))
            or any(
                field in raw.get("field_conflicts", {})
                for field in ("price", "price_eur")
            )
        ):
            continue
        evidence = raw.get("field_evidence", {})
        origin = evidence.get("price", evidence.get("price_eur"))
        original_time = (
            origin.get("observed_at")
            if isinstance(origin, dict)
            else item["observed_at"]
        )
        direct_times = confirmations.get(item["listing_id"], [])
        eligible_times = [
            value for value in direct_times if _fresh(value, clock, cutoff)
        ]
        if not (
            isinstance(origin, dict) and origin.get("legacy_origin_time_unverified")
        ) and _fresh(original_time, clock, cutoff):
            eligible_times.append(original_time)
        if not eligible_times:
            continue
        item["price_verified_at"] = max(eligible_times, key=_time)
        groups.setdefault(item["vehicle_id"], []).append(item)
    catalog_cards = {}
    if not german and groups:
        from .vehicle_catalog import catalog

        catalog_cards = {
            card["id"]: card
            for card in catalog(conn, {**(config or {}), "include_history": False})
        }
    result = []
    for identity, ads in groups.items():
        # Canonical identity is exact; equal year/km/gear never merges cars here.
        ads.sort(
            key=lambda x: ("#" in x["url"], x["source"] == "trovit", x["price_eur"])
        )
        item = dict(ads[0])
        if not german:
            card = catalog_cards.get(identity, {})
            stable_fields = ("year", "fuel", "transmission")
            item["comparison_fields"] = {
                field: card.get(field) for field in stable_fields
            }
            item["comparison_field_conflicts"] = {
                field: card.get("field_conflicts", {}).get(field, [])
                for field in stable_fields
                if card.get(field) is None
                and card.get("field_conflicts", {}).get(field)
            }
        confirmed = bool(item["confirmed_kenteken"] or item["confirmed_vin"])
        item["comparison_group"] = identity
        item["ad_count"] = len(ads)
        item["identity_status"] = "confirmed" if confirmed else "provisional"
        item["comparison_identity"] = (
            "canonical_confirmed" if confirmed else "canonical_provisional"
        )
        item["urls"] = list(dict.fromkeys(x["url"] for x in ads))
        descriptions = []
        for ad in ads:
            detail = json.loads(ad["details_json"])
            historical = conn.execute(
                "SELECT details_json FROM observations WHERE listing_id=? AND json_extract(details_json,'$.description') IS NOT NULL ORDER BY observed_at DESC,id DESC LIMIT 1",
                (ad["listing_id"],),
            ).fetchone()
            if historical:
                descriptions.append(json.loads(historical[0]).get("description") or "")
            descriptions.append(
                detail.get("title", "")
                + " "
                + (detail.get("description") or "")
                + " "
                + str(detail.get("raw", {}).get("card_text", ""))
            )
        item["risk_matches"] = sorted(
            {
                m.group(0).lower()
                for m in (DE_RISK if german else RISK).finditer(" ".join(descriptions))
            }
        )
        if german:
            item["risk_matches"].extend(
                "conflicting_" + field
                for field in ("year", "mileage", "transmission", "fuel")
                if any(
                    field
                    in json.loads(ad["details_json"])
                    .get("raw", {})
                    .get("field_conflicts", {})
                    for ad in ads
                )
            )
        result.append(item)
    return result


def evaluate(conn, config=None):
    from .model_profiles import MERCEDES_PROFILE, bound_scope

    scope = bound_scope(conn)
    if scope and scope.get("model_profile") in {MERCEDES_PROFILE, "configured_vehicle"}:
        return {"opportunities": [], "status": "not_configured_for_model_profile"}
    german = (config or {}).get("scope", {}).get("country") == "DE"
    cars = inventory(conn, config)
    for car in cars:
        if german:
            car["inspection_flags"] = []
            continue
        check = conn.execute(
            "SELECT data_json FROM vehicle_checks WHERE vehicle_id=? AND provider='rdw' ORDER BY checked_at DESC,id DESC LIMIT 1",
            (car["vehicle_id"],),
        ).fetchone()
        rdw = json.loads(check[0]) if check else {}
        car["rdw_check_present"] = bool(rdw)
        car["rdw"] = {
            k: rdw.get(k)
            for k in [
                "handelsbenaming",
                "tellerstandoordeel",
                "vervaldatum_apk",
                "datum_eerste_toelating",
                "datum_eerste_tenaamstelling_in_nederland",
                "openstaande_terugroepactie_indicator",
                "wam_verzekerd",
            ]
        }
        if str(rdw.get("tellerstandoordeel", "")).lower() == "onlogisch":
            car["risk_matches"].append("rdw_onlogisch")
        car["inspection_flags"] = (
            ["rdw_open_recall"]
            if str(rdw.get("openstaande_terugroepactie_indicator", "")).lower() == "ja"
            else []
        )
    for car in cars:
        if car.get("comparison_field_conflicts"):
            car.update(
                peer_count=0,
                peer_median_eur=None,
                discount_pct=None,
                priority="ordinary",
                missing_checks=[
                    "resolve conflicting " + field
                    for field in car["comparison_field_conflicts"]
                ],
            )
            continue
        if german and (car["year"] is None or car["mileage_km"] is None):
            car.update(
                peer_count=0,
                peer_median_eur=None,
                discount_pct=None,
                priority="ordinary",
                missing_checks=[
                    "first registration",
                    "own mileage",
                    "seller availability",
                    "German gross purchase price",
                    "service history and independent inspection",
                ],
            )
            continue
        peers = [
            p
            for p in cars
            if p["vehicle_id"] != car["vehicle_id"]
            and p["identity_status"] == "confirmed"
            and not p["risk_matches"]
            and not p.get("comparison_field_conflicts")
            and p["transmission"] == car["transmission"]
            and p["year"] is not None
            and p["mileage_km"] is not None
            and abs(p["year"] - car["year"]) <= 1
            and abs(p["mileage_km"] - car["mileage_km"]) <= 30000
            and ((p["year"] >= 2022) == (car["year"] >= 2022))
        ]
        prices = [p["price_eur"] for p in peers]
        car["peer_count"] = len(prices)
        car["peer_median_eur"] = (
            round(statistics.median(prices)) if len(prices) >= 6 else None
        )
        car["discount_pct"] = (
            round(100 * (1 - car["price_eur"] / car["peer_median_eur"]), 1)
            if car["peer_median_eur"]
            else None
        )
        km, yr, price = car["mileage_km"], car["year"], car["price_eur"]
        target = (
            not german
            and car["transmission"] == "manual"
            and price <= 10000
            and (
                (2016 <= yr <= 2017 and km <= 150000 and price <= 9000)
                or (2014 <= yr <= 2015 and km <= 150000 and price <= 7500)
                or (2014 <= yr <= 2015 and km <= 180000 and price <= 6500)
                or (2018 <= yr <= 2021 and km <= 150000 and price <= 10000)
            )
        )
        car["priority"] = (
            "inspect_urgently"
            if target
            and car["discount_pct"] is not None
            and car["discount_pct"] >= 15
            and not car["risk_matches"]
            and not car["inspection_flags"]
            else (
                "price_candidate"
                if not german
                and price <= 10000
                and km <= 200000
                and car["discount_pct"] is not None
                and car["discount_pct"] >= 20
                and not car["risk_matches"]
                else "ordinary"
            )
        )
        car["missing_checks"] = [
            "service invoices",
            "cold start and diagnostics",
            "independent inspection",
            "German gross purchase price" if german else "final NL delivery price",
            "seller availability",
        ]
        if car["inspection_flags"]:
            car["missing_checks"].append(
                "resolve or verify open VIN-specific recall before purchase"
            )
        if not german and not car["rdw_check_present"]:
            car["missing_checks"].append("registration and RDW")
        if yr <= 2015:
            car["missing_checks"].append(
                "early B38 VIN-specific campaign and repair history"
            )
    return {
        "generated_at": now(),
        "comparison_cars": len(cars),
        "confirmed_comparison_cars": sum(
            c["identity_status"] == "confirmed" for c in cars
        ),
        "provisional_comparison_identities": sum(
            c["identity_status"] == "provisional" for c in cars
        ),
        "market_country": "DE" if german else "NL",
        "method": "fresh asking prices; canonical vehicle IDs; provisional IDs are not verified unique cars; median uses >=6 confirmed identities; +/-1 year and +/-30000 km; not transaction values",
        "opportunities": sorted(
            [x for x in cars if x["priority"] != "ordinary"],
            key=lambda x: (x["priority"] != "inspect_urgently", -x["discount_pct"]),
        ),
        "cars": cars,
    }


def _signal_key(identity, price, priority):
    return json.dumps([identity, float(price), priority])


def _migrate_signals(conn, timestamp):
    identities = {r["id"]: r["id"] for r in conn.execute("SELECT id FROM vehicles")}
    identities.update(
        {
            r["alias_id"]: r["vehicle_id"]
            for r in conn.execute("SELECT * FROM vehicle_aliases")
        }
    )
    listings = {
        r["id"]: r["vehicle_id"]
        for r in conn.execute("SELECT id,vehicle_id FROM listings")
    }
    registrations = {
        r["kenteken"]: r["id"]
        for r in conn.execute(
            "SELECT id,kenteken FROM vehicles WHERE kenteken IS NOT NULL"
        )
    }
    groups = {}
    for row in conn.execute("SELECT * FROM opportunity_signals ORDER BY id").fetchall():
        try:
            payload = json.loads(row["payload_json"])
            key = json.loads(row["signal_key"])
        except (TypeError, ValueError):
            payload, key = {}, []
        if not isinstance(payload, dict):
            payload = {}
        if payload.get("signal_discontinued"):
            continue
        identity = identities.get(payload.get("vehicle_id")) or listings.get(
            payload.get("listing_id")
        )
        if (
            not identity
            and isinstance(key, list)
            and len(key) == 3
            and isinstance(key[0], str)
        ):
            identity = identities.get(key[0]) or registrations.get(key[0])
        if not identity or not isinstance(key, list) or len(key) != 3:
            # Old fuzzy keys without resolvable evidence must never remain actionable.
            payload["signal_discontinued"] = True
            payload["signal_discontinued_reason"] = "unresolvable_vehicle_identity"
            conn.execute(
                "UPDATE opportunity_signals SET acknowledged_at=COALESCE(acknowledged_at,?),payload_json=? WHERE id=?",
                (timestamp, json.dumps(payload, ensure_ascii=False), row["id"]),
            )
            continue
        payload["vehicle_id"] = identity
        destination = _signal_key(identity, key[1], key[2])
        groups.setdefault(destination, []).append((row, payload))
    for key, group in groups.items():
        winner = next(
            (entry for entry in group if entry[0]["signal_key"] == key), group[0]
        )
        acknowledged = next(
            (
                row["acknowledged_at"]
                for row, _ in group
                if row["acknowledged_at"] is not None
            ),
            None,
        )
        row, payload = winner
        serialized = json.dumps(payload, ensure_ascii=False)
        if (
            row["signal_key"] != key
            or row["acknowledged_at"] != acknowledged
            or row["payload_json"] != serialized
        ):
            conn.execute(
                "UPDATE opportunity_signals SET signal_key=?,acknowledged_at=?,payload_json=? WHERE id=?",
                (key, acknowledged, serialized, row["id"]),
            )
        for other, old_payload in group:
            if other["id"] == row["id"]:
                continue
            old_payload.update(signal_discontinued=True, superseded_by_signal_key=key)
            conn.execute(
                "UPDATE opportunity_signals SET acknowledged_at=COALESCE(acknowledged_at,?),payload_json=? WHERE id=?",
                (
                    acknowledged or timestamp,
                    json.dumps(old_payload, ensure_ascii=False),
                    other["id"],
                ),
            )


def publish(db, destination, enqueue=True, config=None):
    from .model_profiles import MERCEDES_PROFILE, bound_scope

    scope = bound_scope(db.conn)
    if scope and scope.get("model_profile") in {MERCEDES_PROFILE, "configured_vehicle"}:
        return {"opportunities": [], "status": "not_configured_for_model_profile"}
    result = evaluate(db.conn, config)
    target = pathlib.Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    (target / "opportunities.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    timestamp = now()
    with db.conn:
        db.conn.execute("""CREATE TABLE IF NOT EXISTS opportunity_signals(
          id INTEGER PRIMARY KEY, signal_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL,
          priority TEXT NOT NULL, payload_json TEXT NOT NULL, acknowledged_at TEXT)""")
        db.conn.execute(
            "CREATE TABLE IF NOT EXISTS monitor_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        _migrate_signals(db.conn, timestamp)
        eligible_keys = {
            _signal_key(car["vehicle_id"], car["price_eur"], car["priority"])
            for car in result["opportunities"]
        }
        for queued in db.conn.execute(
            "SELECT id,signal_key FROM opportunity_signals WHERE acknowledged_at IS NULL"
        ).fetchall():
            if queued["signal_key"] not in eligible_keys:
                db.conn.execute(
                    "UPDATE opportunity_signals SET acknowledged_at=? WHERE id=?",
                    (timestamp, queued["id"]),
                )
        initialized = db.conn.execute(
            "SELECT value FROM monitor_state WHERE key='alert_baseline'"
        ).fetchone()
        for index, car in enumerate(result["opportunities"]):
            key = _signal_key(car["vehicle_id"], car["price_eur"], car["priority"])
            payload = {k: v for k, v in car.items() if k != "details_json"}
            # Preserve migrated acknowledgments and IDs; only new events enter the outbox.
            acknowledged = (
                timestamp if not enqueue or (not initialized and index >= 3) else None
            )
            db.conn.execute(
                "INSERT INTO opportunity_signals(signal_key,created_at,priority,payload_json,acknowledged_at) VALUES (?,?,?,?,?) ON CONFLICT(signal_key) DO UPDATE SET payload_json=excluded.payload_json WHERE opportunity_signals.acknowledged_at IS NULL",
                (
                    key,
                    timestamp,
                    car["priority"],
                    json.dumps(payload, ensure_ascii=False),
                    acknowledged,
                ),
            )
        db.conn.execute(
            "INSERT OR REPLACE INTO monitor_state VALUES ('alert_baseline',?)",
            (timestamp,),
        )
    return result
