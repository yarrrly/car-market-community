"""Real Marktnet fuel evidence, alias deduplication and detail budget contracts."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from car_market.collect import Collector
from car_market.db import Database, now
from car_market.fetch import Response
from car_market.marktnet import parse_xml, stated_fuel
from car_market.parse import Listing, Page, fuel

FIXTURES = Path(__file__).parent / "fixtures/marktnet_detail_quality"


def own_car():
    return parse_xml((FIXTURES / "94560620.xml").read_text()).listings[0]


@pytest.mark.parametrize("name", ["94560620", "95004295", "95054841"])
def test_real_petrol_detail_is_not_a_hybrid_because_of_electronic_equipment(name):
    item = parse_xml((FIXTURES / (name + ".xml")).read_text()).listings[0]
    assert "Elektronisch" in item.description
    assert item.fuel == "petrol"
    assert item.scope == "target"
    assert not item.raw["detail_complete"]
    assert item.raw["description_may_be_truncated"]


def test_explicit_fuel_keeps_mixed_energy_and_unknown_separate():
    assert stated_fuel("Brandstofsoort: Benzine Transmissie: Automaat Elektronisch ESP") == "petrol"
    assert stated_fuel("Brandstof: Benzine / Elektriciteit Transmissie: Automaat") == "hybrid"
    assert stated_fuel("Brandstof: Diesel Tankinhoud: 50 liter") == "diesel"
    assert stated_fuel("Elektronisch ESP, wij verkopen benzine en hybride autos") is None
    assert fuel("Benzine / Elektriciteit") == "hybrid"
    assert fuel("Benzine Elektronisch Stabiliteits Programma") == "petrol"


def test_marktnet_own_html_and_full_xml_are_one_job(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    item = own_car()
    item.url = "https://www.marktnet.nl/94560620"
    item.links = [{"url": "https://www.marktnet.nl/advertentie.xml?id=94560620"}]
    listing = db.observe(db.start_run(), item, now())
    collector = Collector(db, {})
    collector.queue_details(item, listing)
    assert list(collector.detail_jobs) == ["https://www.marktnet.nl/advertentie.xml?id=94560620"]
    assert collector.detail_jobs[next(iter(collector.detail_jobs))][0] == "marktnet"
    db.conn.close()


def test_source_parser_revision_refreshes_old_200_failure_once(tmp_path, monkeypatch):
    db = Database(tmp_path / "market.sqlite3")
    item = own_car()
    listing = db.observe(db.start_run(), item, now())
    url = "https://www.marktnet.nl/advertentie.xml?id=94560620"
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    db.save_fetch(db.start_run(), "marktnet", url, url, 200, "<ad/>", old, error="detail_unparsed")
    cfg = {"detail_cache_hours": 24, "detail_parser_revisions": {"marktnet": "explicit-fuel-v1"}}
    collector = Collector(db, cfg)
    collector.queue_details(item, listing)
    calls = []
    monkeypatch.setattr(
        collector.fetcher,
        "get",
        lambda request: (
            calls.append(request)
            or Response(request, request, 200, (FIXTURES / "94560620.xml").read_text(), now())
        ),
    )
    collector.details(db.start_run())
    assert calls == [url]
    assert collector.detail_stats["marktnet"]["parsed"] == 1
    later = Collector(db, cfg)
    later.queue_details(item, listing)
    monkeypatch.setattr(
        later.fetcher,
        "get",
        lambda request: (_ for _ in ()).throw(AssertionError("repeated parser refresh")),
    )
    later.details(db.start_run())
    db.conn.close()


@pytest.mark.parametrize("status", [403, 429])
def test_price_change_and_xml_alias_do_not_bypass_recent_denial(tmp_path, monkeypatch, status):
    db = Database(tmp_path / "market.sqlite3")
    item = own_car()
    item.url = "https://www.marktnet.nl/94560620"
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    listing = db.observe(db.start_run(), item, old)
    denied = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    db.save_fetch(
        db.start_run(),
        "marktnet",
        item.url,
        item.url,
        status,
        None,
        denied,
        error="http_" + str(status),
    )
    item.price += 100
    db.observe(db.start_run(), item, now())
    collector = Collector(
        db, {"detail_cache_hours": 24, "detail_parser_revisions": {"marktnet": "explicit-fuel-v1"}}
    )
    collector.queue_details(item, listing)
    monkeypatch.setattr(
        collector.fetcher,
        "get",
        lambda request: (_ for _ in ()).throw(AssertionError("denial bypass")),
    )
    collector.details(db.start_run())
    db.conn.close()


def test_detail_budget_serves_second_source_despite_first_source_backlog(tmp_path, monkeypatch):
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, {"detail_batch_limit": 2})
    items = {}
    for host, count in [("first.test", 10), ("second.test", 1)]:
        for n in range(count):
            url = f"https://{host}/{n}"
            item = Listing(
                "dealer:" + host,
                url,
                url,
                "BMW 218i Active Tourer",
                fuel="petrol",
                price=8000,
                transmission="manual",
            ).qualify()
            listing = db.observe(db.start_run(), item, now())
            collector.queue_details(item, listing)
            items[url] = item
    calls = []
    monkeypatch.setattr(
        collector.fetcher,
        "get",
        lambda url: calls.append(url) or Response(url, url, 200, "test", now()),
    )
    monkeypatch.setattr(
        "car_market.collect.parse_market_page", lambda source, body, url: Page([items[url]])
    )
    collector.details(db.start_run())
    assert calls == ["https://first.test/0", "https://second.test/0"]
    assert collector.detail_stats["dealer:first.test"]["eligible"] == 10
    assert collector.detail_stats["dealer:second.test"]["parsed"] == 1
    db.conn.close()


@pytest.mark.parametrize(
    "name,source,url",
    [
        (
            "poelgeest",
            "dealer:www.van-poelgeest.nl",
            "https://www.van-poelgeest.nl/voorraad/bmw/2-serie/active-tourer-218i-m-sport-comfort-access-panoramadak-stoelverwarming-stuurverwarming-285854804/",
        ),
        (
            "wijnne",
            "dealer:www.wijnne.nl",
            "https://www.wijnne.nl/occasions/2023-bmw-2-serie-218i-136pk-aut-active-tourer-luxury-line-38297km/56274120/",
        ),
    ],
)
def test_real_canonical_alias_requires_same_published_stock_id(name, source, url):
    from car_market.collect import detail_identity_matches, parse_market_page

    body = (Path(__file__).parent / "fixtures/detail_identity" / (name + ".html")).read_text()
    item = parse_market_page(source, body, url).listings[0]
    assert detail_identity_matches(source, item.url, url, body)
    assert not detail_identity_matches(
        source,
        item.url,
        url.replace("285854804", "999999999").replace("56274120", "999999999"),
        body,
    )
    assert not detail_identity_matches(
        source, "https://" + source.removeprefix("dealer:") + "/", url, body
    )


def test_budget_cursor_survives_next_collector_and_serves_waiting_source(tmp_path, monkeypatch):
    db = Database(tmp_path / "market.sqlite3")
    jobs = []
    for host in ["first.test", "second.test"]:
        url = "https://" + host + "/1"
        item = Listing(
            "dealer:" + host, url, url, "BMW 218i Active Tourer", fuel="petrol"
        ).qualify()
        jobs.append((item, db.observe(db.start_run(), item, now())))
    calls = []
    for expected in ["first.test", "second.test"]:
        collector = Collector(db, {"detail_batch_limit": 1})
        for item, listing in jobs:
            collector.queue_details(item, listing)
        monkeypatch.setattr(
            collector.fetcher,
            "get",
            lambda url: calls.append(url) or Response(url, url, 200, "test", now()),
        )
        monkeypatch.setattr(
            "car_market.collect.parse_market_page",
            lambda source, body, url: Page(
                [next(item for item, listing in jobs if item.url == url)]
            ),
        )
        collector.details(db.start_run())
        assert calls[-1] == "https://" + expected + "/1"
    db.conn.close()


def test_redirect_to_dealer_homepage_never_enriches_recommended_car(tmp_path, monkeypatch):
    db = Database(tmp_path / "market.sqlite3")
    item = Listing(
        "dealer:example.test",
        "https://example.test/car/1",
        "https://example.test/car/1",
        "BMW 218i Active Tourer",
        fuel="petrol",
    ).qualify()
    listing = db.observe(db.start_run(), item, now())
    collector = Collector(db, {})
    collector.queue_details(item, listing)
    monkeypatch.setattr(
        collector.fetcher,
        "get",
        lambda url: Response(url, "https://example.test/", 200, "test", now()),
    )
    monkeypatch.setattr(
        "car_market.collect.parse_market_page",
        lambda *args: (_ for _ in ()).throw(AssertionError("homepage passed to detail parser")),
    )
    collector.details(db.start_run())
    assert db.conn.execute("select error from fetches").fetchone()[0] == "detail_not_advertisement"
    assert db.conn.execute("select count(*) from listings").fetchone()[0] == 1
    db.conn.close()


def test_old_false_complete_flag_does_not_survive_marktnet_excerpt(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    item = own_car()
    item.raw["detail_complete"] = True
    db.observe(db.start_run(), item, (datetime.now(UTC) - timedelta(minutes=10)).isoformat())
    later = own_car()
    db.observe(db.start_run(), later, now(), query_key="search")
    import json

    facts = json.loads(
        db.conn.execute("select details_json from latest_observations").fetchone()[0]
    )
    assert facts["raw"]["description_may_be_truncated"]
    assert not facts["raw"]["detail_complete"]
    assert facts["fuel"] == "petrol"
    db.conn.close()


def test_recent_final_url_denial_is_not_hidden_by_later_imported_old_fetch(tmp_path, monkeypatch):
    db = Database(tmp_path / "market.sqlite3")
    item = Listing(
        "dealer:example.test",
        "https://example.test/car/1",
        "https://example.test/car/1",
        "BMW 218i Active Tourer",
        fuel="petrol",
    ).qualify()
    listing = db.observe(db.start_run(), item, now())
    db.save_fetch(
        db.start_run(),
        item.source,
        "https://portal.test/redirect/1",
        item.url,
        403,
        None,
        now(),
        error="http_403",
    )
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    db.save_fetch(db.start_run(), item.source, item.url, item.url, 200, "archive", old)
    collector = Collector(db, {"detail_cache_hours": 24})
    collector.queue_details(item, listing)
    monkeypatch.setattr(
        collector.fetcher,
        "get",
        lambda url: (_ for _ in ()).throw(
            AssertionError("recent redirected denial bypassed by archive import")
        ),
    )
    collector.details(db.start_run())
    db.conn.close()


def test_specific_dealer_keeps_actual_host_source_namespace():
    from car_market.collect import parse_market_page

    body = (Path(__file__).parent / "fixtures/specific_dealers/own-142022179.html").read_text()
    source = "dealer:www.linkerbaan.com"
    url = "https://www.linkerbaan.com/voertuig/bmw-2-active-tourer-218i-m-sport-panodak-harman-kardon-head-up-driving-assistant"
    item = parse_market_page(source, body, url).listings[0]
    assert item.source == source
    assert item.mileage == 60051
    assert item.kenteken == "KTN77L"


def test_rejected_autowereld_payload_cannot_fall_back_to_related_schema():
    from car_market.collect import parse_market_page

    body = (Path(__file__).parent / "fixtures/autowereld_details/own-43850542.html").read_text()
    url = "https://www.autowereld.nl/bmw/2-serie-active-tourer/218i-m-sport-panodak-harman-kardon-head-up-driving-assistant-43850542/details.html"
    assert parse_market_page("autowereld", body, url).listings
    rejected = body.replace('"nr":43850542', '"nr":43593530').replace(
        '"nr": "43850542"', '"nr": "43593530"'
    )
    # Simulate a different requested own URL; do not accept the retained schema as a recommendation.
    assert not parse_market_page(
        "autowereld", rejected, url.replace("43850542", "43593530")
    ).listings


@pytest.mark.parametrize(
    "field,status", [("sold", "seller_marked_sold"), ("reserved", "seller_reserved")]
)
def test_own_dealer_availability_claim_is_not_left_as_observed(
    tmp_path, monkeypatch, field, status
):
    from car_market.collect import parse_market_page

    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, {})
    body = (Path(__file__).parent / "fixtures/specific_dealers/own-142022179.html").read_text()
    url = "https://www.linkerbaan.com/voertuig/bmw-2-active-tourer-218i-m-sport-panodak-harman-kardon-head-up-driving-assistant"
    source = "dealer:www.linkerbaan.com"
    item = parse_market_page(source, body, url).listings[0]
    listing = db.observe(db.start_run(), item, now())
    collector.queue_details(item, listing)
    item.raw[field] = True
    monkeypatch.setattr(
        collector.fetcher, "get", lambda request: Response(request, request, 200, "test", now())
    )
    monkeypatch.setattr("car_market.collect.parse_market_page", lambda *args: Page([item]))
    collector.details(db.start_run())
    db.candidates()
    assert (
        db.conn.execute("select availability from listings where id=?", (listing,)).fetchone()[0]
        == status
    )
    assert not db.conn.execute("select 1 from purchase_candidates").fetchone()
    db.conn.close()


def test_old_observation_does_not_clear_newer_reserved_state(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    item = own_car()
    listing = db.observe(db.start_run(), item, now())
    db.conn.execute("update listings set availability='seller_reserved' where id=?", (listing,))
    db.conn.commit()
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    db.observe(db.start_run(), item, old, query_key="archive")
    assert (
        db.conn.execute("select availability from listings where id=?", (listing,)).fetchone()[0]
        == "seller_reserved"
    )
    db.conn.close()


def test_existing_version_one_gets_fetch_indexes_without_rewriting_observations(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    item = own_car()
    db.observe(db.start_run(), item, now())
    before = db.conn.execute("select count(*) from observations").fetchone()[0]
    db.conn.execute("DROP INDEX fetch_url_time")
    db.conn.execute("DROP INDEX fetch_final_url_time")
    db.conn.execute("PRAGMA user_version=1")
    db.conn.commit()
    db.conn.close()
    migrated = Database(tmp_path / "market.sqlite3")
    assert migrated.conn.execute("pragma user_version").fetchone()[0] == 7
    assert {"fetch_url_time", "fetch_final_url_time"} <= {
        r[1] for r in migrated.conn.execute("pragma index_list(fetches)")
    }
    assert migrated.conn.execute("select count(*) from observations").fetchone()[0] == before
    migrated.conn.close()


def test_historical_import_retains_dates_and_excludes_old_offer_from_buying(tmp_path):
    import json
    import subprocess
    import sys

    source = Database(tmp_path / "source.sqlite3")
    item = own_car()
    item.transmission = "manual"
    item.price = 8000
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    source.observe(source.start_run(), item, old)
    source.conn.close()
    target = tmp_path / "target.sqlite3"
    exports = tmp_path / "exports"
    args = [
        sys.executable,
        "-m",
        "car_market.cli",
        "--db",
        str(target),
        "--export-dir",
        str(exports),
        "import-db",
        str(tmp_path / "source.sqlite3"),
        "--historical",
    ]
    subprocess.run(args, check=True, capture_output=True, text=True)
    db = Database(target)
    row = db.conn.execute("select availability,last_seen from listings").fetchone()
    assert row["availability"] == "historical_only"
    assert row["last_seen"] == old
    assert not db.conn.execute("select 1 from purchase_candidates").fetchone()
    assert not db.conn.execute('select 1 from source_runs where status="complete"').fetchone()
    snapshot = json.loads(
        db.conn.execute("select details_json from latest_observations").fetchone()[0]
    )
    assert snapshot["kenteken"] == item.kenteken
    db.conn.close()


def test_backdated_positive_after_last_seen_cannot_override_later_http_removal(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    item = own_car()
    first = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    between = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    removed = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    listing = db.observe(db.start_run(), item, first)
    db.conn.execute("update listings set availability='removed' where id=?", (listing,))
    db.conn.execute(
        "insert into events(run_id,listing_id,observed_at,kind,current_value) values(?,?,?,?,?)",
        (db.start_run(), listing, removed, "http_removed", "410"),
    )
    db.conn.commit()
    db.observe(db.start_run(), item, between)
    assert (
        db.conn.execute("select availability from listings where id=?", (listing,)).fetchone()[0]
        == "removed"
    )
    db.observe(db.start_run(), item, now())
    assert (
        db.conn.execute("select availability from listings where id=?", (listing,)).fetchone()[0]
        == "observed"
    )
    db.conn.close()
