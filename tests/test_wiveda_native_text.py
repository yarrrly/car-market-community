"""Retained Wiveda text is bound without interpreting native strings as HTML."""

import gzip
import hashlib
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from car_market.germany_dealer_inventory import _devalue, parse_wiveda_bundle
from test_provider_owned_detail_bindings import native_field

FIXTURES = Path(__file__).parent / "fixtures" / "wiveda_native_text"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
OWN_ID = "6e3c5760-ce76-4ffa-b247-30a3300912e8"


def retained(meta):
    encoded = (FIXTURES / meta["fixture"]).read_bytes()
    assert hashlib.sha256(encoded).hexdigest() == meta["fixture_sha256"]
    decoded = gzip.decompress(encoded)
    assert hashlib.sha256(decoded).hexdigest() == meta["decoded_sha256"]
    return decoded.decode("utf-8")


@pytest.fixture
def bundle():
    html, payload = MANIFEST
    return {
        "kind": "wiveda_native",
        "html_url": html["public_url"],
        "html": retained(html),
        "payload_url": payload["public_url"],
        "payload": json.loads(retained(payload)),
    }


@pytest.mark.parametrize("meta", MANIFEST, ids=lambda meta: str(meta["fetch_id"]))
def test_original_native_capture_hash_and_public_provenance(meta):
    assert retained(meta)
    assert meta["source"] == "dealer:wiveda.de" and meta["status"] == 200
    assert meta["public_url"].startswith("https://wiveda.de/fahrzeug/" + OWN_ID)
    assert meta["fetched_at"].startswith("2026-10-11T22:51:")
    assert "original_path" not in meta


def test_exact_native_ampersand_keeps_owned_vehicle_country_and_facts(bundle):
    record = _devalue(bundle["payload"])["vehicle-" + OWN_ID]
    soup = BeautifulSoup(bundle["html"], "html.parser")
    assert record["variant"] == "218 i  Active Tourer M Sport Pano ACC Leder H&k"
    assert soup.select_one('[data-testid="contact-card-subtitle"]').get_text() == record["variant"]

    page = parse_wiveda_bundle(bundle, bundle["html_url"])

    assert page.search_filters["parse_status"] == "parsed_own_detail"
    assert len(page.listings) == 1
    item = page.listings[0]
    assert item.external_id == item.url == bundle["html_url"]
    assert item.scope == "target" and item.raw["market_country"] == "DE"
    assert item.raw["identity_evidence"]["native_vehicle_id"] == OWN_ID
    assert item.raw["country_evidence"]["vehicle_id"] == OWN_ID
    assert item.raw["country_evidence"]["payload_url"] == bundle["payload_url"]
    assert item.raw["native_vehicle"] == record
    assert item.raw["model_variant"] == record["variant"]
    assert item.price == record["price"] and item.mileage == record["mileage"]
    assert item.description == record["description"]
    assert item.raw["detail_complete"] is True


@pytest.mark.parametrize(
    "fault", ["subtitle_without_ampersand", "native_literal_entity", "native_literal_markup"]
)
def test_different_plain_text_cannot_borrow_the_matching_native_subtitle(bundle, fault):
    record = _devalue(bundle["payload"])["vehicle-" + OWN_ID]
    if fault == "subtitle_without_ampersand":
        soup = BeautifulSoup(bundle["html"], "html.parser")
        soup.select_one('[data-testid="contact-card-subtitle"]').string = record["variant"].replace(
            "H&k", "Hk"
        )
        bundle["html"] = str(soup)
    else:
        different = "H&amp;k" if fault == "native_literal_entity" else "H<b>&amp;</b>k"
        native_field(bundle, "variant", record["variant"].replace("H&k", different))

    page = parse_wiveda_bundle(bundle, bundle["html_url"])

    assert not page.listings
    assert page.search_filters["parse_status"] == "own_detail_native_binding_failed"


@pytest.mark.parametrize("country", [None, "NL"])
def test_decoded_text_never_borrows_dealer_or_dom_country(bundle, country):
    native_field(bundle, "country", country)

    page = parse_wiveda_bundle(bundle, bundle["html_url"])

    assert not page.listings
    assert page.search_filters["parse_status"] == "own_vehicle_country_not_DE"
