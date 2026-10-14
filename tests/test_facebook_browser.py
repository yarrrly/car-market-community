"""Authenticated cards are visible facts, never a certified Facebook inventory."""

from pathlib import Path

import pytest
from car_market.db import Database
from car_market.facebook import import_observations
from car_market.facebook_browser import CardUnavailable, refresh_known, visible_observation

URL = "https://www.facebook.com/marketplace/np/item/1380906133610682/"
TEXT = (Path(__file__).parent / "fixtures/source_completion/facebook-visible-card.txt").read_text()
CHECKED = "2026-10-07T10:35:50.389790+00:00"


def test_visible_card_newline_location_price_and_explicit_fields():
    record = visible_observation(TEXT, URL, CHECKED)
    assert record["country"] == "NL"
    assert record["city"] == "Luchthaven Schiphol, NH"
    assert (record["price"], record["year"]) == (9500, 2017)
    assert record["transmission"] == "automatic"
    assert record["fuel"] == "petrol"
    assert "Seller information" not in record["description"]
    assert record["mileage"] is None


def test_foreign_item_is_not_made_dutch_by_seller_description():
    text = TEXT.replace("in Luchthaven Schiphol, NH", "in Paris, France") + "\nin Amsterdam, NH"
    with pytest.raises(CardUnavailable, match="nl_location_unconfirmed"):
        visible_observation(text, URL, CHECKED)


def test_refresh_is_partial_and_access_failure_does_not_remove_previous_listing(
    tmp_path, monkeypatch
):
    record = visible_observation(TEXT, URL, CHECKED)
    db = Database(tmp_path / "market.sqlite3")
    import_observations(db, [record])
    from car_market import facebook_browser

    monkeypatch.setattr(facebook_browser, "read_known", lambda urls, profile: ([record], []))
    run = db.start_run("test")
    result = refresh_known(db, run, {"limit": 25, "profile_path": "not-read"})
    assert (result["status"], result["refreshed"]) == ("partial", 1)
    assert (
        db.conn.execute("SELECT transport FROM fetches ORDER BY id DESC LIMIT 1").fetchone()[0]
        == "authenticated_visible_card"
    )
    row = db.conn.execute(
        "SELECT expected_count,status FROM source_runs WHERE query_key='known_refresh' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert tuple(row) == (None, "partial")
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='not_seen_in_complete_query'"
        ).fetchone()[0]
        == 0
    )
    monkeypatch.setattr(
        facebook_browser, "read_known", lambda urls, profile: ([], ["access_required"])
    )
    result = refresh_known(db, db.start_run("test"), {"limit": 25, "profile_path": "not-read"})
    assert result["status"] == "access_required"
    assert db.conn.execute("SELECT availability FROM listings").fetchone()[0] == "observed"
    assert db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    db.conn.close()


def test_refresh_parser_failure_is_not_reported_as_missing_login(tmp_path, monkeypatch):
    from car_market import facebook_browser

    db = Database(tmp_path / "market.sqlite3")
    import_observations(db, [visible_observation(TEXT, URL, CHECKED)])
    monkeypatch.setattr(
        facebook_browser, "read_known", lambda urls, profile: ([], ["nl_location_unconfirmed"])
    )
    result = refresh_known(db, db.start_run("test"), {"profile_path": "not-read"})
    assert result["status"] == "partial"
    db.conn.close()


def test_collector_refresh_requires_browser_broad_pass_and_source_selection(tmp_path, monkeypatch):
    from car_market import facebook_browser
    from car_market.collect import Collector

    db = Database(tmp_path / "market.sqlite3")
    import_observations(db, [visible_observation(TEXT, URL, CHECKED)])
    calls = []
    monkeypatch.setattr(
        facebook_browser,
        "read_known",
        lambda urls, profile: (calls.append(urls) or [visible_observation(TEXT, URL, CHECKED)], []),
    )
    config = {"queries": [], "facebook_refresh": {"enabled": True, "profile_path": "not-read"}}
    collector = Collector(db, config, browser=True)
    collector.run(["facebook"], details=False, checks=False, fast=True)
    collector.run(["gaspedaal"], details=False, checks=False)
    Collector(db, config).run(["facebook"], details=False, checks=False)
    assert not calls
    result = collector.run(["facebook"], details=False, checks=False)
    assert len(calls) == 1
    assert result["status"] == "partial"
    db.conn.close()
