"""Independent chronology oracles for offset timestamps and damaged future proofs."""

import json
from datetime import UTC, datetime, timedelta

from car_market.db import Database
from car_market.parse import Listing
from car_market.provenance import value_hash


def item(price, **kwargs):
    return Listing(
        "fixture",
        "same",
        "https://example.test/same",
        "BMW 218i Active Tourer",
        price=price,
        year=2016,
        mileage=120000,
        fuel="petrol",
        transmission="manual",
        **kwargs,
    ).qualify()


def test_actual_latest_price_and_presence_survive_offset_order_and_reopen(tmp_path):
    path = tmp_path / "market.sqlite3"
    db = Database(path)
    older = "2026-10-07T12:00:00+02:00"
    newer = "2026-10-07T11:00:00+00:00"
    lid = db.observe(db.start_run(), item(8000), older)
    db.observe(db.start_run(), item(7500), newer)
    db.observe(db.start_run(), item(8200), "2026-10-07T12:30:00+02:00")
    assert (
        db.conn.execute(
            "SELECT price_eur FROM latest_observations WHERE listing_id=?", (lid,)
        ).fetchone()[0]
        == 7500
    )
    row = db.conn.execute("SELECT first_seen,last_seen FROM listings WHERE id=?", (lid,)).fetchone()
    assert tuple(row) == (older, newer)
    assert (
        db.conn.execute("SELECT COUNT(*) FROM events WHERE kind='price_eur_changed'").fetchone()[0]
        == 1
    )
    db.conn.execute("DROP VIEW latest_observations")
    db.conn.execute(
        "CREATE VIEW latest_observations AS SELECT * FROM observations o WHERE o.id=(SELECT n.id FROM observations n WHERE n.listing_id=o.listing_id ORDER BY n.observed_at DESC,n.id DESC LIMIT 1)"
    )
    db.conn.execute("PRAGMA user_version=4")
    db.conn.commit()
    db.conn.close()
    db = Database(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert (
        db.conn.execute(
            "SELECT price_eur FROM latest_observations WHERE listing_id=?", (lid,)
        ).fetchone()[0]
        == 7500
    )
    assert db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()
    db.conn.close()


def test_same_instant_offsets_use_latest_row_as_tiebreak(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    lid = db.observe(db.start_run(), item(8000), "2026-10-07T12:00:00+02:00")
    db.observe(db.start_run(), item(7500), "2026-10-07T10:00:00+00:00")
    assert (
        db.conn.execute(
            "SELECT price_eur FROM latest_observations WHERE listing_id=?", (lid,)
        ).fetchone()[0]
        == 7500
    )
    db.conn.close()


def test_corrupt_future_confirmation_does_not_block_current_same_price(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    clock = datetime.now(UTC)
    old = (clock - timedelta(hours=2)).isoformat()
    current = (clock - timedelta(minutes=1)).isoformat()
    future = (clock + timedelta(days=1)).isoformat()
    lid = db.observe(db.start_run(), item(8000), old)
    db.conn.execute(
        "UPDATE field_verifications SET verified_at=?,evidence_json=? WHERE listing_id=? AND field='price'",
        (future, json.dumps({"observed_at": future}), lid),
    )
    db.conn.commit()
    db.observe(db.start_run(), item(8000), current)
    proof = db.conn.execute(
        "SELECT verified_at,value_hash FROM field_verifications WHERE listing_id=? AND field='price'",
        (lid,),
    ).fetchone()
    assert tuple(proof) == (current, value_hash(8000))
    assert db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    db.conn.close()


def test_future_observation_cannot_supply_current_verification(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    db.observe(db.start_run(), item(8000), future)
    assert db.conn.execute("SELECT COUNT(*) FROM field_verifications").fetchone()[0] == 0
    db.conn.close()


def test_future_original_lineage_is_recovered_by_current_own_proof(tmp_path):
    from car_market.opportunities import inventory
    from car_market.vehicle_catalog import get_vehicle

    db = Database(tmp_path / "market.sqlite3")
    clock = datetime.now(UTC)
    old = (clock - timedelta(hours=2)).isoformat()
    current = (clock - timedelta(minutes=1)).isoformat()
    future = (clock + timedelta(days=1)).isoformat()
    raw = {"field_evidence": {"price": {"observed_at": future, "inherited": False}}}
    lid = db.observe(db.start_run(), item(8000, raw=raw), old)
    db.observe(db.start_run(), item(8000), current)
    vehicle = db.conn.execute("SELECT vehicle_id FROM listings WHERE id=?", (lid,)).fetchone()[0]
    config = {"as_of": clock.isoformat(), "freshness_hours": 3}
    card = get_vehicle(db.conn, vehicle, config)
    assert card["price_min_eur"] == 8000
    assert card["offers"][0]["current_price"] is True
    assert inventory(db.conn, config)[0]["price_verified_at"] == current
    original = json.loads(db.conn.execute("SELECT details_json FROM observations").fetchone()[0])
    assert original["raw"]["field_evidence"]["price"]["observed_at"] == future
    assert db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    db.conn.close()
