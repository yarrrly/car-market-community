"""Transport, transaction and import boundaries of the DE workflow."""

import dataclasses
import json
import sys
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import pytest
import test_germany_secondary as cases
from car_market import cli
from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Fetcher, Response
from car_market.germany import (
    canonical_de_id,
    observe_german_item,
    qualify_german_item,
    validate_market_path,
)
from car_market.germany_secondary import AUTOHERO_API, secondary_page_request
from car_market.opportunities import evaluate
from car_market.quality_specific_dealers import parse_specific_dealer_detail

STAMP = "2026-10-07T20:00:00+00:00"
CONFIG = {"scope": {"country": "DE"}, "workers": 1}


def own_item():
    return cases.retained("autohero-detail.html", "autohero_de").listings[0]


@pytest.mark.parametrize("managed", [True, False])
def test_conflicting_vin_preserves_outer_writes_and_original_error(tmp_path, managed):
    db = Database(tmp_path / "market.sqlite3")
    Collector(db, CONFIG)
    run = db.start_run()
    item = own_item()
    observe_german_item(db, run, item, STAMP)
    conflict = dataclasses.replace(item, vin="WBA12345678901234", raw=dict(item.raw))

    def attempt():
        db.conn.execute("INSERT INTO monitor_state VALUES ('outer_sentinel','retained')")
        original = list(db.conn.iterdump())
        with pytest.raises(ValueError):
            observe_german_item(db, run, conflict, STAMP)
        assert list(db.conn.iterdump()) == original
        assert db.conn.in_transaction

    if managed:
        with db.transaction():
            attempt()
    else:
        db.conn.execute("BEGIN")
        attempt()
        db.conn.commit()
    assert (
        db.conn.execute("SELECT value FROM monitor_state WHERE key='outer_sentinel'").fetchone()[0]
        == "retained"
    )
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("page", [1, 2, 3])
def test_fetcher_honors_exact_explicit_autohero_body(page):
    spec = secondary_page_request(
        "autohero_de", cases.MANIFEST["autohero-first.html"]["requested_url"], page
    )
    outgoing = []

    class HTTPResponse:
        url, status = AUTOHERO_API, 200
        headers = Message()
        headers["Content-Type"] = "application/json; charset=utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return b"{}"

    def capture(request, timeout):
        outgoing.append(json.loads(request.data))
        return HTTPResponse()

    with patch("urllib.request.urlopen", capture):
        response = Fetcher(delay=0, locale="de-DE").get(spec["url"], request_body=spec["body"])
    assert not response.error
    assert outgoing == [spec["body"]]
    assert outgoing[0]["variables"]["search"]["offset"] == (page - 1) * 24


def test_collector_autohero_uses_distinct_page_bodies_and_context(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, CONFIG)
    url = cases.MANIFEST["autohero-first.html"]["requested_url"]
    query = {
        "source": "autohero_de",
        "key": "native",
        "url": url,
        "pagination": "secondary_de",
        "coverage": "enumerate",
    }
    state = cases.autohero_state()["ROOT_QUERY"]
    payload = next(v for k, v in state.items() if k.startswith("searchAdV9AdsV2:"))
    first = json.dumps({"data": {"searchAdV9AdsV2": payload}})
    last = (cases.FIXTURES / "autohero-last.json").read_text()
    requests = []

    def fetch(url, refresh=False, request_body=None):
        assert request_body is not None
        search = request_body["variables"]["search"]
        requests.append(search)
        assert search["filter"]["value"][0]["value"] == "DE"
        return Response(url, url, 200, first if search["offset"] == 0 else last, STAMP)

    collector.fetcher.get = fetch
    collector.collect_query(db.start_run(), query)
    assert [r["offset"] for r in requests] == [0, 24]
    health = db.conn.execute("SELECT * FROM source_health").fetchone()
    assert (health["status"], health["received_count"], health["expected_count"]) == (
        "complete",
        34,
        34,
    )
    assert all(row[0] == "autohero_de" for row in db.conn.execute("SELECT source FROM listings"))


