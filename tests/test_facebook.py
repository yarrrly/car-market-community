"""Synthetic intake contract tests; these are not evidence of real Facebook ads."""

import json
import sys

import pytest
from car_market import cli
from car_market.collect import Collector
from car_market.coverage import report
from car_market.db import Database
from car_market.facebook import import_observations, observations
from car_market.parse import Listing


def record(**changes):
    return {
        "url": "https://www.facebook.com/marketplace/item/123456789/?tracking=test",
        "title": "BMW 218i Active Tourer",
        "country": "NL",
        "observed_at": "2026-10-04T10:00:00+02:00",
        **changes,
    }


def test_missing_facts_stay_unconfirmed():
    item, observed = observations([record()])[0]
    assert item.scope == "needs_confirmation"
    assert item.price is item.transmission is item.fuel is item.kenteken is None
    assert observed == "2026-10-04T08:00:00+00:00"
    assert item.url.endswith("/123456789/")
    assert item.raw["published_at"] is None


def test_real_nonpersonalized_marketplace_route_keeps_identity():
    first, _ = observations(
        [record(url="https://www.facebook.com/marketplace/np/item/1380906133610682/")]
    )[0]
    second, _ = observations(
        [record(url="https://www.facebook.com/marketplace/item/1380906133610682/")]
    )[0]
    assert first.external_id == second.external_id
    assert "/np/item/" in first.url


def test_seller_sold_title_is_retained_but_not_a_purchase_candidate(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    import_observations(
        db,
        [
            record(
                title="BMW 218i Active Tourer Executive Verkochtt!!!",
                fuel="petrol",
                transmission="manual",
                price=6499,
                currency="EUR",
            )
        ],
    )
    assert (
        db.conn.execute("SELECT availability FROM listings").fetchone()[0] == "seller_marked_sold"
    )
    assert not db.conn.execute("SELECT 1 FROM purchase_candidates").fetchone()
    db.conn.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"country": "DE"},
        {"price": 8000},
        {"price": -1, "currency": "EUR"},
        {"price": float("nan"), "currency": "EUR"},
        {"mileage": True},
        {"observed_at": "2026-10-04T10:00:00"},
        {"observed_at": "2099-01-01T00:00:00Z"},
        {"published_at": "2026-10-05T10:00:00Z"},
        {"cookies": "must not be stored"},
        {"vin": "incorrect"},
        {"url": "https://facebook.com.evil.test/marketplace/item/123/"},
        {"url": "https://www.facebook.com/marketplace/"},
        {"description": {}},
    ],
)
def test_invalid_or_sensitive_inputs_rejected(changes):
    with pytest.raises(ValueError):
        observations([record(**changes)])


def test_dedup_partial_history_and_coverage(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    db.observe(
        run,
        Listing(
            "dealer",
            "1",
            "https://example.test/1",
            "BMW 218i Active Tourer",
            fuel="petrol",
            kenteken="AB123C",
        ).qualify(),
        "2026-10-04T08:00:00+00:00",
    )
    batch = [
        record(fuel="petrol", kenteken="AB-123-C", price=8000, currency="EUR"),
        record(url="https://www.facebook.com/groups/test/posts/123456789/"),
        record(url="https://www.facebook.com/dealer/posts/123456789/", title="BMW 218d"),
    ]
    assert import_observations(db, batch)["accepted"] == 2
    import_observations(db, batch[:1])
    assert (
        db.conn.execute("SELECT COUNT(*) FROM listings WHERE source='facebook'").fetchone()[0] == 2
    )
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 2
    assert not db.conn.execute(
        "SELECT 1 FROM events WHERE kind='not_seen_in_complete_query'"
    ).fetchone()
    coverage = report(db.conn, {"queries": [], "channels": [{"source": "facebook"}]})
    channel = coverage["channels"][0]
    assert (channel["status"], channel["observed_ads"], channel["matched_to_other_sources"]) == (
        "partial",
        2,
        1,
    )
    assert channel["ads_without_strong_identity"] == 1
    assert not channel["inventory_complete"]
    assert not coverage["market_100_percent_verified"]
    with pytest.raises(ValueError):
        import_observations(db, [record(), record(country="BE")])
    assert db.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3
    db.conn.close()


def test_cli_intake_without_network(tmp_path, monkeypatch, capsys):
    batch = tmp_path / "observations.json"
    batch.write_text(json.dumps([record()]))
    db_path = tmp_path / "market.sqlite3"
    monkeypatch.setattr(
        sys, "argv", ["car-market", "--db", str(db_path), "import-facebook", str(batch)]
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["accepted"] == 1
    db = Database(db_path)
    assert db.conn.execute("SELECT transport,status FROM fetches").fetchone()[:] == (
        "user_supplied",
        None,
    )
    db.conn.close()


def test_daemon_does_not_queue_authenticated_facebook_details(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, {"queries": []})
    item, _ = observations([record()])[0]
    collector.queue_details(item, 1)
    assert not collector.detail_jobs
    db.conn.close()
