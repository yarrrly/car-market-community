import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from car_market.quality_specific_dealers import parse_specific_dealer_detail

FIXTURES = Path(__file__).parent / "fixtures" / "specific_dealers"
PROVENANCE = json.loads((FIXTURES / "provenance.json").read_text())


def fixture(gp_id):
    name = f"own-{gp_id}.html"
    return (FIXTURES / name).read_text(), PROVENANCE[name]["final_url"]


@pytest.mark.parametrize(
    ("gp_id", "kenteken", "price", "mileage", "gear", "city", "photo_count"),
    [
        ("140052008", "6ZSK83", 7350, 168205, "manual", "Dronten", 35),
        ("137613854", "HT363J", 13950, 47448, "automatic", "Dronten", 34),
        ("141580364", "ZL046Z", 15950, 108796, "automatic", "Oss", 36),
        ("142022179", "KTN77L", 31795, 60051, "automatic", "Harderwijk", 46),
    ],
)
def test_real_own_dealer_card(gp_id, kenteken, price, mileage, gear, city, photo_count):
    body, url = fixture(gp_id)
    item = parse_specific_dealer_detail(body, url).listings[0]
    assert (item.source, item.external_id, item.url) == ("dealer", url, url)
    assert (item.kenteken, item.price, item.mileage) == (kenteken, price, mileage)
    assert (item.transmission, item.fuel, item.city) == (gear, "petrol", city)
    assert item.scope == "target" and item.description
    assert len(item.images) == photo_count


@pytest.mark.parametrize("gp_id", ["140052008", "137613854", "141580364", "142022179"])
def test_other_url_cannot_be_matched_by_shared_year_and_mileage(gp_id):
    body, url = fixture(gp_id)
    if "flevomobiel" in url:
        url = url.replace("3720439", "9999999").replace("3654989", "9999999")
    elif "hedinautomotive" in url:
        url = url.replace("40790097", "99999999")
    else:
        url = url.replace("bmw-2-active-tourer", "unrelated-active-tourer")
    assert parse_specific_dealer_detail(body, url) is None


def test_flevo_visible_and_own_contact_registration_must_agree():
    body, url = fixture("140052008")
    soup = BeautifulSoup(body, "html.parser")
    soup.select_one('[data-section="kenteken"] .data-list-item-value').string = "8-XRR-49"
    assert parse_specific_dealer_detail(str(soup), url) is None


def test_flevo_missing_identity_never_falls_back_to_related_schema():
    body, url = fixture("140052008")
    soup = BeautifulSoup(body, "html.parser")
    for node in soup.select('input[name="ad_contact_adurl"]'):
        node.decompose()
    assert parse_specific_dealer_detail(str(soup), url) is None


@pytest.mark.parametrize("mutation", ["mpn", "offer_url", "bootstrap_id", "registration"])
def test_hedin_independent_own_identity_and_plate_guards(mutation):
    body, url = fixture("141580364")
    soup = BeautifulSoup(body, "html.parser")
    script = next(
        n for n in soup.select('script[type="application/ld+json"]') if '"mpn"' in n.string
    )
    car = json.loads(script.string)
    if mutation == "mpn":
        car["mpn"] = "99999999"
    elif mutation == "offer_url":
        car["offers"]["url"] = url.replace("40790097", "99999999")
    else:
        node = soup.select_one("#vehicle-intro .compare-toggle[data-vehicle]")
        own = json.loads(node["data-vehicle"])
        if mutation == "bootstrap_id":
            own["id"] = "unrelated-id"
        else:
            own["attributes"]["kenteken"]["value"] = "8XRR49"
        node["data-vehicle"] = json.dumps(own)
    script.string = json.dumps(car)
    assert parse_specific_dealer_detail(str(soup), url) is None


def test_hedin_uses_unclamped_own_modal_text_and_own_vin():
    body, url = fixture("141580364")
    item = parse_specific_dealer_detail(body, url).listings[0]
    assert item.vin == "WBA6S110207D68325"
    assert "Gaat jouw autohart" in item.description
    assert item.raw["detail_complete"] is True


def test_linkerbaan_dutch_mileage_uses_own_next_vehicle_not_decimal_schema():
    body, url = fixture("142022179")
    item = parse_specific_dealer_detail(body, url).listings[0]
    assert item.mileage == 60051
    assert item.raw["schema_mileage"]["value"] == 60.051
    assert item.raw["description_is_excerpt"] is True
    assert item.raw["detail_complete"] is False


@pytest.mark.parametrize(
    "mutation", ["schema_id", "schema_url", "schema_host", "registration", "query"]
)
def test_linkerbaan_rejects_cross_vehicle_data(mutation):
    body, url = fixture("142022179")
    soup = BeautifulSoup(body, "html.parser")
    own_script = soup.select_one('script[id^="vehicle-schema-"]')
    schema = json.loads(own_script.string)
    if mutation == "schema_id":
        own_script["id"] = "vehicle-schema-unrelated"
    elif mutation == "schema_url":
        schema["url"] = "https://www.linkerbaan.com/auto/unrelated"
    elif mutation == "schema_host":
        schema["url"] = schema["url"].replace("www.linkerbaan.com", "example.com")
    elif mutation == "registration":
        soup.select_one('[class*="LicensePlate_value"]').string = "8XRR49"
    else:
        next_script = soup.select_one("#__NEXT_DATA__")
        doc = json.loads(next_script.string)
        doc["query"]["slug"] = "unrelated"
        next_script.string = json.dumps(doc)
    own_script.string = json.dumps(schema)
    assert parse_specific_dealer_detail(str(soup), url) is None


@pytest.mark.parametrize("gp_id", ["140052008", "141580364", "142022179"])
def test_related_plate_photo_and_description_outside_own_blocks_are_ignored(gp_id):
    body, url = fixture(gp_id)
    before = parse_specific_dealer_detail(body, url).listings[0]
    soup = BeautifulSoup(body, "html.parser")
    trap = '<aside><img src="https://example.com/wrong.jpg" ci-src="https://example.com/wrong.jpg"><dt>Kenteken</dt><dd>8-XRR-49</dd><p>Wrong dealer history for another car of the same year and mileage.</p></aside>'
    soup.body.append(BeautifulSoup(trap, "html.parser"))
    after = parse_specific_dealer_detail(str(soup), url).listings[0]
    assert (after.kenteken, after.images, after.description) == (
        before.kenteken,
        before.images,
        before.description,
    )


def test_unknown_host_is_not_a_generic_dealer_fallback():
    body, _ = fixture("140052008")
    assert parse_specific_dealer_detail(body, "https://example.com/3720439/111230007") is None
