import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from car_market.mercedes_nl_sources import (
    enrich_nl_listing,
    observed_nl_contract,
    own_nl_evidence,
    parse_mercedes_nl_page,
)
from car_market.model_profiles import MERCEDES_PROFILE, use_profile
from car_market.parse import Listing

ROOT = Path(__file__).parent / "fixtures/mercedes_nl_sources"
SERVICE = Path(__file__).parents[1]


def fixture(source):
    return json.loads((ROOT / (source + ".json")).read_text())


def own_html(state):
    return '<script type="application/json">' + json.dumps(state) + "</script>"


def test_fixtures_are_bound_by_hash_with_scenario_http_metadata():
    provenance = json.loads((ROOT / "provenance.json").read_text())
    for name, meta in provenance.items():
        assert (
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            == meta["fixture_sha256"]
        )
        original = meta["original"]
        assert original["status"] == 200
        assert original["requested_url"].startswith("https://")
        assert original["parser_url"] == original["final_url"]
        assert original["timestamp"].startswith("2026-10-08T")


def test_marktplaats_native_mercedes_ids_ranges_and_broad_controls():
    body = (ROOT / "marktplaats-filtered.json").read_text()
    contract = observed_nl_contract("marktplaats", body)
    request = contract["filters"]
    assert contract["total"] == 9
    assert request["categories"]["l2Categories"][0]["id"] == 130
    assert {965, 486, 534, 473} <= set(request["attributesById"])
    assert request["attributeRanges"] == [
        {"attributeKey": "constructionYear", "from": 2016}
    ]
    config = json.loads(
        (SERVICE / "tests/fixtures/legacy-config/sources-mercedes-nl.json").read_text()
    )
    queries = {q["key"]: q for q in config["queries"] if q["source"] == "marktplaats"}
    assert queries["c180_broad_control"]["verification"]["parsed"]["total"] == 365
    assert queries["180_coupe_control"]["verification"]["parsed"]["total"] == 44


def test_search_year_does_not_supply_generation_country_or_first_registration():
    row = fixture("marktplaats")["records"][0]
    evidence = own_nl_evidence("marktplaats", row)
    assert evidence["construction_year"] == "2019"
    assert evidence["body_type"] == "Coupé"
    assert evidence["generation"] is None
    assert evidence["production_date"] is None
    assert evidence["first_registration"] is None
    assert evidence["country"] == "NL"
    without_location = copy.deepcopy(row)
    without_location.pop("location", None)
    if isinstance(without_location.get("seller"), dict):
        without_location["seller"].pop("location", None)
    assert own_nl_evidence("marktplaats", without_location)["country"] is None
    with use_profile(MERCEDES_PROFILE, "NL"):
        page = parse_mercedes_nl_page(
            "marktplaats",
            (ROOT / "marktplaats-filtered.json").read_text(),
            fixture("marktplaats")["provenance"]["parser_url"],
        )
    assert all(item.scope != "target" for item in page.listings)
    assert all(
        item.raw["mercedes_nl_evidence"]["infer_missing"] is False
        for item in page.listings
    )


def test_native_anwb_body_preserves_own_vehicle_and_dealer_fields():
    data = fixture("anwb")
    body = data["provenance"]["request_body"]
    assert body["brandModel"]["filter"] == [
        {"slug": "mercedes-benz", "models": [{"slug": "c-klasse"}]}
    ]
    assert body["constructionYear"]["min"] == [{"name": 2016}]
    assert body["bodyType"]["filter"] == [{"slug": "coupe"}]
    row = data["records"][0]
    own = own_nl_evidence("anwb", row)
    assert own["body_type"] == row["carData"]["general"]["bodyType"]
    assert own["dealer_address"]["bound_to_own_listing"]
    assert own["generation"] is None
    assert (
        own["first_registration"]
        == row["carData"]["history"]["dateFirstAdmissionInternational"]
    )
    assert own["doors"] == 2


@pytest.mark.parametrize("nested_raw_data", [False, True])
@pytest.mark.parametrize(
    "generation",
    [{"raw": 12, "formatted": "205"}, {"raw": 11, "formatted": "204"}, {"raw": 12}],
)
def test_typed_native_generation_stays_typed_and_numeric_id_is_not_decoded(
    nested_raw_data, generation
):
    row = copy.deepcopy(fixture("autotrader")["records"][0])
    native = (
        row["vehicle"].setdefault("rawData", {}) if nested_raw_data else row["vehicle"]
    )
    native["classification"] = {"modelGeneration": copy.deepcopy(generation)}
    assert own_nl_evidence("autotrader", row)["generation"] == generation


