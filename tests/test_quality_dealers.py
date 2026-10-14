import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from car_market.parse import parse_page, walk
from car_market.quality_dealer_adapters import parse_quality_page

FIXTURES = Path(__file__).parent / "fixtures" / "quality_dealers"
PROVENANCE = json.loads((FIXTURES / "provenance.json").read_text())


def detail(source, label="detail-1"):
    name = f"{source}-{label}.html"
    return parse_quality_page(source, (FIXTURES / name).read_text(), PROVENANCE[name]["final_url"])


def test_carteam_primary_vehicle_not_recommendations_or_wrong_schema():
    page = detail("carteam")
    assert len(page.listings) == 1
    item = page.listings[0]
    assert item.title == "BMW 2 Serie Active Tourer 218i"
    assert (item.price, item.year, item.mileage) == (8499, 2015, 181241)
    assert (item.kenteken, item.seller, item.city) == ("6ZTB85", "Carteam Garage Pronk", "De Goorn")
    assert item.transmission == "automatic"
    assert item.fuel == "petrol"
    assert item.vin is None
    assert len(item.images) > 1
    assert item.scope == "target"
    name = "carteam-detail-1.html"
    old = parse_page("carteam", (FIXTURES / name).read_text(), PROVENANCE[name]["final_url"])
    assert any(x.price == 13999 for x in old.listings)


def test_vakgarage_detail_data_attribute_and_visible_specs():
    page = detail("vakgarage")
    assert len(page.listings) == 1
    item = page.listings[0]
    assert (item.price, item.year, item.mileage) == (6450, 2014, 193646)
    assert (item.kenteken, item.seller) == ("5XPB51", "Vakgarage Simonis")
    assert item.external_id == "840239610"
    assert item.transmission == "manual"
    assert item.fuel == "petrol"
    assert len(item.images) == 32
    assert "waterpomp" in item.description
    assert item.city is None


def test_autouncle_detail_schema_array_type_and_image_object():
    item = detail("autouncle").listings[0]
    assert (item.price, item.year, item.mileage) == (3425, 2015, 200316)
    assert item.transmission == "automatic"
    assert item.fuel == "petrol"
    assert item.city == "ROOSENDAAL"
    assert item.images[0].endswith("2015-bmw-218-active-tourer-sport-line-136.webp")
    assert item.scope == "needs_confirmation"
    assert item.kenteken is None


def test_autokopen_price_on_request_does_not_take_fiscal_value():
    item = detail("autokopen").listings[0]
    assert item.price is None
    assert item.price_type == "unknown"
    assert (item.year, item.mileage, item.kenteken) == (2018, 96266, "JTN06R")
    assert item.transmission == "automatic"
    assert item.scope == "target"


def test_autokopen_half_payment_is_not_full_purchase_price():
    item = detail("autokopen", "detail-2").listings[0]
    assert "BETAAL NU 50%" in item.title
    assert item.price is None
    assert item.price_type == "unknown"
    assert item.raw["price_warning"] == "conditional_or_instalment_price"
    assert (item.year, item.mileage) == (2022, 163946)
    assert item.seller == "Auto2Go"
    assert item.vin is None


def test_unrelated_schema_not_accepted_as_detail():
    name = "autouncle-detail-1.html"
    page = parse_quality_page(
        "autouncle", (FIXTURES / name).read_text(), "https://www.autouncle.nl/nl-nl/d/unrelated"
    )
    assert not page.listings


def test_autofirst_detail_includes_template_specs_and_gallery():
    item = detail("autofirst").listings[0]
    assert (item.price, item.year, item.mileage) == (13950, 2015, 103561)
    assert item.kenteken == "GT531Z"
    assert item.transmission == "automatic"
    assert item.fuel == "petrol"
    assert (item.seller, item.city) == ("AutoFirst Quadflieg", "Landgraaf")
    assert len(item.images) > 10
    assert "Afleverpakketten" in item.description
    assert item.scope == "target"


@pytest.mark.parametrize(
    ("source", "label", "complete"),
    [
        ("autofirst", "detail-1", True),
        ("carteam", "detail-1", True),
        ("vakgarage", "detail-1", True),
        ("autokopen", "detail-1", True),
        ("autokopen", "detail-2", False),
        ("autouncle", "detail-1", False),
    ],
)
def test_detail_complete_requires_full_isolated_seller_text(source, label, complete):
    assert detail(source, label).listings[0].raw["detail_complete"] is complete


def test_vakgarage_uses_full_modal_description_instead_of_clamped_preview():
    item = detail("vakgarage").listings[0]
    assert "Sportlaan 10" in item.description
    assert item.raw["detail_complete"] is True
    assert item.raw["description_truncated"] is False


@pytest.mark.parametrize("mutation", ["missing", "ellipsis", "clamp"])
def test_incomplete_seller_description_never_marks_detail_complete(mutation):
    name = "carteam-detail-1.html"
    soup = BeautifulSoup((FIXTURES / name).read_text(), "html.parser")
    node = soup.select_one("#omschrijving")
    if mutation == "missing":
        node.decompose()
    elif mutation == "ellipsis":
        node.append("…")
    else:
        node["class"] = ["line-clamp-5"]
    item = parse_quality_page("carteam", str(soup), PROVENANCE[name]["final_url"]).listings[0]
    assert item.raw["detail_complete"] is False


@pytest.mark.parametrize("designation", ["218", "218i", "218d"])
def test_autouncle_unknown_fuel_keeps_candidate_unconfirmed(designation):
    name = "autouncle-detail-1.html"
    soup = BeautifulSoup((FIXTURES / name).read_text(), "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        doc = json.loads(script.string)
        for node in walk(doc):
            node.pop("fuelType", None)
            for key in ("name", "description"):
                if isinstance(node.get(key), str):
                    node[key] = node[key].replace("BMW 218 Active", f"BMW {designation} Active")
        script.string = json.dumps(doc)
    item = parse_quality_page("autouncle", str(soup), PROVENANCE[name]["final_url"]).listings[0]
    assert item.fuel is None
    assert item.scope == ("excluded" if designation == "218d" else "needs_confirmation")
