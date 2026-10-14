"""Coverage regressions; public response excerpts from 2026-10-06 in fixtures/market_coverage."""

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest
from car_market import cli
from car_market.collect import Collector, page_url
from car_market.coverage import report
from car_market.db import Database, now
from car_market.fetch import Fetcher, Response
from car_market.parse import Listing, parse_page

FIXTURES = Path(__file__).parent / "fixtures/market_coverage"


def test_mobiel_sponsored_cards_do_not_inflate_inventory():
    page = parse_page(
        "nederlandmobiel",
        (FIXTURES / "mobiel.html").read_text(),
        "https://www.nederlandmobiel.nl/auto-occasions/29/2699/bmw/2-serie-active-tour",
    )
    assert page.total == len(page.listings) == 15
    assert page.pages == 1
    assert not any("330D" in ad.title for ad in page.listings)


def test_mobiel_and_trovit_pagination_use_observed_routes():
    page = parse_page(
        "trovit", (FIXTURES / "trovit.html").read_text(), "https://autos.trovit.nl/bmw-218i/2"
    )
    assert (page.total, page.pages, page.page) == (639, 26, 2)
    assert page_url(
        {"url": "https://autos.trovit.nl/bmw-218i", "pagination": "trovit"}, 26, page
    ).endswith("/26")
    assert page_url(
        {"url": "https://www.nederlandmobiel.nl/cars", "pagination": "mobiel"}, 2, page
    ).endswith("?p-page=2")


def test_autohero_public_api_uses_whole_inventory_and_correct_price():
    payload = (FIXTURES / "autohero.json").read_text()
    page = parse_page(
        "autohero",
        payload,
        "https://www.autohero.com/v1/retail-customer-gateway/graphql/searchAdV9AdsV2?pageNumber=2",
    )
    assert (page.total, page.pages, page.page) == (403, 17, 2)
    original = json.loads(payload)["data"]["searchAdV9AdsV2"]["data"][0]
    assert page.listings[0].price == original["offerPrice"]["amountMinorUnits"] / 100
    assert page.listings[0].external_id == original["id"]
    assert page.listings[0].scope == "excluded"  # BMW i3, not an Active Tourer.


def test_autokopen_reads_cards_instead_of_aggregate_product_schema():
    page = parse_page(
        "autokopen",
        (FIXTURES / "autokopen.html").read_text(),
        "https://autokopen.nl/auto/bmw/2-serie-active-tourer",
    )
    assert (page.total, page.pages, len(page.listings)) == (674, 29, 2)
    assert page.listings[0].external_id == "12888754"
    assert page.listings[0].price == 8499
    assert page.listings[0].scope == "target"
    assert page.listings[1].price is None  # Price on request, not a monthly payment.


def test_autoweek_counters_metadata_and_hybrid_are_separate():
    page = parse_page(
        "autoweek",
        (FIXTURES / "autoweek.html").read_text(),
        "https://www.autoweek.nl/occasions/bmw/2-serie-tourer/?pagina=2",
    )
    assert (page.total, page.pages, page.page) == (556, 23, 2)
    assert len(page.listings) == 3
    assert page.listings[0].fuel == "hybrid"
    assert page.listings[0].price == 24900
    assert page.listings[0].scope == "excluded"


def test_mobiel_thousands_counter_is_not_truncated():
    page = parse_page(
        "nederlandmobiel", "<p>1.265 Resultaten</p>", "https://www.nederlandmobiel.nl/"
    )
    assert (page.total, page.pages) == (1265, 51)


def test_autohero_card_without_public_detail_url_is_still_counted():
    payload = json.loads((FIXTURES / "autohero.json").read_text())
    del payload["data"]["searchAdV9AdsV2"]["data"][0]["carUrlTitle"]
    page = parse_page("autohero", json.dumps(payload), "https://www.autohero.com/")
    assert len(page.listings) == 2
    assert "#" in page.listings[0].url


def test_browser_failure_does_not_poison_caller_async_loop(monkeypatch):
    fetcher = Fetcher(browser=True)
    caller = threading.get_ident()
    calls = []

    def session():
        calls.append(threading.get_ident())
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        if len(calls) == 1:
            raise ValueError("browser unavailable")
        return "recovered"

    monkeypatch.setattr(fetcher, "_anwb_session", session)

    async def exercise():
        with pytest.raises(ValueError, match="browser unavailable"):
            fetcher.anwb_session()
        assert fetcher.anwb_session() == "recovered"

    asyncio.run(exercise())
    assert all(thread != caller for thread in calls)


def test_unparseable_response_keeps_raw_evidence(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    co = Collector(db, {})
    response = Response(
        "https://www.autohero.com/api", "https://www.autohero.com/api", 200, '{"data": {}}', now()
    )
    with pytest.raises(ValueError, match="inventory missing"):
        co.ingest_response(db.start_run(), "autohero", "inventory", response)
    assert (
        db.conn.execute("SELECT count(*) FROM fetches WHERE raw_path IS NOT NULL").fetchone()[0]
        == 1
    )
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 0
    db.conn.close()


def test_report_ignores_obsolete_queries_and_reports_control_misses(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    for key in ("primary", "control", "obsolete"):
        db.finish_source(run, "source", key, now(), "complete", 1, 1, 1, 1, None)
    ad = Listing(
        "source", "1", "https://example.test/1", "BMW 218i Active Tourer", fuel="petrol"
    ).qualify()
    db.observe(run, ad, now(), query_key="control")
    config = {
        "queries": [
            {"source": "source", "key": "primary", "url": "https://example.test"},
            {
                "source": "source",
                "key": "control",
                "url": "https://example.test",
                "role": "control",
            },
        ]
    }
    result = report(db.conn, config)
    assert len(result["queries"]) == 2
    assert len(result["queries"][1]["missed_by_primary"]) == 1
    assert result["all_configured_queries_complete_and_fresh"]
    assert result["market_100_percent_verified"] is False
    assert result["ads_without_registration"] == 1
    db.conn.close()


def test_broad_controls_are_not_polled_every_five_minutes(tmp_path, monkeypatch):
    db = Database(tmp_path / "market.sqlite3")
    co = Collector(
        db,
        {
            "queries": [
                {"source": "s", "key": "primary"},
                {"source": "s", "key": "control", "cadence": "broad"},
            ]
        },
    )
    calls = []
    monkeypatch.setattr(co, "collect_query", lambda run, q: calls.append(q["key"]))
    co.run(fast=True, details=False, checks=False)
    assert calls == ["primary"]
    calls.clear()
    co.run(details=False, checks=False)
    assert calls == ["primary", "control"]
    db.conn.close()


def test_daemon_runs_new_control_query_before_next_six_hour_slot(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite3"
    db = Database(path)
    run = db.start_run("live")
    db.finish_run(run, "partial", {})
    db.conn.close()
    config = tmp_path / "sources.json"
    config.write_text(
        json.dumps({"queries": [{"source": "s", "key": "new_control", "cadence": "broad"}]})
    )
    monkeypatch.setattr(
        sys, "argv", ["car-market", "--db", str(path), "--config", str(config), "daemon"]
    )

    def collect(args, config):
        assert not args.fast
        assert args.sources is None
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "collect_once", collect)
    with pytest.raises(KeyboardInterrupt):
        cli.main()
