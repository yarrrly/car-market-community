"""Data lineage and unsafe linking regressions; synthetic records test the contract."""

import json
from email.message import Message

import pytest
from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Fetcher, Response
from car_market.parse import Listing, Page
from car_market.quality import report
from car_market.sale_terms import classify


def car(source="dealer:test", external="https://test/1", **fields):
    return Listing(
        source,
        external,
        "https://test/1",
        "BMW 218i Active Tourer",
        year=2015,
        mileage=120000,
        **fields,
    ).qualify()


def test_missing_card_fields_keep_original_evidence(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    db.observe(
        run,
        car(
            fuel="petrol",
            transmission="manual",
            kenteken="AB123C",
            price=8000,
            description="Full seller description",
            images=["https://test/image.jpg"],
            raw={"detail_complete": True},
        ),
        "2026-10-04T10:00:00+00:00",
        None,
    )
    next_run = db.start_run()
    db.observe(next_run, car(price=7900), "2026-10-05T10:00:00+00:00", query_key="search")
    row = db.conn.execute("SELECT * FROM latest_observations").fetchone()
    facts = json.loads(row["details_json"])
    assert facts["scope"] == "target"
    assert facts["images"] == ["https://test/image.jpg"]
    assert facts["kenteken"] == "AB123C"
    assert (
        facts["raw"]["field_evidence"]["transmission"]["observed_at"] == "2026-10-04T10:00:00+00:00"
    )
    assert facts["raw"]["field_evidence"]["transmission"]["inherited"]
    assert not facts["raw"]["field_evidence"]["price"]["inherited"]
    assert db.conn.execute("SELECT scope FROM listings").fetchone()[0] == "target"
    db.conn.close()


def test_cross_run_unknown_price_is_not_old_price(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    db.observe(db.start_run(), car(fuel="petrol", price=8000), "2026-10-04T10:00:00+00:00")
    db.observe(db.start_run(), car(fuel="petrol"), "2026-10-05T10:00:00+00:00")
    assert db.conn.execute("SELECT price_eur FROM market_current").fetchone()[0] is None
    db.conn.close()


def test_linked_dealer_does_not_confirm_identity_by_year_mileage(tmp_path, monkeypatch):
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, {"detail_batch_limit": 5})
    original = car("gaspedaal", "1", fuel="petrol")
    listing_id = db.observe(db.start_run(), original, "2026-10-04T10:00:00+00:00")
    url = "https://dealer.test/1"
    detail = car("dealer:dealer.test", "1", fuel="petrol", kenteken="AB123C")
    detail.url = url
    collector.detail_jobs[url] = ("portal", listing_id, original)
    monkeypatch.setattr(
        collector.fetcher,
        "get",
        lambda _: Response(url, url, 200, "test", "2026-10-05T10:00:00+00:00"),
    )
    monkeypatch.setattr(
        "car_market.collect.parse_page",
        lambda *a: Page([detail]),
    )
    collector.details(db.start_run())
    assert (
        db.conn.execute(
            "SELECT kenteken FROM vehicles WHERE id=(SELECT vehicle_id FROM listings WHERE id=?)",
            (listing_id,),
        ).fetchone()[0]
        is None
    )
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 2
    db.conn.close()


def test_single_target_in_catalogue_is_not_detail_enrichment(tmp_path, monkeypatch):
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, {"detail_batch_limit": 5})
    original = car("gaspedaal", "1", fuel="petrol")
    listing_id = db.observe(db.start_run(), original, "2026-10-04T10:00:00+00:00")
    url = "https://dealer.test/catalogue"
    collector.detail_jobs[url] = ("portal", listing_id, original)
    monkeypatch.setattr(
        collector.fetcher,
        "get",
        lambda _: Response(url, url, 200, "test", "2026-10-05T10:00:00+00:00"),
    )
    monkeypatch.setattr(
        "car_market.collect.parse_page",
        lambda *a: Page(
            [car("dealer:dealer.test", "1", fuel="petrol", kenteken="AB123C")], total=50
        ),
    )
    collector.details(db.start_run())
    assert db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    db.conn.close()


