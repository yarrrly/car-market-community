import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from car_market.collect import canonical_id
from car_market.parse import parse_page

ROOT = Path(__file__).parent / "fixtures" / "autotrack_uuid"
BODY = (ROOT / "own-detail.html").read_text()
URL = json.loads((ROOT / "provenance.json").read_text())["url"]
UUID = "eb124f9f-66ef-4c99-bf14-f88a0894a54d"


def altered(kind):
    soup = BeautifulSoup(BODY, "html.parser")
    node = soup.select_one("script#__NEXT_DATA__")
    state = json.loads(node.string)
    schema_node = soup.select_one('script[type="application/ld+json"]')
    schema = json.loads(schema_node.string)
    if kind == "next_id":
        state["props"]["pageProps"]["listingDetails"]["id"] = "another-car"
    elif kind == "query":
        state["query"]["slug"] = ["another-car"]
    elif kind == "canonical":
        soup.select_one('link[rel="canonical"]')["href"] = "https://www.autotrack.nl/a/another-car"
    elif kind == "offer_url":
        schema["offers"]["url"] = "https://www.autotrack.nl/a/another-car"
    elif kind == "schema_id":
        schema["offers"]["itemOffered"]["identifier"] = "another-car"
    elif kind == "plate":
        schema["offers"]["itemOffered"]["licensePlate"] = "AA-123-B"
    elif kind == "price":
        schema["offers"]["price"] = 1
    elif kind == "mileage":
        schema["offers"]["itemOffered"]["mileageFromOdometer"]["value"] = 1
    elif kind == "transmission":
        schema["offers"]["itemOffered"]["vehicleTransmission"] = "Handgeschakeld"
    node.string = json.dumps(state)
    schema_node.string = json.dumps(schema)
    return str(soup)


def test_real_uuid_layout_keeps_source_identity_and_own_fields():
    page = parse_page("autotrack", BODY, URL)
    assert len(page.listings) == 1
    item = page.listings[0]
    assert item.source == "autotrack"
    assert item.external_id == canonical_id("autotrack", URL) == URL
    assert item.raw["own_uuid"] == UUID
    assert (item.kenteken, item.price, item.mileage, item.year) == ("KX186B", 9499, 180491, 2016)
    assert (item.fuel, item.transmission, item.scope) == ("petrol", "automatic", "target")
    assert item.seller == "Carstore Tilburg B.V."
    assert item.city == "TILBURG"
    assert len(item.images) >= 10 and "proefrijden" in item.description
    assert item.raw["detail_complete"] is None


@pytest.mark.parametrize(
    "kind", ["next_id", "query", "canonical", "offer_url", "schema_id", "plate"]
)
def test_uuid_layout_rejects_mismatched_identity_without_inventory_fallback(kind):
    assert parse_page("autotrack", altered(kind), URL).listings == []


@pytest.mark.parametrize("kind", ["price", "mileage", "transmission"])
def test_own_payload_schema_disagreement_keeps_field_unknown(kind):
    item = parse_page("autotrack", altered(kind), URL).listings[0]
    assert getattr(item, kind) is None
    assert kind in item.raw["field_conflicts"]
    assert item.kenteken == "KX186B"


def test_uuid_layout_cannot_use_a_foreign_host():
    assert (
        parse_page("autotrack", BODY, URL.replace("www.autotrack.nl", "example.test")).listings
        == []
    )


@pytest.mark.parametrize("kind", ["price", "mileage", "transmission"])
@pytest.mark.parametrize("same_run", [True, False])
def test_conflicting_field_cannot_be_refilled_from_earlier_observation(tmp_path, kind, same_run):
    from car_market.db import Database, now

    db = Database(tmp_path / "market.sqlite3")
    earlier = db.start_run()
    original = parse_page("autotrack", BODY, URL).listings[0]
    listing_id = db.observe(earlier, original, now())
    current = earlier if same_run else db.start_run()
    conflicting = parse_page("autotrack", altered(kind), URL).listings[0]
    assert db.observe(current, conflicting, now()) == listing_id
    row = db.conn.execute(
        "SELECT details_json FROM latest_observations WHERE listing_id=?", (listing_id,)
    ).fetchone()
    stored = json.loads(row[0])
    assert stored[kind] is None
    column = {"price": "price_eur", "mileage": "mileage_km", "transmission": "transmission"}[kind]
    assert (
        db.conn.execute(
            f"SELECT {column} FROM latest_observations WHERE listing_id=?", (listing_id,)
        ).fetchone()[0]
        is None
    )
    assert (
        db.conn.execute(
            f"SELECT {column} FROM market_current WHERE listing_id=?", (listing_id,)
        ).fetchone()[0]
        is None
    )
    assert stored["raw"]["field_evidence"][kind]["conflict"] is True
    assert not stored["raw"]["field_evidence"][kind]["inherited"]
    assert stored["kenteken"] == "KX186B"
    db.conn.close()