def test_autotrack_native_response_filters_and_international_registration():
    data = fixture("autotrack")
    contract = data["contract"]
    assert contract["total"] == 27
    assert contract["filters"]["merkModel"]["filter"][0]["slug"] == "mercedes-benz"
    assert contract["filters"]["trefwoord"]["filter"] == [{"name": "180"}]
    own = own_nl_evidence("autotrack", data["records"][0])
    assert own["body_type"] == "COUPE"
    assert (
        own["first_registration"]
        == data["records"][0]["autogegevens"]["geschiedenis"][
            "datumEersteToelatingInternationaal"
        ]
    )
    assert own["generation"] is None


def test_autotrader_uses_own_make_and_bound_location_not_query_country():
    data = fixture("autotrader")
    state = {
        "props": {
            "pageProps": {
                "listings": data["records"],
                "pageQuery": data["contract"]["filters"],
                "numberOfResults": 29,
                "numberOfPages": 2,
            }
        }
    }
    with use_profile(MERCEDES_PROFILE, "NL"):
        page = parse_mercedes_nl_page(
            "autotrader", own_html(state), data["provenance"]["parser_url"]
        )
    item = page.listings[0]
    assert item.title.startswith("Mercedes-Benz C 180")
    assert item.raw["country_code"] == "NL"
    assert item.raw["dealerAddress"]["postalCode"] == "8243 PM"
    assert item.raw["dealerAddress"]["streetAddress"] == "Vijzelweg 8"
    assert item.raw["body_type"] == "Coupé"
    foreign = copy.deepcopy(data["records"][0])
    foreign["location"]["countryCode"] = "DE"
    assert own_nl_evidence("autotrader", foreign)["country"] == "DE"
    unbound = copy.deepcopy(data["records"][0])
    unbound.pop("location")
    unbound.pop("seller", None)
    assert own_nl_evidence("autotrader", unbound)["country"] is None


def test_autokopen_reads_initial_results_and_own_postal_address():
    data = fixture("autokopen")
    state = {
        "initialResults": data["records"],
        "meta": {"total": 84, "pages": 4, "page": 1},
        "searchParams": data["contract"]["filters"],
    }
    with use_profile(MERCEDES_PROFILE, "NL"):
        page = parse_mercedes_nl_page(
            "autokopen", own_html(state), data["provenance"]["parser_url"]
        )
    assert page.total == 84
    assert len(page.listings) == 2
    for item, row in zip(page.listings, data["records"], strict=True):
        assert item.price == row["price"]
        assert item.title == row["title"] + " " + row["variant"]
        assert item.raw["dealerAddress"]["postalCode"] == (
            row["dealer"]["postal_code"] or None
        )
        assert item.raw["mercedes_nl_evidence"]["generation"] is None


def test_independently_own_hedin_vin_body_and_production_date_are_retained():
    data = fixture("hedin")
    own = own_nl_evidence(data["source"], data["records"][0])
    assert own["vin"] == "WDD2053401F675905"
    assert own["body_type"] == "Coupé"
    assert own["doors"] == "2"
    assert own["production_date"] == "2017"
    assert own["model_year"] == "2017"
    assert own["first_registration"] is None
    assert own["country"] is None


def test_autofirst_bound_dealer_street_and_postcode_are_preserved():
    row = fixture("autofirst")["records"][0]
    own = own_nl_evidence("autofirst", row)
    assert own["dealer_address"]["street_address"] == "Industrieweg 37"
    assert own["dealer_address"]["postal_code"] == "4538AG"
    assert own["dealer_address"]["city"] == "Terneuzen"
    assert own["country"] is None


def test_detached_schema_address_cannot_promote_missing_country():
    row = copy.deepcopy(fixture("gaspedaal")["records"][0])
    row["recommendations"] = [
        {"seller": {"address": {"postalCode": "1011AA", "countryCode": "NL"}}}
    ]
    own = own_nl_evidence("gaspedaal", row)
    assert own["dealer_address"]["postal_code"] is None
    assert own["country"] is None
    assert own["generation"] is None


def test_enrichment_keeps_existing_conflicts_and_does_not_mutate_retained_record():
    row = copy.deepcopy(fixture("autotrader")["records"][0])
    row["generation"] = "C204"
    old = copy.deepcopy(row)
    item = Listing(
        "autotrader",
        row["id"],
        "https://www.autotrader.nl/auto/x",
        "Mercedes-Benz C180",
        raw=row,
    )
    enrich_nl_listing(item)
    assert row == old
    assert item.raw["generation"] == "C204"
    assert item.raw["mercedes_nl_evidence"]["generation"] == "C204"
    assert item.raw["mercedes_nl_evidence"]["model_year"] is None


