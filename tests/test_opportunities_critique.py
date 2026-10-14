"""Regressions for canonical identity, price freshness and price signal migration."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from car_market.db import Database
from car_market.opportunities import evaluate, inventory, publish
from car_market.parse import Listing

CLOCK = datetime(2026, 10, 7, 16, tzinfo=UTC)
NOW = CLOCK.isoformat()
CONFIG = {"as_of": NOW}
ACK = "2026-10-07T15:00:00+00:00"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline test attempted an HTTP request")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "market.sqlite3")
    yield database
    database.conn.close()


def observe(db, identity="cheap", age_hours=0, source="fixture", **fields):
    values = {
        "price": 7500,
        "year": 2015,
        "mileage": 132000,
        "transmission": "manual",
        "fuel": "petrol",
    }
    values.update(fields)
    timestamp = (CLOCK - timedelta(hours=age_hours)).isoformat()
    item = Listing(
        source,
        identity,
        f"https://example.test/{source}/{identity}",
        "BMW 218i Active Tourer",
        **values,
    ).qualify()
    lid = db.observe(db.start_run(timestamp=timestamp), item, timestamp)
    vid = db.conn.execute("SELECT vehicle_id FROM listings WHERE id=?", (lid,)).fetchone()[0]
    return lid, vid


def peers(db, confirmed=True, count=8, age_hours=0):
    for i in range(count):
        observe(
            db,
            f"peer-{i}",
            age_hours,
            price=10000,
            mileage=125000 + i * 500,
            vin=f"WBA2A31010V{i:06d}" if confirmed else None,
        )


def signals(db):
    return [dict(r) for r in db.conn.execute("SELECT * FROM opportunity_signals ORDER BY id")]


def emit(db):
    return publish(db, db.path.parent / "signals", config=CONFIG)


def legacy_signal(db, identity, payload, ack=ACK, price=7500):
    db.conn.executescript("""CREATE TABLE IF NOT EXISTS opportunity_signals(
      id INTEGER PRIMARY KEY,signal_key TEXT UNIQUE NOT NULL,created_at TEXT NOT NULL,
      priority TEXT NOT NULL,payload_json TEXT NOT NULL,acknowledged_at TEXT);
      CREATE TABLE IF NOT EXISTS monitor_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      INSERT OR REPLACE INTO monitor_state VALUES ('alert_baseline','seed');""")
    cursor = db.conn.execute(
        "INSERT INTO opportunity_signals(signal_key,created_at,priority,payload_json,acknowledged_at) VALUES (?,?,?,?,?)",
        (
            json.dumps([identity, price, "inspect_urgently"]),
            ACK,
            "inspect_urgently",
            json.dumps(payload),
            ack,
        ),
    )
    db.conn.commit()
    return cursor.lastrowid


def test_different_confirmed_vins_with_equal_fingerprint_stay_separate(db):
    observe(db, "a", vin="WBA2A31010V404804")
    observe(db, "b", vin="WBA6S110807E67117")
    cars = inventory(db.conn, CONFIG)
    assert len(cars) == 2
    assert len({c["vehicle_id"] for c in cars}) == 2
    assert all(c["identity_status"] == "confirmed" for c in cars)


def test_same_vin_and_different_mileage_is_one_comparison(db):
    observe(db, "a", vin="WBA2A31010V404804")
    observe(db, "b", vin="WBA2A31010V404804", mileage=132001)
    cars = inventory(db.conn, CONFIG)
    assert len(cars) == 1 and cars[0]["ad_count"] == 2 and len(cars[0]["urls"]) == 2


def test_fuzzy_equal_provisional_ids_are_not_merged_or_certified(db):
    observe(db, "a")
    observe(db, "b", source="second")
    result = evaluate(db.conn, CONFIG)
    assert result["comparison_cars"] == 2
    assert result["confirmed_comparison_cars"] == 0
    assert result["provisional_comparison_identities"] == 2
    assert all(c["identity_status"] == "provisional" for c in result["cars"])


def test_fingerprint_does_not_transfer_known_registration(db):
    observe(db, "known", kenteken="GB178T")
    observe(db, "unknown")
    cars = inventory(db.conn, CONFIG)
    assert len(cars) == 2
    unknown = next(c for c in cars if c["external_id"] == "unknown")
    assert unknown["confirmed_kenteken"] is None and unknown["identity_status"] == "provisional"


def test_provisional_peer_ids_cannot_prove_six_independent_comparables(db):
    peers(db, confirmed=False)
    observe(db)
    result = evaluate(db.conn, CONFIG)
    assert not result["opportunities"]
    assert all(c["peer_count"] == 0 and c["peer_median_eur"] is None for c in result["cars"])


def test_only_confirmed_fresh_peers_count_toward_median(db):
    peers(db, count=6)
    observe(db, "provisional-peer", price=20000)
    observe(db)
    candidate = evaluate(db.conn, CONFIG)["opportunities"][0]
    assert candidate["peer_count"] == 6 and candidate["peer_median_eur"] == 10000


def test_stale_candidate_never_enters_pending_outbox(db):
    peers(db)
    observe(db, age_hours=90 * 24)
    assert not emit(db)["opportunities"] and signals(db) == []


def test_stale_peer_prices_do_not_create_a_discount(db):
    peers(db, age_hours=14)
    observe(db)
    result = evaluate(db.conn, CONFIG)
    assert result["comparison_cars"] == 1 and not result["opportunities"]


@pytest.mark.parametrize("age,expected", [(13, 1), (13 + 1 / 3600, 0), (-1 / 3600, 0)])
def test_price_freshness_boundaries_and_future_presence(db, age, expected):
    observe(db, age_hours=age)
    assert len(inventory(db.conn, CONFIG)) == expected


def test_interval_freshness_and_explicit_override_match_catalog_config(db):
    observe(db, age_hours=4)
    assert not inventory(db.conn, {**CONFIG, "interval_hours": 1})
    assert len(inventory(db.conn, {**CONFIG, "interval_hours": 1, "freshness_hours": 5})) == 1


def test_as_of_timezone_offset_uses_same_instant(db):
    observe(db)
    assert len(inventory(db.conn, {"as_of": "2026-10-07T18:00:00+02:00"})) == 1


@pytest.mark.parametrize("hours", [-1, float("nan"), True])
def test_invalid_freshness_is_rejected(db, hours):
    with pytest.raises(ValueError):
        inventory(db.conn, {**CONFIG, "freshness_hours": hours})


def stale_price(db):
    old = (CLOCK - timedelta(hours=14)).isoformat()
    lid, vid = observe(
        db, raw={"field_evidence": {"price": {"observed_at": old, "inherited": True}}}
    )
    return lid, vid, old


def verification(db, lid, value=7500, timestamp=NOW):
    from car_market.provenance import value_hash

    db.conn.execute("""CREATE TABLE IF NOT EXISTS field_verifications(
        listing_id INTEGER NOT NULL,field TEXT NOT NULL,verified_at TEXT NOT NULL,
        fetch_id INTEGER,value_hash TEXT NOT NULL,evidence_json TEXT NOT NULL)""")
    db.conn.execute(
        "INSERT OR REPLACE INTO field_verifications(listing_id,field,verified_at,fetch_id,value_hash,evidence_json) VALUES (?,?,?,?,?,?)",
        (
            lid,
            "price",
            timestamp,
            None,
            value_hash(value),
            json.dumps({"source": "fixture", "inherited": False, "observed_at": timestamp}),
        ),
    )
    db.conn.commit()


def test_fresh_presence_does_not_refresh_inherited_price_origin(db):
    stale_price(db)
    assert not inventory(db.conn, CONFIG)


def test_future_price_lineage_is_rejected_even_with_current_presence(db):
    observe(
        db,
        raw={
            "field_evidence": {"price": {"observed_at": (CLOCK + timedelta(seconds=1)).isoformat()}}
        },
    )
    assert not inventory(db.conn, CONFIG)


def test_matching_direct_unchanged_price_confirmation_refreshes_eligibility_only(db):
    lid, _, old = stale_price(db)
    verification(db, lid)
    cars = inventory(db.conn, CONFIG)
    assert len(cars) == 1 and cars[0]["price_verified_at"] == NOW
    payload = json.loads(
        db.conn.execute(
            "SELECT details_json FROM latest_observations WHERE listing_id=?", (lid,)
        ).fetchone()[0]
    )
    assert payload["raw"]["field_evidence"]["price"]["observed_at"] == old


@pytest.mark.parametrize("value,age", [(7000, 0), (7500, 14), (7500, -1)])
def test_mismatched_stale_or_future_confirmation_does_not_refresh_price(db, value, age):
    lid, _, _ = stale_price(db)
    verification(db, lid, value, (CLOCK - timedelta(hours=age)).isoformat())
    assert not inventory(db.conn, CONFIG)


def test_v4_confirmation_never_revives_removed_offer(db):
    lid, _, _ = stale_price(db)
    verification(db, lid)
    db.conn.execute("UPDATE listings SET availability='removed' WHERE id=?", (lid,))
    db.conn.commit()
    assert not inventory(db.conn, CONFIG)


def test_rdw_checklist_belongs_to_each_car(db):
    _, unchecked = observe(db, "unchecked")
    _, checked = observe(db, "checked", kenteken="GB178T", mileage=132001)
    db.conn.execute(
        "INSERT INTO vehicle_checks(vehicle_id,checked_at,provider,data_json) VALUES (?,?,'rdw',?)",
        (checked, NOW, json.dumps({"handelsbenaming": "BMW 218I ACTIVE TOURER"})),
    )
    db.conn.commit()
    cars = {c["vehicle_id"]: c for c in evaluate(db.conn, CONFIG)["cars"]}
    assert "registration and RDW" in cars[unchecked]["missing_checks"]
    assert "registration and RDW" not in cars[checked]["missing_checks"]


def test_mileage_correction_same_uuid_and_price_does_not_repeat_alert(db):
    peers(db)
    _, vid = observe(db)
    emit(db)
    db.conn.execute("UPDATE opportunity_signals SET acknowledged_at=?", (ACK,))
    db.conn.commit()
    observe(db, mileage=132001)
    emit(db)
    rows = signals(db)
    assert len(rows) == 1 and json.loads(rows[0]["signal_key"])[0] == vid
    assert rows[0]["acknowledged_at"] == ACK


def test_registration_enrichment_keeps_existing_uuid_signal(db):
    peers(db)
    lid, vid = observe(db)
    emit(db)
    db.conn.execute("UPDATE opportunity_signals SET acknowledged_at=?", (ACK,))
    db.conn.commit()
    db.attach_identity(lid, "GB178T")
    db.conn.commit()
    emit(db)
    assert len(signals(db)) == 1 and json.loads(signals(db)[0]["signal_key"])[0] == vid
    assert signals(db)[0]["acknowledged_at"] == ACK


@pytest.mark.parametrize("ack", [None, ACK])
def test_legacy_fuzzy_signal_migration_preserves_id_creation_and_ack(db, ack):
    peers(db)
    lid, vid = observe(db)
    sid = legacy_signal(
        db, "('approximate', (2015, 132000, 'manual'))", {"listing_id": lid, "vehicle_id": vid}, ack
    )
    emit(db)
    emit(db)
    rows = signals(db)
    assert len(rows) == 1 and rows[0]["id"] == sid and rows[0]["created_at"] == ACK
    assert json.loads(rows[0]["signal_key"]) == [vid, 7500.0, "inspect_urgently"]
    assert rows[0]["acknowledged_at"] == ack


def test_alias_merge_preserves_acknowledged_signal_without_second_alert(db):
    peers(db)
    lid, former = observe(db)
    emit(db)
    sid = signals(db)[0]["id"]
    db.conn.execute("UPDATE opportunity_signals SET acknowledged_at=?", (ACK,))
    db.conn.commit()
    _, winner = observe(db, "confirmed-copy", vin="WBA2A31010V999999", mileage=132001)
    db.attach_identity(lid, vin="WBA2A31010V999999")
    db.conn.commit()
    assert db.resolve_vehicle_id(former) == winner
    emit(db)
    emit(db)
    rows = signals(db)
    assert len(rows) == 1 and rows[0]["id"] == sid and rows[0]["acknowledged_at"] == ACK
    assert json.loads(rows[0]["signal_key"])[0] == winner


@pytest.mark.parametrize("ack", [None, ACK])
def test_colliding_legacy_keys_retire_duplicate_without_changing_pending_state(db, ack):
    peers(db)
    lid, vid = observe(db)
    legacy_signal(db, "GB178T", {"vehicle_id": vid, "listing_id": lid}, ack=ack)
    legacy_signal(db, "('approximate', (2015, 132000, 'manual'))", {"vehicle_id": vid}, ack=None)
    emit(db)
    emit(db)
    rows = signals(db)
    live = [r for r in rows if not json.loads(r["payload_json"]).get("signal_discontinued")]
    assert len(rows) == 2 and len(live) == 1 and live[0]["acknowledged_at"] == ack
    assert sum(r["acknowledged_at"] is None for r in rows) == (1 if ack is None else 0)


def test_unresolvable_old_fuzzy_key_is_discontinued_without_claiming_identity(db):
    legacy_signal(db, "('approximate', (2015, 132000, 'manual'))", {}, ack=None)
    emit(db)
    row = signals(db)[0]
    assert row["acknowledged_at"] is not None
    assert (
        json.loads(row["payload_json"])["signal_discontinued_reason"]
        == "unresolvable_vehicle_identity"
    )


def test_pending_payload_updates_without_new_signal_id(db):
    peers(db)
    observe(db)
    emit(db)
    sid = signals(db)[0]["id"]
    observe(db, mileage=132001)
    emit(db)
    assert len(signals(db)) == 1 and signals(db)[0]["id"] == sid
    assert json.loads(signals(db)[0]["payload_json"])["mileage_km"] == 132001


def test_price_drop_still_creates_new_event_after_migration(db):
    peers(db)
    lid, vid = observe(db)
    legacy_signal(
        db, "('approximate', (2015, 132000, 'manual'))", {"vehicle_id": vid, "listing_id": lid}
    )
    emit(db)
    observe(db, price=7000)
    emit(db)
    rows = signals(db)
    assert (
        len(rows) == 2 and rows[0]["acknowledged_at"] == ACK and rows[1]["acknowledged_at"] is None
    )
    assert json.loads(rows[1]["signal_key"]) == [vid, 7000.0, "inspect_urgently"]


def test_legacy_registration_only_key_resolves_without_payload_uuid(db):
    peers(db)
    _, vid = observe(db, kenteken="GB178T")
    legacy_signal(db, "GB178T", {})
    emit(db)
    assert len(signals(db)) == 1 and signals(db)[0]["acknowledged_at"] == ACK
    assert json.loads(signals(db)[0]["signal_key"])[0] == vid


def test_v4_inherited_proof_cannot_refresh_stale_price(db):
    lid, _, _ = stale_price(db)
    verification(db, lid)
    db.conn.execute(
        "UPDATE field_verifications SET evidence_json=? WHERE listing_id=? AND field='price'",
        (json.dumps({"inherited": True, "observed_at": NOW}), lid),
    )
    db.conn.commit()
    assert not inventory(db.conn, CONFIG)


def test_candidate_aging_out_retires_pending_signal(db):
    peers(db)
    observe(db)
    emit(db)
    later = {"as_of": (CLOCK + timedelta(hours=14)).isoformat()}
    result = publish(db, db.path.parent / "signals", config=later)
    assert not result["opportunities"] and signals(db)[0]["acknowledged_at"] is not None


def test_unchanged_direct_price_recheck_uses_v4_without_rewriting_old_observation(db):
    lid, _ = observe(db, age_hours=14)
    old = (CLOCK - timedelta(hours=14)).isoformat()
    observe(db)
    latest = db.conn.execute(
        "SELECT observed_at,details_json FROM latest_observations WHERE listing_id=?", (lid,)
    ).fetchone()
    assert latest["observed_at"] == old
    assert (
        json.loads(latest["details_json"])["raw"]["field_evidence"]["price"]["observed_at"] == old
    )
    assert (
        db.conn.execute(
            "SELECT verified_at FROM field_verifications WHERE listing_id=? AND field='price'",
            (lid,),
        ).fetchone()[0]
        == NOW
    )
    assert inventory(db.conn, CONFIG)[0]["price_verified_at"] == NOW


def test_unchanged_inherited_price_recheck_does_not_create_direct_confirmation(db):
    lid, _ = observe(db, age_hours=14)
    old = (CLOCK - timedelta(hours=14)).isoformat()
    observe(db, raw={"field_evidence": {"price": {"observed_at": old, "inherited": True}}})
    assert (
        db.conn.execute(
            "SELECT verified_at FROM field_verifications WHERE listing_id=? AND field='price'",
            (lid,),
        ).fetchone()[0]
        == old
    )
    assert not inventory(db.conn, CONFIG)


def test_v3_readonly_snapshot_remains_supported_without_mutation(db):
    observe(db)
    db.conn.execute("DROP TABLE field_verifications")
    db.conn.commit()
    changes = db.conn.total_changes
    db.conn.execute("PRAGMA query_only=ON")
    assert inventory(db.conn, CONFIG)[0]["price_verified_at"] == NOW
    assert db.conn.total_changes == changes
    assert (
        db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='field_verifications'").fetchone()
        is None
    )


@pytest.mark.parametrize("delivered", [False, True])
def test_alias_merge_of_two_canonical_signals_keeps_one_actionable_event(db, delivered):
    peers(db)
    lid, former = observe(db, "first")
    _, winner = observe(db, "second", vin="WBA2A31010V999999", mileage=132001)
    emit(db)
    assert len(signals(db)) == 2
    if delivered:
        db.conn.execute(
            "UPDATE opportunity_signals SET acknowledged_at=? WHERE signal_key=?",
            (ACK, json.dumps([former, 7500.0, "inspect_urgently"])),
        )
        db.conn.commit()
    db.attach_identity(lid, vin="WBA2A31010V999999")
    db.conn.commit()
    emit(db)
    emit(db)
    rows = signals(db)
    active = [r for r in rows if not json.loads(r["payload_json"]).get("signal_discontinued")]
    assert len(rows) == 2 and len(active) == 1
    assert json.loads(active[0]["signal_key"])[0] == winner
    assert active[0]["acknowledged_at"] == (ACK if delivered else None)


def test_fresh_legacy_origin_is_not_a_direct_price_confirmation(db):
    lid, _ = observe(
        db,
        raw={
            "field_evidence": {"price": {"observed_at": NOW, "legacy_origin_time_unverified": True}}
        },
    )
    assert (
        db.conn.execute(
            "SELECT 1 FROM field_verifications WHERE listing_id=? AND field='price'", (lid,)
        ).fetchone()
        is None
    )
    assert not inventory(db.conn, CONFIG)


def test_matching_legacy_verification_proof_cannot_refresh_old_price(db):
    lid, _, _ = stale_price(db)
    verification(db, lid)
    db.conn.execute(
        "UPDATE field_verifications SET evidence_json=? WHERE listing_id=? AND field='price'",
        (json.dumps({"legacy_origin_time_unverified": True, "observed_at": NOW}), lid),
    )
    db.conn.commit()
    assert not inventory(db.conn, CONFIG)


@pytest.mark.parametrize(
    "proof",
    [
        {},
        {"observed_at": "not-a-date"},
        {"observed_at": "2026-10-07T16:00:00"},
        {"observed_at": (CLOCK - timedelta(hours=14)).isoformat()},
        {"observed_at": (CLOCK - timedelta(seconds=1)).isoformat()},
        {"observed_at": (CLOCK + timedelta(seconds=1)).isoformat()},
        {"observed_at": NOW, "source": "other-source"},
        {"observed_at": NOW, "listing_id": 999},
        {"observed_at": NOW, "listing_id": "1"},
        {"observed_at": NOW, "listing_id": True},
        {"observed_at": NOW, "external_id": "unrelated-stock"},
        {"observed_at": NOW, "inherited": True},
        {"observed_at": NOW, "conflict": True},
        {"observed_at": NOW, "legacy_origin_time_unverified": True},
        {"observed_at": NOW, "inherited": "false"},
        {"observed_at": NOW, "inherited": 0},
    ],
)
def test_matching_hash_cannot_refresh_price_with_invalid_verification_proof(db, proof):
    peers(db)
    lid, _, _ = stale_price(db)
    verification(db, lid)
    db.conn.execute(
        "UPDATE field_verifications SET evidence_json=? WHERE listing_id=? AND field='price'",
        (json.dumps(proof), lid),
    )
    db.conn.commit()
    result = emit(db)
    assert not result["opportunities"] and signals(db) == []
    assert all(car["listing_id"] != lid for car in result["cars"])


@pytest.mark.parametrize("serialized", ["{", "[]", "null", '"text"', "1"])
def test_nonobject_or_malformed_verification_json_never_refreshes_price(db, serialized):
    lid, _, _ = stale_price(db)
    verification(db, lid)
    db.conn.execute(
        "UPDATE field_verifications SET evidence_json=? WHERE listing_id=? AND field='price'",
        (serialized, lid),
    )
    db.conn.commit()
    assert not inventory(db.conn, CONFIG)


def test_valid_publication_proof_accepts_equal_actual_time_with_different_timezone(db):
    lid, _, _ = stale_price(db)
    verification(db, lid)
    db.conn.execute(
        "UPDATE field_verifications SET evidence_json=? WHERE listing_id=? AND field='price'",
        (
            json.dumps(
                {
                    "observed_at": "2026-10-07T18:00:00+02:00",
                    "inherited": False,
                    "conflict": False,
                    "legacy_origin_time_unverified": False,
                    "source": "fixture",
                    "listing_id": lid,
                    "external_id": "cheap",
                }
            ),
            lid,
        ),
    )
    db.conn.commit()
    assert inventory(db.conn, CONFIG)[0]["price_verified_at"] == NOW


def test_timezone_missing_from_both_proof_and_verified_at_is_not_accepted(db):
    lid, _, _ = stale_price(db)
    verification(db, lid, timestamp="2026-10-07T16:00:00")
    assert not inventory(db.conn, CONFIG)


def test_verification_cannot_precede_newer_own_price_origin(db):
    future = (CLOCK + timedelta(seconds=1)).isoformat()
    lid, _ = observe(db, raw={"field_evidence": {"price": {"observed_at": future}}})
    verification(db, lid)
    assert not inventory(db.conn, CONFIG)
