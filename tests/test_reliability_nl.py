"""Fresh public NL contracts and bounded ownership failure reproductions."""

import copy
import hashlib
import json
from pathlib import Path

from bs4 import BeautifulSoup
import pytest

from car_market.configured_collect import ConfiguredCollector
from car_market.mercedes_nl_sources import observed_nl_contract
from car_market.parse import json_documents, parse_page, walk


ROOT = Path(__file__).parent / "fixtures" / "reliability_nl"
PROVENANCE = json.loads((ROOT / "provenance.json").read_text())
SELECTORS = {
    "corolla": {"make": "Toyota", "model_terms": [["Corolla"]]},
    "golf": {"make": "Volkswagen", "model_terms": [["Golf"]]},
    "model-y": {"make": "Tesla", "model_terms": [["Model Y"]]},
    "bmw218i": {"make": "BMW", "model_terms": [["218i"], ["Active Tourer"]]},
}


def configured(source, body, url, model):
    collector = object.__new__(ConfiguredCollector)
    collector.country = "NL"
    collector.config = {
        "scope": {
            "model_profile": "configured_vehicle",
            "country": "NL",
            "selector": SELECTORS[model],
        }
    }
    return collector._parse(source, body, url)


@pytest.mark.parametrize("name", sorted(PROVENANCE))
def test_public_excerpts_have_exact_provenance_and_no_session_data(name):
    meta = PROVENANCE[name]
    assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == meta["sha256"]
    original = meta["original"]
    assert original["status"] == 200
    assert original["timestamp"].startswith("2026-10-11T")
    assert original["requested_url"].startswith("https://www.")
    assert len(original["sha256"]) == 64
    assert "anonymous_no_session" in original["transport"]
    assert "excerpt" in meta["fixture_kind"]
    assert all(key not in original for key in ("cookies", "authorization", "profile"))


@pytest.mark.parametrize("model,total,pages", [
    ("corolla", 948, 10), ("golf", 6684, 67),
    ("model-y", 438, 5), ("bmw218i", 306, 4),
])
def test_flight_text_boundary_retains_native_counters_and_full_own_cards(model, total, pages):
    name = f"gaspedaal-{model}-flight.html"
    body = (ROOT / name).read_text()
    url = PROVENANCE[name]["original"]["final_url"]
    contract = observed_nl_contract("gaspedaal", body)
    assert (contract["total"], contract["page"], contract["page_size"]) == (total, 1, 100)
    assert contract["filters"]["importAuto"] == {"filter": [{"id": False}]}
    page = configured("gaspedaal", body, url, model)
    assert (page.total, page.page, page.pages) == (total, 1, pages)
    assert [i.external_id for i in page.listings] == PROVENANCE[name]["retained_ids"]
    assert all("autogegevens" in i.raw for i in page.listings)
    assert all(i.scope == "needs_confirmation" for i in page.listings)
    assert all(i.raw["model_scope_evidence"]["reasons"] == ["country_unconfirmed"] for i in page.listings)
    assert all(i.raw.get("market_country") is None for i in page.listings)
    assert all(i.year and i.price and i.mileage for i in page.listings)


def test_length_delimited_utf8_text_cannot_supply_json_facts():
    text = 'Beschrijving: België\n9:{"totalOccasions":999999}\n'
    own = {"commonPortalURLProps": {"totalOccasions": 0, "pageSize": 100, "pageNumber": 1}}
    stream = "a:T" + format(len(text.encode()), "x") + "," + text + "b:" + json.dumps(own) + "\n"
    body = "<script>self.__next_f.push(" + json.dumps([1, stream]) + ")</script>"
    assert list(json_documents(body)) == [own]
    truncated = "a:T" + format(len(text.encode()) + 100, "x") + "," + text
    body = "<script>self.__next_f.push(" + json.dumps([1, truncated]) + ")</script>"
    assert list(json_documents(body)) == []


@pytest.mark.parametrize("missing", ["totalOccasions", "pageSize"])
def test_missing_gaspedaal_counter_contract_keeps_page_count_unknown(missing):
    body = (ROOT / "gaspedaal-corolla-flight.html").read_text()
    state = copy.deepcopy(next(n for d in json_documents(body) for n in walk(d) if "commonPortalURLProps" in n))
    state["commonPortalURLProps"].pop(missing)
    page = parse_page("gaspedaal", '<script type="application/json">' + json.dumps(state) + "</script>", "https://www.gaspedaal.nl/toyota/corolla")
    assert page.pages is None
    assert len(page.listings) == 2


def test_marktplaats_native_model_can_prove_a_truncated_title():
    name = "marktplaats-golf-native-model.json"
    page = configured("marktplaats", (ROOT / name).read_text(), PROVENANCE[name]["original"]["final_url"], "golf")
    item, = page.listings
    assert "Golf" not in item.title
    assert item.raw["model_family"] == "Golf"
    assert item.scope == "target"
    assert item.raw["market_country"] == "NL"