@pytest.mark.parametrize(
    "source",
    [
        "gaspedaal",
        "autotrack",
        "anwb",
        "autotrader",
        "viabovag",
        "autokopen",
        "marktplaats",
        "autofirst",
    ],
)
def test_real_native_cards_keep_unknown_generation_unknown(source):
    for row in fixture(source)["records"]:
        own = own_nl_evidence(source, row)
        assert own["generation"] is None
        assert own["production_year"] is None


def test_config_is_isolated_and_every_enabled_query_has_own_successful_evidence():
    config = json.loads(
        (SERVICE / "tests/fixtures/legacy-config/sources-mercedes-nl.json").read_text()
    )
    assert config["scope"] == {
        "country": "NL",
        "model_profile": MERCEDES_PROFILE,
        "model": "Mercedes-Benz C180 Coupe",
        "generation": "C205",
        "first_registration_from": "2016-01-01",
        "fuel": "petrol",
        "transmissions": ["automatic"],
        "price_ceiling": None,
        "purchase_ceiling": None,
    }
    assert (
        config["research_paths"]["data"] == "~/src/mercedes-c180/nl/data/market.sqlite3"
    )
    assert config["research_paths"]["production_writer_authorized"] is True
    expected = {
        "gaspedaal",
        "marktplaats",
        "autotrack",
        "anwb",
        "autotrader",
        "viabovag",
        "nederlandmobiel",
        "autoweek",
        "autokopen",
        "autouncle",
        "marktnet",
        "trovit",
        "autowereld",
        "autofirst",
        "vakgarage",
        "carteam",
    }
    assert expected <= {q["source"] for q in config["queries"]}
    assert not any(
        q["source"] in {"bmw", "officialMercedes"} for q in config["queries"]
    )
    for query in config["queries"]:
        assert query["country"] == "NL"
        assert query["model_profile"] == MERCEDES_PROFILE
        assert query["infer_missing"] is False
        if query["source"] in {"autoscout24", "mercedes_benz_nl", "autohero"}:
            assert query.get("verification")
            continue
        evidence = query["verification"]
        assert evidence["market_complete"] is False
        assert evidence["c205_target_complete"] is False
        if query["enabled"]:
            assert evidence["observations"][0]["status"] == 200
            assert "error" not in evidence["parsed"]
        if query["source"] == "marktplaats":
            assert parse_qs(urlsplit(query["url"]).query)["l2CategoryIds[]"] == ["130"]
    disabled = {q["source"] for q in config["queries"] if not q["enabled"]}
    assert {"autowereld", "trovit", "autoweek"} <= disabled


def test_marktnet_mercedes_category_is_independently_published_and_c180_xml_is_partial():
    from defusedxml import ElementTree

    tree = ElementTree.fromstring(
        (ROOT / "marktnet-categories.xml").read_text().lstrip("\ufeff")
    )
    categories = [n for n in tree.iter() if n.get("name") == "Mercedes"]
    assert len(categories) == 1
    assert categories[0].get("id") == "57"
    assert categories[0].get("url") == "autos/mercedes"
    with use_profile(MERCEDES_PROFILE, "NL"):
        page = parse_mercedes_nl_page(
            "marktnet",
            (ROOT / "marktnet-c180cat.xml").read_text(),
            "https://www.marktnet.nl/zoeken.xml?cat_id=57&q=c180&descr=on",
        )
    assert page.total == 69
    assert len(page.listings) == 20
    assert page.pages == 4


def live_detail(fetch_id):
    entries = json.loads((ROOT / "live_detail_addresses.json").read_text())["entries"]
    return next(entry for entry in entries if entry["fetch_id"] == fetch_id)


def native_detail_html(entry):
    return (
        '<script type="application/ld+json">'
        + json.dumps(entry["record"])
        + "</script>"
        + entry.get("heading_dom", "")
        + entry.get("dealer_dom", "")
    )


def test_live_address_excerpts_keep_original_fetch_hash_and_time():
    meta = json.loads((ROOT / "live_address_provenance.json").read_text())
    assert (
        hashlib.sha256((ROOT / meta["fixture_file"]).read_bytes()).hexdigest()
        == meta["fixture_sha256"]
    )
    entries = json.loads((ROOT / meta["fixture_file"]).read_text())["entries"]
    assert len(entries) == 9
    for index, entry in enumerate(entries):
        proof = entry["provenance"]
        assert proof["status"] == 200
        assert proof["parser_url"] == entry["url"]
        assert proof["timestamp"].startswith("2026-10-08T")


