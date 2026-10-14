import gzip
import hashlib
import json
from pathlib import Path

import pytest
from car_market.collect import detail_identity_matches
from car_market.parse import json_documents, parse_page

FIXTURES = Path(__file__).parent / "fixtures" / "generic_schema_types"
PROVENANCE = json.loads((FIXTURES / "provenance.json").read_text())
SOURCE = PROVENANCE["source"]
URL = PROVENANCE["url"]


def fixture_body():
    archive = (FIXTURES / "vanmossel-274469.html.gz").read_bytes()
    assert hashlib.sha256(archive).hexdigest() == PROVENANCE["fixture_gzip_SHA256"]
    body = gzip.decompress(archive)
    assert hashlib.sha256(body).hexdigest() == PROVENANCE["fixture_body_SHA256"]
    return body.decode()


def schema_body(value):
    car = next(doc for doc in json_documents(fixture_body()) if isinstance(doc, dict))
    car["@type"] = value
    return '<script type="application/ld+json">' + json.dumps(car) + "</script>"


def test_native_vanmossel_with_numeric_packed_metadata():
    body = fixture_body()
    page = parse_page(SOURCE, body, URL)
    assert len(page.listings) == 1
    item = page.listings[0]
    assert (item.price, item.year, item.vin, item.scope) == (
        16940,
        2018,
        "WBA6S110X0VF82426",
        "target",
    )
    assert item.url == URL
    assert detail_identity_matches(SOURCE, item.url, URL, body, advertisement_id=item.external_id)


@pytest.mark.parametrize(
    "value",
    [
        84,
        None,
        True,
        3.5,
        {"Car": True},
        [84, None],
        [["Car"]],
        [{"@type": "Car"}],
        "car",
        "CAR",
        "Carriage",
    ],
)
def test_unsupported_or_wrong_case_car_types_stay_empty(value):
    assert parse_page(SOURCE, schema_body(value), URL).listings == []


@pytest.mark.parametrize(
    "value",
    [
        "Car",
        "Vehicle",
        "Product",
        ["Car"],
        ["Thing", "Car"],
        ["Vehicle", "Product"],
        [84, "Car", None, ["Product"]],
    ],
)
def test_only_valid_string_types_preserve_native_vehicle(value):
    items = parse_page(SOURCE, schema_body(value), URL).listings
    assert len(items) == 1
    assert (items[0].price, items[0].year, items[0].vin) == (16940, 2018, "WBA6S110X0VF82426")


def test_removing_own_car_leaves_numeric_metadata_empty():
    docs = [doc for doc in json_documents(fixture_body()) if isinstance(doc, list)]
    body = '<script type="application/json">' + json.dumps(docs) + "</script>"
    assert parse_page(SOURCE, body, URL).listings == []


def test_itemlist_contract_unchanged():
    car = next(doc for doc in json_documents(fixture_body()) if isinstance(doc, dict))
    body = (
        '<script type="application/ld+json">'
        + json.dumps({"@type": "ItemList", "itemListElement": [{"item": car}]})
        + "</script>"
    )
    assert len(parse_page(SOURCE, body, URL).listings) == 1