def test_quality_reports_disagreement_without_mutation(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    from car_market.db import now

    db.observe(
        db.start_run(),
        car("one", "1", fuel="petrol", transmission="manual", kenteken="AB123C"),
        now(),
    )
    db.observe(
        db.start_run(),
        car("two", "2", fuel="petrol", transmission="automatic", kenteken="AB123C"),
        now(),
    )
    before = db.conn.total_changes
    result = report(db.conn, {"queries": [{"source": "missing_source"}]})
    assert db.conn.total_changes == before
    assert not result["accuracy_verified"]
    assert result["identity_field_disagreements"][0]["field"] == "transmission"
    assert any(
        s["source"] == "missing_source" and s["observed_ads"] == 0 for s in result["sources"]
    )
    db.conn.close()


def test_anwb_controls_use_distinct_request_bodies_and_cache(monkeypatch):
    calls = []
    url = "https://api.anwb.nl/occasion-portal/v1/occasion-portal/listings/search-with-counters?pageNumber=1"

    class Reply:
        status = 200
        headers = Message()
        headers["Content-Type"] = "application/json; charset=utf-8"

        def __init__(self, body):
            self.body = body
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return json.dumps(self.body).encode()

    def opened(request, **kwargs):
        body = json.loads(request.data)
        calls.append(body)
        return Reply(body)

    monkeypatch.setattr("urllib.request.urlopen", opened)
    fetcher = Fetcher(delay=0)
    fetcher._anwb_headers = {}
    narrow = {
        "brandModel": {"filter": [{"slug": "bmw", "models": [{"slug": "2-serie-active-tourer"}]}]}
    }
    broad = {"brandModel": {"filter": [{"slug": "bmw", "models": [{"slug": "2-serie"}]}]}}
    assert json.loads(fetcher.get(url, request_body=narrow).body) == narrow
    assert json.loads(fetcher.get(url, request_body=broad).body) == broad
    assert json.loads(fetcher.get(url, request_body=narrow).body) == narrow
    assert calls == [narrow, broad]


def test_real_export_quote_is_not_a_dutch_retail_candidate(tmp_path):
    from pathlib import Path

    from car_market.parse import parse_page

    body = (Path(__file__).parent / "fixtures/quality_primary/m2450755184.html").read_text()
    item = parse_page(
        "marktplaats", body, "https://www.marktplaats.nl/v/auto-s/bmw/m2450755184-bmw"
    ).listings[0]
    classify(item)
    assert item.price == 4950  # Preserve the displayed asking amount.
    assert item.price_type == "export_price"
    assert item.raw["seller_stated_nl_price_eur"] == 5450
    db = Database(tmp_path / "market.sqlite3")
    db.observe(db.start_run(), item, "2026-10-07T05:55:00+00:00")
    assert not db.conn.execute("SELECT * FROM purchase_candidates").fetchall()
    db.conn.close()


def test_parser_revision_refreshes_old_success_once(tmp_path, monkeypatch):
    from car_market.db import now

    db = Database(tmp_path / "market.sqlite3")
    original = car(fuel="petrol")
    run = db.start_run()
    listing_id = db.observe(run, original, now())
    db.save_fetch(run, original.source, original.url, original.url, 200, "{}", now())
    config = {"detail_cache_hours": 24, "detail_parser_revision": "new-parser"}
    collector = Collector(db, config)
    collector.queue_details(original, listing_id)
    calls = []
    monkeypatch.setattr(
        collector.fetcher,
        "get",
        lambda url: calls.append(url) or Response(url, url, 200, "{}", now()),
    )
    monkeypatch.setattr("car_market.collect.parse_page", lambda *a: Page([original]))
    collector.details(db.start_run())
    assert calls == [original.url]
    later = Collector(db, config)
    later.queue_details(original, listing_id)
    monkeypatch.setattr(
        later.fetcher,
        "get",
        lambda url: (_ for _ in ()).throw(AssertionError("unexpected second refresh")),
    )
    later.details(db.start_run())
    db.conn.close()


def test_unparsed_200_does_not_confirm_arbitrary_plate(tmp_path, monkeypatch):
    from car_market.db import now

    db = Database(tmp_path / "market.sqlite3")
    original = car("dealer:dealer.test", "https://dealer.test/1", fuel="petrol")
    original.url = "https://dealer.test/1"
    listing_id = db.observe(db.start_run(), original, now())
    collector = Collector(db, {"detail_cache_hours": 24, "detail_parser_revision": "new-parser"})
    collector.queue_details(original, listing_id)
    monkeypatch.setattr(
        collector.fetcher,
        "get",
        lambda url: Response(url, url, 200, "<p>Recommended: Kenteken: AB-123-C</p>", now()),
    )
    monkeypatch.setattr("car_market.collect.parse_page", lambda *a: Page([]))
    collector.details(db.start_run())
    assert db.conn.execute("SELECT kenteken FROM vehicles").fetchone()[0] is None
    assert (
        db.conn.execute("SELECT error FROM fetches ORDER BY id DESC LIMIT 1").fetchone()[0]
        == "detail_unparsed"
    )
    later = Collector(db, {"detail_cache_hours": 24, "detail_parser_revision": "new-parser"})
    later.queue_details(original, listing_id)
    monkeypatch.setattr(
        later.fetcher,
        "get",
        lambda url: (_ for _ in ()).throw(AssertionError("parse-error backoff ignored")),
    )
    later.details(db.start_run())
    db.conn.close()


@pytest.mark.parametrize("source", ["dealer:test", "marktnet"])
def test_revision_does_not_bypass_recent_http_denial(tmp_path, monkeypatch, source):
    from car_market.db import now

    db = Database(tmp_path / "market.sqlite3")
    original = car(source, fuel="petrol")
    run = db.start_run()
    listing_id = db.observe(run, original, now())
    db.save_fetch(
        run, original.source, original.url, original.url, 403, None, now(), error="http_403"
    )
    collector = Collector(db, {"detail_cache_hours": 24, "detail_parser_revision": "new-parser"})
    collector.queue_details(original, listing_id)
    monkeypatch.setattr(
        collector.fetcher,
        "get",
        lambda url: (_ for _ in ()).throw(AssertionError("HTTP backoff ignored")),
    )
    collector.details(db.start_run())
    db.conn.close()


def test_detail_recommendation_url_mismatch_is_rejected(tmp_path, monkeypatch):
    from car_market.db import now

    db = Database(tmp_path / "market.sqlite3")
    original = car("dealer:dealer.test", "https://dealer.test/1", fuel="petrol")
    original.url = "https://dealer.test/1"
    listing_id = db.observe(db.start_run(), original, now())
    collector = Collector(db, {})
    collector.queue_details(original, listing_id)
    recommendation = car(
        "dealer:dealer.test", "https://dealer.test/2", fuel="petrol", kenteken="AB123C"
    )
    recommendation.url = "https://dealer.test/2"
    monkeypatch.setattr(
        collector.fetcher, "get", lambda url: Response(url, url, 200, "test", now())
    )
    monkeypatch.setattr("car_market.collect.parse_page", lambda *a: Page([recommendation]))
    collector.details(db.start_run())
    assert db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    assert db.conn.execute("SELECT error FROM fetches").fetchone()[0] == "detail_identity_mismatch"
    db.conn.close()
