"""Bind public detail payloads to the requested UUID before enrichment."""

import json
from pathlib import Path

import pytest
from car_market.collect import Collector
from car_market.db import Database, now
from car_market.fetch import Response
from car_market.parse import Listing, Page, json_documents, parse_page

FIXTURES = Path(__file__).parent / "fixtures/quality_primary"
UUID = "789ada1c-ded9-465d-93ff-f0783b445b2e"
OTHER = "00000000-0000-4000-8000-000000000001"


def body(source, mutation=None):
    document = next(
        doc
        for doc in json_documents((FIXTURES / (source + "-" + UUID + ".html")).read_text())
        if isinstance(doc, dict) and doc.get("props", {}).get("pageProps", {}).get("listingDetails")
    )
    own = document["props"]["pageProps"]["listingDetails"]
    if mutation:
        mutation(own)
    return (
        '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(document) + "</script>"
    )


@pytest.mark.parametrize("source", ["autoscout24", "autotrader"])
def test_real_detail_rejects_another_payload_uuid(source):
    url = "https://www." + source + ".nl/aanbod/" + UUID
    assert len(parse_page(source, body(source), url).listings) == 1
    assert parse_page(source, body(source, lambda own: own.update(id=OTHER)), url).listings == []


@pytest.mark.parametrize("source", ["autoscout24", "autotrader"])
@pytest.mark.parametrize(
    "web_page",
    [
        "https://www.autoscout24.nl/aanbod/" + OTHER,
        "https://example.test/aanbod/" + UUID,
        {"id": UUID},
    ],
)
def test_detail_rejects_another_or_foreign_publisher_page(source, web_page):
    url = "https://www." + source + ".nl/aanbod/" + UUID
    assert (
        parse_page(source, body(source, lambda own: own.update(webPage=web_page)), url).listings
        == []
    )


@pytest.mark.parametrize("source", ["autoscout24", "autotrader"])
def test_same_publisher_uuid_can_use_its_related_portal_url(source):
    url = "https://www." + source + ".nl/aanbod/" + UUID
    related = "https://www.autoscout24.nl/aanbod/" + UUID
    item = parse_page(source, body(source, lambda own: own.update(webPage=related)), url).listings[
        0
    ]
    assert item.external_id == UUID


def test_foreign_host_cannot_supply_an_owned_uuid_detail():
    assert (
        parse_page(
            "autoscout24", body("autoscout24"), "https://example.test/aanbod/" + UUID
        ).listings
        == []
    )


def test_missing_own_next_cannot_fall_back_to_recommended_schema():
    schema = {
        "@type": "Car",
        "name": "BMW 218i Active Tourer",
        "url": "https://www.autoscout24.nl/aanbod/" + UUID,
        "offers": {"price": 8000},
    }
    assert (
        parse_page(
            "autoscout24",
            '<script type="application/ld+json">' + json.dumps(schema) + "</script>",
            schema["url"],
        ).listings
        == []
    )


def test_explicit_foreign_make_is_not_manufactured_into_bmw():
    url = "https://www.autoscout24.nl/aanbod/" + UUID
    item = parse_page(
        "autoscout24", body("autoscout24", lambda own: own["vehicle"].update(make="MINI")), url
    ).listings[0]
    assert item.title.startswith("MINI ") and item.scope == "excluded"


def test_collector_does_not_rename_a_different_body_uuid_to_requested_id(tmp_path, monkeypatch):
    db = Database(tmp_path / "market.sqlite3")
    url = "https://www.autoscout24.nl/aanbod/" + UUID
    original = Listing(
        "gaspedaal",
        "1",
        "https://www.gaspedaal.nl/#1",
        "BMW 218i Active Tourer",
        fuel="petrol",
        raw={"portalen": [{"klikUrl": url}]},
    ).qualify()
    listing = db.observe(db.start_run(), original, now())
    item = Listing(
        "autoscout24", OTHER, url, "BMW 218i Active Tourer", fuel="petrol", kenteken="GD431V"
    ).qualify()
    collector = Collector(db, {"detail_batch_limit": 1})
    collector.detail_jobs[url] = ("portal", listing, original)
    monkeypatch.setattr(collector.fetcher, "get", lambda _: Response(url, url, 200, "test", now()))
    monkeypatch.setattr("car_market.collect.parse_page", lambda *args: Page([item]))
    collector.details(db.start_run())
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 1
    assert db.conn.execute("SELECT kenteken FROM vehicles").fetchone()[0] is None
    assert db.conn.execute("SELECT error FROM fetches").fetchone()[0] == "detail_identity_mismatch"
    db.conn.close()