@pytest.mark.parametrize(
    "name,source", [("autohero-detail.html", "autohero_de"), ("pkw-api-direct.json", "pkw_de")]
)
def test_native_secondary_detail_id_binds_requested_uri(name, source):
    item = cases.retained(name, source).listings[0]
    assert canonical_de_id(source, item.url) == item.external_id
    assert canonical_de_id(source, item.external_id) == item.external_id
    assert canonical_de_id(source, item.url.replace("https://", "https://foreign.example/")) is None


def test_de_snapshot_import_rejected_before_destination_constructor(tmp_path, monkeypatch):
    destination, snapshot = tmp_path / "new.sqlite3", tmp_path / "source.sqlite3"
    db = Database(snapshot)
    Collector(db, CONFIG)
    db.conn.close()
    config = tmp_path / "sources.json"
    config.write_text(json.dumps(CONFIG))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "car-market",
            "--db",
            str(destination),
            "--config",
            str(config),
            "import-db",
            str(snapshot),
        ],
    )
    with pytest.raises(ValueError, match="German country proof"):
        cli.main()
    assert not destination.exists()


@pytest.mark.parametrize("country", [None, "NL", "FR"])
@pytest.mark.parametrize("direct", [True, False])
def test_country_guard_rejects_before_any_identity(tmp_path, country, direct):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    item = own_item()
    item.raw["market_country"] = country
    original = list(db.conn.iterdump())
    with pytest.raises(ValueError, match="country proof"):
        if direct:
            db.observe(run, item, STAMP)
        else:
            observe_german_item(db, run, item, STAMP)
    assert list(db.conn.iterdump()) == original


