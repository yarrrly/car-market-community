"""Original snapshot statements remain idempotent through newer normalization."""

import json
import sqlite3
import sys

import pytest
from car_market import cli
from car_market.db import Database
from car_market.parse import Listing

STAMP = "2026-10-07T10:00:00+00:00"


@pytest.mark.parametrize("kind", ["conditional_price", "model_conflict"])
def test_original_json_survives_normalization_and_repeat_import(tmp_path, monkeypatch, kind):
    snapshot = tmp_path / "snapshot/market.sqlite3"
    snapshot.parent.mkdir()
    db = Database(snapshot)
    item = Listing(
        "fixture",
        "original",
        "https://example.test/original",
        "BMW 218i Active Tourer",
        price=8000,
        fuel="petrol",
        kenteken="AB123C",
        description="Dit bedrag is uitsluitend de aanbetaling."
        if kind == "conditional_price"
        else None,
    ).qualify()
    run = db.start_run(timestamp=STAMP)
    lid = db.observe(run, item, STAMP)
    payload = json.loads(db.conn.execute("SELECT details_json FROM observations").fetchone()[0])
    payload["price_type"] = "advertised_price"
    payload["raw"].pop("price_warning", None)
    payload["raw"].pop("advertised_amount_eur", None)
    payload["scope"] = "target"
    db.conn.execute(
        "UPDATE observations SET details_json=?,price_type=?,scope=?",
        (json.dumps(payload), "advertised_price", "target"),
    )
    if kind == "model_conflict":
        identity = db.conn.execute("SELECT vehicle_id FROM listings WHERE id=?", (lid,)).fetchone()[
            0
        ]
        db.conn.execute(
            "INSERT INTO vehicle_checks(vehicle_id,checked_at,provider,data_json) VALUES (?,?,?,?)",
            (identity, STAMP, "rdw", '{"handelsbenaming":"218I GRAN TOURER"}'),
        )
    db.conn.commit()
    db.conn.close()
    destination = tmp_path / "destination/market.sqlite3"
    arguments = [
        "car-market",
        "--db",
        str(destination),
        "--export-dir",
        str(tmp_path / "exports"),
        "import-db",
        str(snapshot),
    ]
    monkeypatch.setattr(sys, "argv", arguments)
    cli.main()
    with sqlite3.connect(destination) as conn:
        before = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        normalized = conn.execute(
            "SELECT details_json,price_type,scope FROM observations"
        ).fetchone()
        assert json.loads(normalized[0]) == payload
        assert (
            normalized[1] == "conditional_price"
            if kind == "conditional_price"
            else normalized[2] == "model_conflict"
        )
    cli.main()
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == before == 1


def test_preserved_observation_does_not_modify_callers_item(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    item = Listing(
        "fixture",
        "original",
        "https://example.test/original",
        "BMW 218i Active Tourer",
        price=8000,
        fuel="petrol",
        description="Dit bedrag is uitsluitend de aanbetaling.",
    ).qualify()
    import dataclasses

    original = dataclasses.asdict(item)
    lid = db.observe(db.start_run(), item, STAMP, preserve_observation=True)
    assert dataclasses.asdict(item) == original
    assert (
        json.loads(
            db.conn.execute(
                "SELECT details_json FROM observations WHERE listing_id=?", (lid,)
            ).fetchone()[0]
        )
        == original
    )
    assert (
        db.conn.execute("SELECT price_type FROM observations").fetchone()[0] == "conditional_price"
    )
    db.conn.close()