@pytest.mark.parametrize("fetch_id", [660, 655])
def test_live_marktplaats_seller_location_and_own_body_are_retained(fetch_id):
    entry = live_detail(fetch_id)
    own = own_nl_evidence("marktplaats", entry["record"])
    location = entry["record"]["seller"]["location"]
    assert own["country"] == location["countryAbbreviation"] == "NL"
    assert own["dealer_address"]["city"] == location["cityName"]
    assert own["dealer_address"]["bound_to_own_listing"]
    assert own["body_type"] == "Stationwagon"
    assert own["model_family"] == "C-Klasse Estate"


@pytest.mark.parametrize("fetch_id", [752, 751])
def test_live_nlm_foreign_address_cannot_become_nl_from_query_or_domain(fetch_id):
    entry = live_detail(fetch_id)
    own = own_nl_evidence("nederlandmobiel", entry["record"])
    assert own["country"] == "BE"
    assert (
        own["dealer_address"]["postal_code"]
        == entry["record"]["attributes"]["Postcode"]
    )
    assert (
        own["dealer_address"]["street_address"]
        == entry["record"]["attributes"]["Adres"]
    )
    assert own["body_type"] == "Sportwagen/Coupé"
    assert own["doors"] == "2"
    assert own["generation"] is None
    without_country = copy.deepcopy(entry["record"])
    without_country["attributes"].pop("Land")
    assert own_nl_evidence("nederlandmobiel", without_country)["country"] is None


@pytest.mark.parametrize("fetch_id,postcode", [(417, "3433NP"), (407, "7575PD")])
def test_live_viabovag_uses_own_offer_seller_address_not_publisher_footer(
    fetch_id, postcode
):
    entry = live_detail(fetch_id)
    row = copy.deepcopy(entry["record"])
    row["publisher"] = {"address": {"postalCode": "3981 AJ", "addressCountry": "NL"}}
    own = own_nl_evidence("viabovag", row)
    assert own["country"] == "NL"
    assert own["dealer_address"]["postal_code"] == postcode
    seller = row["offers"].get("seller") or row["offers"]["offeredBy"]
    assert own["dealer_address"]["name"] == seller["name"]
    row["offers"].pop("seller", None)
    row["offers"].pop("offeredBy", None)
    detached = own_nl_evidence("viabovag", row)
    assert detached["country"] is None
    assert detached["dealer_address"]["postal_code"] is None


def test_live_autotrack_dealer_route_requires_own_uri_and_visible_postal_block():
    entry = live_detail(1026)
    with use_profile(MERCEDES_PROFILE, "NL"):
        page = parse_mercedes_nl_page(
            "autotrack", native_detail_html(entry), entry["url"]
        )
    assert len(page.listings) == 1
    item = page.listings[0]
    assert item.raw["market_country"] == "NL"
    assert item.raw["dealerAddress"]["postalCode"] == "9231 HS"
    assert item.raw["dealerAddress"]["streetAddress"] == "Molenweg 4"
    assert item.raw["own_detail_dealer"]["publication_url"] == entry["url"]
    # A filter URL and an external route link without a visible dealer card cannot prove country.
    missing = copy.deepcopy(entry)
    missing["dealer_dom"] = missing["dealer_dom"].replace("9231 HS Surhuisterveen", "")
    with use_profile(MERCEDES_PROFILE, "NL"):
        page = parse_mercedes_nl_page(
            "autotrack", native_detail_html(missing), entry["url"]
        )
    assert "own_detail_dealer" not in page.listings[0].raw
    assert page.listings[0].raw.get("market_country") is None


@pytest.mark.parametrize("fetch_id,identity", [(781, "143616"), (1045, "8399474")])
def test_live_autokopen_own_guarded_sku_preserves_search_publication_id(
    fetch_id, identity
):
    entry = live_detail(fetch_id)
    with use_profile(MERCEDES_PROFILE, "NL"):
        page = parse_mercedes_nl_page(
            "autokopen", native_detail_html(entry), entry["url"]
        )
    assert len(page.listings) == 1
    assert page.listings[0].external_id == identity
    assert str(page.listings[0].raw["sku"]) == identity
    assert page.listings[0].raw["market_country"] == "NL"
    wrong = copy.deepcopy(entry)
    wrong["record"]["sku"] = "99999999"
    with use_profile(MERCEDES_PROFILE, "NL"):
        page = parse_mercedes_nl_page(
            "autokopen", native_detail_html(wrong), entry["url"]
        )
    assert page.listings[0].external_id != "99999999"