def test_enrichment_retains_own_model_and_never_uses_german_plate(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    item = own_item()
    item.description = "Weitere Angebote: BMW 220i, BMW 218d"
    item.kenteken = "AB123C"
    listing = observe_german_item(db, run, item, STAMP)
    assert (
        db.conn.execute("SELECT scope FROM listings WHERE id=?", (listing,)).fetchone()[0]
        == "target"
    )
    assert db.conn.execute("SELECT kenteken FROM vehicles").fetchone()[0] is None
    assert item.raw["registration_identifier"] == "AB123C"


@pytest.mark.parametrize(
    "unavailable,state", [("sold", "seller_marked_sold"), ("reserved", "seller_reserved")]
)
def test_availability_uses_actual_time_and_requires_explicit_reopening(
    tmp_path, unavailable, state
):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()

    def observe(stamp, declared):
        item = own_item()
        item.raw["availability_status"] = declared
        return observe_german_item(db, run, item, stamp)

    listing = observe("2026-10-07T12:00:00Z", "available")
    observe("2026-10-07T12:30:00+02:00", unavailable)
    assert (
        db.conn.execute("SELECT availability FROM listings WHERE id=?", (listing,)).fetchone()[0]
        == "observed"
    )
    observe("2026-10-07T15:00:00+02:00", unavailable)
    observe("2026-10-07T12:45:00Z", "available")
    observe("2026-10-07T13:10:00Z", None)
    assert (
        db.conn.execute("SELECT availability FROM listings WHERE id=?", (listing,)).fetchone()[0]
        == state
    )
    observe("2026-10-07T13:20:00Z", "available")
    assert (
        db.conn.execute("SELECT availability FROM listings WHERE id=?", (listing,)).fetchone()[0]
        == "observed"
    )


@pytest.mark.parametrize(
    "text,kind",
    [
        ("Exportpreis 9.000 EUR; nur Export", "export_price"),
        ("9.000 EUR exkl. MwSt.", "net_price"),
        ("Anzahlung 9.000 EUR", "deposit_price"),
        ("Andere Angebote Exportpreis 8.000 EUR", "advertised_price"),
    ],
)
def test_conditional_price_is_bound_to_own_amount(text, kind):
    item = own_item()
    item.price, item.price_type, item.description = 9000, "advertised_price", text
    qualify_german_item(item)
    assert item.price_type == kind


def test_optional_monthly_terms_do_not_replace_gross_cash():
    item = own_item()
    item.price, item.price_type = 9000, "advertised_price"
    item.raw["leasing"] = {"monthlyRate": 299}
    qualify_german_item(item)
    assert (item.price, item.price_type) == (9000, "advertised_price")
    item.price = 299
    qualify_german_item(item)
    assert item.price_type == "monthly_price"


def test_market_scope_check_is_read_only_and_rejects_other_country(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    Collector(db, CONFIG)
    original = list(db.conn.iterdump())
    with pytest.raises(ValueError, match="country"):
        validate_market_path(db.path, "NL")
    assert list(db.conn.iterdump()) == original


def test_de_inventory_has_no_nl_budget_limits_or_rdw_requirements(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    Collector(db, CONFIG)
    run = db.start_run()
    item = own_item()
    item.price, item.year, item.mileage = 75000, 2024, None
    item.description = "Standort Deutschland, unfallfrei, deutsche Zulassung"
    observe_german_item(db, run, item, STAMP)
    report = evaluate(db.conn, {**CONFIG, "as_of": STAMP})
    assert report["comparison_cars"] == 1
    assert report["cars"][0]["price_eur"] == 75000
    assert report["cars"][0]["risk_matches"] == []
    assert "registration and RDW" not in report["cars"][0]["missing_checks"]
    assert report["market_country"] == "DE"
    assert db.conn.execute("SELECT count(*) FROM purchase_candidates").fetchone()[0] == 1


def test_new_own_model_correction_excludes_existing_target_but_stale_does_not(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    listing = observe_german_item(db, run, own_item(), STAMP)

    def corrected(stamp):
        item = own_item()
        item.title = "BMW 218i Gran Tourer"
        item.raw.update(model_family="2er", model_variant="218i Gran Tourer")
        return observe_german_item(db, run, item, stamp)

    corrected("2026-10-06T20:00:00Z")
    assert (
        db.conn.execute("SELECT scope FROM listings WHERE id=?", (listing,)).fetchone()[0]
        == "target"
    )
    assert corrected("2026-10-07T21:00:00Z") == listing
    assert (
        db.conn.execute("SELECT scope FROM listings WHERE id=?", (listing,)).fetchone()[0]
        == "excluded"
    )
    assert db.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 1
    assert evaluate(db.conn, {**CONFIG, "as_of": "2026-10-07T21:00:00Z"})["comparison_cars"] == 0


def test_wanted_ad_is_not_a_vehicle_for_sale(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    item = own_item()
    item.title = "Suche BMW 218i Active Tourer"
    assert observe_german_item(db, db.start_run(), item, STAMP) is None
    assert db.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 0


def test_unmarked_dealer_database_needs_positive_country_proof(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    item = own_item()
    item.source = "dealer:legacy.example"
    item.raw["market_country"] = "NL"
    item.raw.pop("market_context_country", None)
    db.observe(db.start_run(), item, STAMP)
    original = list(db.conn.iterdump())
    with pytest.raises(ValueError, match="German country proof"):
        validate_market_path(db.path, "DE")
    assert list(db.conn.iterdump()) == original


def test_price_unchanged_publication_proof_and_equipment_changes_are_preserved(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    item = own_item()
    listing = observe_german_item(db, run, item, STAMP)
    updated = own_item()
    updated.raw["own_metadata_fields"] = {"equipment": ["Panoramadach"]}
    updated.raw["publication_reference"] = {
        "kind": "explicit_own_publication_link",
        "source": "autohero_de",
        "url": item.url,
        "external_id": item.external_id,
    }
    observe_german_item(db, db.start_run(), updated, "2026-10-07T20:10:00Z")
    payload = json.loads(
        db.conn.execute(
            "SELECT details_json FROM latest_observations WHERE listing_id=?", (listing,)
        ).fetchone()[0]
    )
    assert payload["raw"]["own_metadata_fields"]["equipment"] == ["Panoramadach"]
    assert payload["raw"]["publication_reference"] == updated.raw["publication_reference"]
    assert db.conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 2


def test_bound_de_database_rejects_real_nl_generic_dealer_before_identity(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    Collector(db, CONFIG)
    base = Path(__file__).parent / "fixtures/specific_dealers"
    body = (base / "own-140052008.html").read_text()
    url = json.loads((base / "provenance.json").read_text())["own-140052008.html"]["final_url"]
    item = parse_specific_dealer_detail(body, url).listings[0]
    run = db.start_run()
    original = list(db.conn.iterdump())
    with pytest.raises(ValueError, match="German country proof"):
        db.observe(run, item, STAMP)
    assert not db.conn.execute("SELECT 1 FROM vehicles").fetchone()
    assert not db.conn.execute("SELECT 1 FROM listings").fetchone()
    assert list(db.conn.iterdump()) == original


def test_de_export_cannot_replace_reserved_nl_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    db = Database(tmp_path / "source.sqlite3")
    Collector(db, CONFIG)
    destination = tmp_path / "src/first-car-nl/exports"
    destination.mkdir(parents=True)
    sentinel = destination / "market.sqlite3"
    sentinel.write_bytes(b"preserve previous NL snapshot")
    with pytest.raises(ValueError, match="reserved"):
        db.export(destination)
    assert sentinel.read_bytes() == b"preserve previous NL snapshot"
    assert sorted(x.name for x in destination.iterdir()) == ["market.sqlite3"]


def test_de_detail_cannot_accept_own_a_redirect_to_own_b(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, CONFIG)
    run = db.start_run()
    destination = own_item()
    original = next(
        item
        for item in cases.retained("autohero-first.html", "autohero_de").listings
        if item.scope == "target" and item.external_id != destination.external_id
    )
    listing = observe_german_item(db, run, original, STAMP)
    collector.queue_details(original, listing)
    body = (cases.FIXTURES / "autohero-detail.html").read_text()
    collector.fetcher.get = lambda url: Response(url, destination.url, 200, body, STAMP)
    collector.details(run)
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 1
    assert not db.conn.execute(
        "SELECT 1 FROM listings WHERE external_id=?", (destination.external_id,)
    ).fetchone()
    assert collector.detail_stats[original.source]["parsed"] == 0
    assert db.conn.execute("SELECT error FROM fetches").fetchone()[0] == "detail_identity_mismatch"


def test_de_explicit_publisher_detail_is_observed_independently(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, CONFIG)
    base = Path(__file__).parent / "fixtures/germany_kleinanzeigen"
    metadata = next(
        x for x in json.loads((base / "manifest.json").read_text()) if x["fixture"] == "detail.html"
    )
    original = collector._parse(
        "kleinanzeigen_de", (base / "detail.html").read_text(), metadata["final_url"]
    ).listings[0]
    destination = own_item()
    original.links = [{"label": "publisher", "url": destination.url}]
    run = db.start_run()
    listing = observe_german_item(db, run, original, STAMP)
    collector.queue_details(original, listing)
    collector.detail_jobs = {destination.url: collector.detail_jobs[destination.url]}
    body = (cases.FIXTURES / "autohero-detail.html").read_text()
    collector.fetcher.get = lambda url: Response(url, url, 200, body, STAMP)
    collector.details(run)
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 2
    assert collector.detail_stats[original.source]["parsed"] == 1
    assert (
        db.conn.execute(
            "SELECT v.vin FROM vehicles v JOIN listings l ON l.vehicle_id=v.id WHERE l.id=?",
            (listing,),
        ).fetchone()[0]
        is None
    )
    assert (
        db.conn.execute(
            "SELECT v.vin FROM vehicles v JOIN listings l ON l.vehicle_id=v.id WHERE l.source='autohero_de'"
        ).fetchone()[0]
        == destination.vin
    )


@pytest.mark.parametrize("status", [404, 410])
@pytest.mark.parametrize(
    "case", ["foreign", "other_ad", "other_request", "stale", "naive", "fresh", "equal"]
)
def test_de_http_removal_requires_native_identity_and_current_timestamp(tmp_path, status, case):
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, CONFIG)
    item = own_item()
    run = db.start_run()
    listing = observe_german_item(db, run, item, STAMP)
    collector.queue_details(item, listing)
    # The job predates a later search sighting: compare the current persisted last_seen.
    observe_german_item(db, run, own_item(), "2026-10-07T20:10:00Z")
    other_url = item.url.replace(item.external_id, "11111111-1111-4111-8111-111111111111")
    final_url = {"foreign": "https://foreign.invalid/ad", "other_ad": other_url}.get(case, item.url)
    requested_url = other_url if case == "other_request" else item.url
    stamp = {
        "stale": "2026-10-07T20:05:00Z",
        "naive": "2026-10-07T20:11:00",
        "equal": "2026-10-07T22:10:00+02:00",
    }.get(case, "2026-10-07T20:11:00Z")
    collector.fetcher.get = lambda url: Response(
        requested_url, final_url, status, None, stamp, "HTTP error"
    )
    collector.details(run)
    removed = case in {"fresh", "equal"}
    assert (
        db.conn.execute("SELECT availability FROM listings WHERE id=?", (listing,)).fetchone()[0]
        == "removed"
    ) == removed
    assert db.conn.execute("SELECT count(*) FROM events WHERE kind='http_removed'").fetchone()[
        0
    ] == int(removed)
    assert db.conn.execute("SELECT count(*) FROM fetches").fetchone()[0] == 1


def test_direct_audit_import_rejects_bound_de_before_start_run(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    Collector(db, CONFIG)
    before = list(db.conn.iterdump())
    with (
        patch.object(db, "start_run", side_effect=AssertionError("must not start")),
        pytest.raises(ValueError, match="German country proof"),
    ):
        cli.import_audit(db, tmp_path / "missing")
    assert list(db.conn.iterdump()) == before


def test_direct_de_plate_attachment_rejects_before_mutation(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    Collector(db, CONFIG)
    listing = observe_german_item(db, db.start_run(), own_item(), STAMP)
    before = list(db.conn.iterdump())
    with pytest.raises(ValueError, match="German registration"):
        db.attach_identity(listing, kenteken="B-AB-123", vin="WBA12345678901234")
    assert list(db.conn.iterdump()) == before


def test_nl_audit_import_and_plate_attachment_remain_available(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    item = own_item()
    item.source, item.raw, item.vin = "autohero", {}, None
    listing = db.observe(db.start_run(), item, STAMP)
    db.attach_identity(listing, kenteken="AB-123-C")
    assert db.conn.execute("SELECT kenteken FROM vehicles").fetchone()[0] == "AB123C"
    before = db.conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    cli.import_audit(db, tmp_path)
    assert db.conn.execute("SELECT count(*) FROM runs").fetchone()[0] == before + 1


def test_discovery_failure_records_error_and_allows_next_query(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, CONFIG)
    run = db.start_run()
    item = own_item()
    listing = observe_german_item(db, run, item, STAMP, key="invalid_budget")
    with (
        patch(
            "car_market.germany_discovery.collect_discovery",
            side_effect=ValueError("invalid budget"),
        ),
        patch("car_market.germany_bmw_collect.collect_bmw") as next_query,
    ):
        collector.collect_query(run, {"source": "quoka_de", "key": "invalid_budget"})
        collector.collect_query(run, {"source": "bmw_de", "key": "next"})
    next_query.assert_called_once()
    row = db.conn.execute("SELECT status,error FROM source_runs WHERE source='quoka_de'").fetchone()
    assert tuple(row) == ("error", "invalid budget")
    assert (
        db.conn.execute("SELECT availability FROM listings WHERE id=?", (listing,)).fetchone()[0]
        != "not_seen"
    )


@pytest.mark.parametrize("conflicts", [0, 1])
def test_de_cli_enrich_bridges_before_candidates_and_retains_summary(
    tmp_path, monkeypatch, capsys, conflicts
):
    config = tmp_path / "sources.json"
    config.write_text(json.dumps(CONFIG))
    database = tmp_path / "market.sqlite3"
    events = []
    result = {"conflicts": conflicts, "merged": 0}
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "market",
            "--db",
            str(database),
            "--config",
            str(config),
            "--export-dir",
            str(tmp_path / "export"),
            "enrich",
            "--no-rdw",
        ],
    )
    with (
        patch.object(Collector, "details", side_effect=lambda run: events.append("details")),
        patch(
            "car_market.germany_identity.bridge_de_publications",
            side_effect=lambda db: events.append("bridge") or result,
        ),
        patch.object(Database, "candidates", side_effect=lambda: events.append("candidates")),
        patch.object(Database, "export", return_value={}),
    ):
        cli.main()
    assert events == ["details", "bridge", "candidates"]
    assert json.loads(capsys.readouterr().out)["publisher_bridges"] == result
    db = Database(database)
    row = db.conn.execute("SELECT status,summary FROM runs").fetchone()
    assert row["status"] == ("partial" if conflicts else "complete")
    assert json.loads(row["summary"])["publisher_bridges"] == result
