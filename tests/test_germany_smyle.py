"""Real OCS rows and own Smyle payload, without checkout or session state."""

import hashlib
import json
from pathlib import Path

import pytest
from car_market.germany import canonical_de_id, german_detail_identity_matches
from car_market.germany_autoscout import parse_autoscout_de

ROOT = Path(__file__).parent / "fixtures/germany_smyle"
MANIFEST = {m["fixture"]: m for m in json.loads((ROOT / "manifest.json").read_text())}


def test_search_preserves_all_twenty_native_ids_including_smyle():
    meta = MANIFEST["search.json"]
    body = (ROOT / "search.json").read_text()
    assert hashlib.sha256(body.encode()).hexdigest() == meta["fixture_hash"]
    page = parse_autoscout_de(body, meta["url"])
    assert len(page.listings) == 20
    smyle = next(x for x in page.listings if "/smyle/details/" in x.url)
    assert smyle.external_id == "f62cfa53-a8bc-49d2-b54b-95ed8430a824"
    assert smyle.scope == "excluded" and smyle.fuel == "diesel"
    assert canonical_de_id(smyle.source, smyle.url) == smyle.external_id


def test_smyle_detail_uses_actual_stock_location_and_gross_vehicle_price():
    meta = MANIFEST["detail.json"]
    body = (ROOT / "detail.json").read_text()
    assert hashlib.sha256(body.encode()).hexdigest() == meta["fixture_hash"]
    item = parse_autoscout_de(body, meta["url"]).listings[0]
    assert (item.scope, item.price, item.city, item.year, item.mileage) == (
        "target",
        27650,
        "Rosenheim",
        2025,
        2821,
    )
    assert (
        item.raw["country_evidence"]["path"] == "properData.carDetails.ocsInfo.location.countryCode"
    )
    assert item.raw["own_smyle_record"]["ocsInfo"]["financingExample"]["monthlyPayment"] == 245
    assert item.price_type == "advertised_price"
    assert len(item.images) > 15
    assert item.raw["detail_complete"] is False
    assert german_detail_identity_matches(
        item.source, meta["url"], meta["url"], body, item.external_id
    )


@pytest.mark.parametrize(
    "mutation",
    ["guid", "id", "country", "country_missing", "inactive", "unavailable", "foreign_canonical"],
)
def test_smyle_rejects_wrong_own_identity_country_and_availability(mutation):
    doc = json.loads((ROOT / "detail.json").read_text())
    state = doc["pageProps"]
    own = state["properData"]["carDetails"]
    if mutation == "guid":
        state["properData"]["guid"] = "other"
    elif mutation == "id":
        own["id"] = "other"
    elif mutation == "country":
        own["ocsInfo"]["location"]["countryCode"] = "NL"
    elif mutation == "country_missing":
        own["ocsInfo"]["location"].pop("countryCode")
    elif mutation == "inactive":
        own["ocsInfo"]["status"] = "SOLD"
    elif mutation == "unavailable":
        state["listingAvailable"] = False
    body = json.dumps(doc)
    if mutation == "foreign_canonical":
        body = (
            '<head><link rel="canonical" href="https://foreign.example/smyle/details/'
            + own["id"]
            + '/"></head><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({"props": doc})
            + "</script>"
        )
    assert not parse_autoscout_de(body, MANIFEST["detail.json"]["url"]).listings