@pytest.mark.parametrize("model", SELECTORS)
def test_fresh_marktplaats_detail_keeps_own_make_model_and_country(model):
    name = f"marktplaats-{model}-detail.html"
    page = configured("marktplaats", (ROOT / name).read_text(), PROVENANCE[name]["original"]["final_url"], model)
    item, = page.listings
    assert item.scope == "target"
    assert item.raw["model_brand"] == SELECTORS[model]["make"]
    assert item.raw["market_country"] == "NL"
    assert item.price and item.year and item.mileage
    # The excerpt omits the full description; it cannot certify completeness.
    assert item.raw["detail_complete"] is False


@pytest.mark.parametrize("model", SELECTORS)
def test_numeric_autotrack_detail_keeps_own_native_car_and_dealer_proof(model):
    name = f"autotrack-{model}-detail.html"
    page = configured("autotrack", (ROOT / name).read_text(), PROVENANCE[name]["original"]["final_url"], model)
    item, = page.listings
    assert item.scope == "target"
    assert item.raw["market_country"] == "NL"
    assert item.raw["own_detail_vehicle"]["brand"] == SELECTORS[model]["make"]
    assert item.raw["own_detail_dealer"]["publication_url"] == item.url
    assert item.price and item.year and item.mileage and item.kenteken


def test_autotrack_message_confirmation_heading_cannot_hide_vehicle_variant():
    name = "autotrack-bmw218i-detail.html"
    body = (ROOT / name).read_text() + "<h1>Bericht verzonden!</h1>"
    page = configured("autotrack", body, PROVENANCE[name]["original"]["final_url"], "bmw218i")
    item, = page.listings
    assert "218i" in item.title
    assert item.scope == "target"


@pytest.mark.parametrize("trap", ["missing_schema", "ambiguous_schema", "foreign_host", "unbound_car", "wrong_component", "competing_plate", "competing_heading"])
def test_numeric_autotrack_detail_does_not_promote_related_inventory(trap):
    name = "autotrack-corolla-detail.html"
    url = PROVENANCE[name]["original"]["final_url"]
    soup = BeautifulSoup((ROOT / name).read_text(), "html.parser")
    schema_node = soup.select_one('script[type="application/ld+json"]')
    schema = json.loads(schema_node.string)
    state_node = soup.select_one('script[type="application/json"]')
    state = json.loads(state_node.string)
    if trap == "missing_schema":
        schema_node.decompose()
        unrelated = json.loads((ROOT.parent / "quality_api" / "autotrack-card.json").read_text())
        soup.append(BeautifulSoup('<script type="application/json">' + json.dumps(unrelated) + "</script>", "html.parser"))
    elif trap == "ambiguous_schema":
        soup.append(copy.copy(schema_node))
    elif trap == "foreign_host":
        url = url.replace("www.autotrack.nl", "example.test")
        schema["url"] = url
        schema_node.string = json.dumps(schema)
    elif trap == "unbound_car":
        other = json.loads((ROOT / "autotrack-bmw218i-detail.html").read_text().split('<script type="application/json">', 1)[1].split("</script>", 1)[0])["pageInfo"]["car"]
        soup.insert(0, BeautifulSoup('<script type="application/json">' + json.dumps(other) + "</script>", "html.parser"))
        for node in soup.select('[data-testid^="specKey-"]'):
            node.parent.decompose()
    elif trap == "wrong_component":
        state["pageInfo"]["componentId"] = "unrelated-car"
        state["pageInfo"]["car"]["licensePlate"] = "JXF21B"
        state_node.string = json.dumps(state)
    elif trap == "competing_plate":
        soup.insert(0, BeautifulSoup("<div><p>Kenteken</p><p>JXF-21-B</p></div>", "html.parser"))
    elif trap == "competing_heading":
        soup.insert(0, BeautifulSoup("<div><h1>BMW 218i Active Tourer</h1></div>", "html.parser"))
    page = configured("autotrack", str(soup), url, "corolla")
    if trap in {"missing_schema", "ambiguous_schema", "foreign_host"}:
        assert page.listings == []
    else:
        item, = page.listings
        assert item.kenteken == "ZB798R"
        assert item.year == 2019
        assert item.title.startswith("Toyota Corolla")
        assert item.scope == "target"


@pytest.mark.parametrize("trap", ["foreign_schema", "prefix_identity", "foreign_request", "extra_path"])
def test_marktplaats_detail_schema_requires_exact_own_publication(trap):
    body = (ROOT.parent / "quality_primary" / "m2448936451.html").read_text()
    url = "https://www.marktplaats.nl/v/auto-s/bmw/m2448936451-bmw"
    soup = BeautifulSoup(body, "html.parser")
    schema_node = soup.select_one('script[type="application/ld+json"]')
    schema = json.loads(schema_node.string)
    if trap == "foreign_schema":
        schema["url"] = schema["url"].replace("www.marktplaats.nl", "example.test")
    elif trap == "prefix_identity":
        schema["url"] = schema["url"].replace("m2448936451", "m24489364510")
    elif trap == "foreign_request":
        url = url.replace("www.marktplaats.nl", "example.test")
    elif trap == "extra_path":
        url += "/another-car"
    schema_node.string = json.dumps(schema)
    page = parse_page("marktplaats", str(soup), url)
    if trap in {"foreign_request", "extra_path"}:
        assert page.listings == []
    else:
        item, = page.listings
        assert item.external_id == "m2448936451"
        assert item.images == []
        assert item.price == 9499
        assert item.kenteken == "KX186B"
