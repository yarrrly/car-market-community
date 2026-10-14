"""Fresh listings must not conceal stale fields, invalid clocks or broken payloads."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from car_market.db import Database, now
from car_market.parse import Listing
from car_market.quality import report


def populated(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    candidate = Listing("autoscout24", "one", "https://www.autoscout24.nl/aanbod/one", "BMW 218i Active Tourer", price=10000, fuel="petrol").qualify()
    db.observe(db.start_run(), candidate, now())
    return db


@pytest.mark.parametrize("stamp", ["broken", "2026-01-01T12:00:00", (datetime.now(UTC) + timedelta(days=1)).isoformat()])
def test_invalid_or_future_last_seen_is_stale_without_crashing(tmp_path, stamp):
    db = populated(tmp_path)
    try:
        db.conn.execute("UPDATE listings SET last_seen=?", (stamp,))
        changes = db.conn.total_changes
        source = report(db.conn, {"queries": []})["sources"][0]
        assert source["invalid_timestamp_ads"] == source["stale_ads"] == 1
        assert db.conn.total_changes == changes
    finally:
        db.conn.close()


def test_quality_uses_explicit_freshness_window(tmp_path):
    db = populated(tmp_path)
    try:
        stamp = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        db.conn.execute("UPDATE listings SET last_seen=?", (stamp,))
        source = report(db.conn, {"queries": [], "freshness_hours": 1})["sources"][0]
        assert source["stale_ads"] == 1
    finally:
        db.conn.close()


@pytest.mark.parametrize("payload", ["{broken", "[]", '{"raw": []}', '{"raw": {"field_evidence": {"price": "invalid"}}}'])
def test_corrupt_observation_payload_does_not_hide_other_diagnostics(tmp_path, payload):
    db = populated(tmp_path)
    try:
        db.conn.execute("UPDATE observations SET details_json=?", (payload,))
        source = report(db.conn, {"queries": []})["sources"][0]
        assert source["invalid_observation_payload_ads"] == 1
        assert source["observed_ads"] == 1
        assert source["fields"]["price"]["fresh_confirmations"] == 0
    finally:
        db.conn.close()


def test_new_presence_does_not_refresh_a_retained_price_confirmation(tmp_path):
    db = populated(tmp_path)
    try:
        row = db.conn.execute("SELECT details_json FROM observations LIMIT 1").fetchone()
        data = json.loads(row[0])
        data["raw"]["field_evidence"]["price"]["observed_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        db.conn.execute("UPDATE observations SET details_json=?", (json.dumps(data),))
        source = report(db.conn, {"queries": []})["sources"][0]
        assert source["stale_ads"] == 0
        assert source["fields"]["price"]["present"] == 1
        assert source["fields"]["price"]["fresh_confirmations"] == 0
    finally:
        db.conn.close()
