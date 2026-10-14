"""Own retained DE/NL fields plus explicitly synthetic contract faults; no network."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from car_market.germany_autoscout import parse_autoscout_de
from car_market.germany_mobile import mobile_ad_id, mobile_page_url, parse_mobile
from car_market.mercedes_autoscout import (
    mercedes_autoscout_page_url,
    parse_mercedes_autoscout,
)
from car_market.search_profile import search_context

ROOT = Path(__file__).parent / "fixtures"
NATIVE = ROOT / "reliability_de"
MANIFEST = json.loads((NATIVE / "manifest.json").read_text())


def native(country="DE"):
    case = next(c for c in MANIFEST if c["file"].startswith(country.lower()))
    doc = json.loads((NATIVE / case["file"]).read_text())
    state = doc["props"]["pageProps"]
    own = state.get("listingDetails") or state["listings"][0]
    return doc, own, case["source_url"]


def scope(country="DE", make="BMW", terms=None, **selector):
    return {
        "model_profile": "configured_vehicle",
        "country": country,
        "selector": {
            "make": make,
            "model_terms": terms or [["218i"], ["Active Tourer"]],
            **selector,
        },
    }


def parse(doc, url, country="DE", **selector):
    with search_context(scope(country, **selector)):
        return parse_mercedes_autoscout(json.dumps(doc), url, country)


def test_sanitized_projections_remain_traceable_to_retained_public_responses():
    for case in MANIFEST:
        assert hashlib.sha256((NATIVE / case["file"]).read_bytes()).hexdigest() == case[
            "fixture_sha256"
        ]
        if "retained_input" in case:
            assert hashlib.sha256((ROOT / case["retained_input"]).read_bytes()).hexdigest() == case[
                "retained_input_sha256"
            ]
        else:
            assert case["source_sha256"] == case["retained_input_sha256"]
        assert len(case["source_sha256"]) == 64
        assert case["observed_at"].startswith("2026-10-")
        assert case["source_url"].startswith("https://www.autoscout24.")


@pytest.mark.parametrize("case", [c for c in MANIFEST if c["file"].startswith("baseline-")])
def test_fresh_baseline_projections_keep_every_own_slot_and_identity(case, monkeypatch):
    from datetime import datetime

    class ObservationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(case["observed_at"])

    monkeypatch.setattr("car_market.search_profile.datetime", ObservationClock)
    doc = json.loads((NATIVE / case["file"]).read_text())
    native_rows = doc["props"]["pageProps"]["listings"]
    with search_context(case["retained_scope"]):
        page = parse_mercedes_autoscout(json.dumps(doc), case["source_url"], case["retained_scope"]["country"])
    assert [row.external_id for row in page.listings] == [row["id"] for row in native_rows]
    assert page.total == doc["props"]["pageProps"]["numberOfResults"]
    assert all(row.raw["price_currency"] == "EUR" for row in page.listings)
    assert all(row.raw["vehicle"] == native["vehicle"] for row, native in zip(page.listings, native_rows))
    if case["file"].endswith("future-year.json"):
        item = page.listings[0]
        assert item.scope == "needs_confirmation"
        assert item.raw["model_scope_evidence"]["reasons"] == ["future_year"]
    else:
        assert all(row.scope == "target" for row in page.listings)


def test_repeated_own_slots_remain_visible_to_coverage_denominators():
    case = next(c for c in MANIFEST if c["file"] == "baseline-nl-corolla-page1.json")
    doc = json.loads((NATIVE / case["file"]).read_text())
    rows = doc["props"]["pageProps"]["listings"]
    rows.append(deepcopy(rows[0]))
    with search_context(case["retained_scope"]):
        page = parse_mercedes_autoscout(json.dumps(doc), case["source_url"], "NL")
    assert len(page.listings) == len(rows) == 21
    assert len({row.external_id for row in page.listings}) == len({row["id"] for row in rows})


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_generic_parser_preserves_own_facts_in_each_market(country):
    doc, own, url = native(country)
    before = deepcopy(own)
    selector = {} if country == "DE" else {"make": "Mercedes-Benz", "terms": [["C 180"]]}
    item = parse(doc, url, country, **selector).listings[0]
    assert item.scope == "target"
    assert item.raw["vehicle"] == before["vehicle"]
    assert item.raw["location"] == before["location"]
    assert item.raw["price"] == before["price"]
    assert item.raw["market_country"] == country
    assert item.raw["price_currency"] == "EUR"
    assert item.price_type == "advertised_price"
    assert item.raw["publication_reference"]["external_id"] == own["id"]
    assert own == before


@pytest.mark.parametrize("make_field", [{"name": "BMW"}, {"formatted": "BMW", "raw": 13}])
def test_native_brand_labels_remain_in_generic_title(make_field):
    doc, own, url = native()
    own["vehicle"]["make"] = make_field
    item = parse(doc, url).listings[0]
    assert item.title.startswith("BMW 218i")
    assert item.scope == "target"


@pytest.mark.parametrize("value", [-1, "-100 km", "−100 km", True, 1.5, float("inf")])
def test_invalid_own_mileage_cannot_become_positive_integer(value):
    doc, own, url = native()
    own["vehicle"]["mileageInKmRaw"] = value
    item = parse(doc, url).listings[0]
    assert item.mileage is None
    assert item.raw["vehicle"]["mileageInKmRaw"] == value


@pytest.mark.parametrize("field", ["vehicleDetails", "images"])
@pytest.mark.parametrize("value", [None, 42])
def test_optional_native_collections_may_be_missing_or_malformed(field, value):
    doc, own, url = native()
    own[field] = value
    assert parse(doc, url).listings[0].scope == "target"


@pytest.mark.parametrize(
    "price,expected",
    [
        ({}, (None, "unknown")),
        ({"priceRaw": 100}, (None, "unknown_currency")),
        ({"priceRaw": 100, "priceFormatted": "CHF 100"}, (None, "non_eur")),
        ({"priceRaw": 100, "currency": "USD"}, (None, "non_eur")),
        ({"priceRaw": 100, "currency": "CHF", "priceFormatted": "€ 100"}, (None, "ambiguous_price")),
        ({"priceRaw": -100, "priceFormatted": "€ 100"}, (None, "unknown")),
        ({"priceFormatted": "€ -100"}, (None, "unknown")),
        ({"priceRaw": 100, "priceFormatted": "€ 100 per maand"}, (100, "monthly_payment")),
        ({"priceRaw": 100, "priceFormatted": "€ 100 p/m"}, (100, "monthly_payment")),
        ({"priceRaw": 100, "priceFormatted": "€ 100 excl. BTW"}, (100, "net_price")),
        ({"priceRaw": 100, "priceFormatted": "€ 100 aanbetaling"}, (100, "conditional_price")),
        ({"priceFormatted": "Prijs op aanvraag"}, (None, "price_on_request")),
    ],
)
def test_own_unknown_foreign_and_finance_prices_never_become_cash(price, expected):
    doc, own, url = native()
    own["prices"] = {}
    own["price"] = price
    item = parse(doc, url).listings[0]
    assert (item.price, item.price_type) == expected
    assert item.raw["price_warning"] == expected[1]
    assert item.raw["price"] == price


@pytest.mark.parametrize("own_country", [None, "NL", "CH"])
def test_country_never_comes_from_edition_or_search_url(own_country):
    doc, own, url = native()
    own["location"]["countryCode"] = own_country
    item = parse(doc, url).listings[0]
    assert item.scope == ("needs_confirmation" if own_country is None else "excluded")
    assert item.raw.get("market_country") == own_country


@pytest.mark.parametrize(
    "make,terms,expected",
    [
        ("Toyota", [["Corolla"]], "target"),
        ("Volkswagen", [["Golf"]], "excluded"),
        ("Tesla", [["Model Y"]], "excluded"),
        ("BMW", [["218i"], ["Active Tourer"]], "excluded"),
    ],
)
def test_benchmark_selectors_do_not_relabel_an_actual_corolla(make, terms, expected):
    doc = smyle()
    item = parse(doc, doc["_fixture_provenance"]["source_url"], make=make, terms=terms).listings[0]
    assert item.scope == expected
    assert item.fuel == "hybrid" and item.year == 2023
    assert item.raw["own_smyle_record"]["make"] == "Toyota"


def test_recommendations_and_description_cannot_prove_missing_model():
    doc, own, url = native()
    other = deepcopy(own)
    own["vehicle"].update(model=None, motorTypeName=None, variant=None, modelVersionInput=None)
    own["description"] = "BMW 218i Active Tourer Toyota Corolla Volkswagen Golf Tesla Model Y"
    doc["props"]["pageProps"]["recommendations"] = [other]
    item = parse(doc, url).listings[0]
    assert item.scope == "needs_confirmation"
    assert "model_unconfirmed" in item.raw["model_scope_evidence"]["reasons"]


@pytest.mark.parametrize("cy", [None, ["NL"], {"code": "NL"}, "DE"])
def test_search_country_must_match_typed_native_and_request_contract(cy):
    doc, _, url = native("NL")
    doc["props"]["pageProps"]["pageQuery"]["cy"] = cy
    page = parse(doc, url, "NL", make="Mercedes-Benz", terms=[["C 180"]])
    assert not page.listings and page.total is None


@pytest.mark.parametrize("page", [None, True, "bad", -1, 201, 2])
def test_search_page_must_match_request_and_source_cap(page):
    doc, _, url = native("NL")
    doc["props"]["pageProps"]["pageQuery"]["page"] = page
    result = parse(doc, url, "NL", make="Mercedes-Benz", terms=[["C 180"]])
    assert not result.listings and result.total is None


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_search_without_explicit_request_country_stays_unverified(country):
    case = next(c for c in MANIFEST if c["file"] == f"baseline-{country.lower()}-corolla-page1.json")
    doc = json.loads((NATIVE / case["file"]).read_text())
    with search_context(case["retained_scope"]):
        page = parse_mercedes_autoscout(json.dumps(doc), case["source_url"].split("?")[0], country)
    assert not page.listings and page.total is None


def test_pagination_cannot_reuse_proven_country_with_a_foreign_query():
    doc, _, url = native("NL")
    first = parse(doc, url, "NL", make="Mercedes-Benz", terms=[["C 180"]])
    with pytest.raises(ValueError):
        mercedes_autoscout_page_url({"url": url.replace("cy=NL", "cy=D")}, 2, first)


def smyle():
    return json.loads((ROOT / "configured_autoscout/smyle-corolla.json").read_text())


@pytest.mark.parametrize("label", [None, "CHF 28.790"])
def test_smyle_currency_failure_cannot_retain_a_cash_price(label):
    doc = smyle()
    doc["props"]["pageProps"]["properData"]["carDetails"]["price"]["value"]["formatted"] = label
    item = parse(doc, doc["_fixture_provenance"]["source_url"], make="Toyota", terms=[["Corolla"]]).listings[0]
    assert item.price is None
    assert item.price_type == ("unknown_currency" if label is None else "non_eur")
    assert item.raw["price_warning"] == item.price_type


def test_legacy_de_adapter_uses_the_same_currency_and_numeric_guards():
    doc, own, url = native()
    own["prices"] = {}
    own["price"] = {"priceRaw": 100, "priceFormatted": "CHF 100"}
    own["vehicle"]["mileageInKmRaw"] = "-100 km"
    item = parse_autoscout_de(json.dumps(doc), url).listings[0]
    assert item.price is None and item.mileage is None
    assert item.raw["price_currency"] == "CHF"


def mobile_own():
    from car_market.parse import json_documents

    doc = next(json_documents((ROOT / "germany_mobile/detail.html").read_text()))
    return doc[0]["listing"]


def mobile_parse(item):
    body = '<script type="application/json">' + json.dumps({"eventScope": "page-vip", "listing": item}) + "</script>"
    return parse_mobile(body, "https://home.mobile.de/home/vip?id=450021582&customerId=12421572")


@pytest.mark.parametrize(
    "price,expected",
    [
        ({"grs": {"amount": 100}}, (None, "unknown_currency")),
        ({"grs": {"amount": -100, "currency": "EUR"}}, (None, "unknown")),
        ({"grs": {"amount": True, "currency": "EUR"}}, (None, "unknown")),
        ({"grs": {"amount": float("inf"), "currency": "EUR"}}, (None, "unknown")),
        ({"grs": 100}, (None, "unknown")),
    ],
)
def test_mobile_retained_native_price_contract_rejects_unknown_and_invalid_cash(price, expected):
    own = mobile_own()
    own["price"] = price
    item = mobile_parse(own).listings[0]
    assert (item.price, item.price_type) == expected


@pytest.mark.parametrize("value", [None, 42])
@pytest.mark.parametrize("field", ["attr", "images", "contact", "sellerAddress"])
def test_mobile_optional_field_drift_does_not_crash(value, field):
    own = mobile_own()
    own[field] = value
    if field == "contact":
        own["sellerAddress"] = {"country": "DE"}
    page = mobile_parse(own)
    assert page.listings and page.listings[0].external_id == "450021582"


@pytest.mark.parametrize("url", ["https://user@home.mobile.de/home/vip?id=450021582", "https://home.mobile.de:8443/home/vip?id=450021582"])
def test_mobile_detail_identity_rejects_credentials_and_unobserved_ports(url):
    assert mobile_ad_id(url) is None
    with pytest.raises(ValueError):
        mobile_page_url(url.replace("/home/vip", "/home/index.html"), 2)


@pytest.mark.parametrize("mutation", ["remove", "wrong_amount", "unrelated_euro"])
def test_mobile_search_currency_requires_same_own_card_and_quote(mutation):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup((ROOT / "germany_mobile/search-en.html").read_text(), "html.parser")
    card = soup.select_one('[data-testid="base-result-listing-1"]')
    price = card.select_one('[data-testid="main-price-label"]')
    if mutation == "wrong_amount":
        price.string = "€ 1"
    else:
        price.decompose()
        if mutation == "unrelated_euro":
            other = soup.new_tag("aside", attrs={"data-testid": "main-price-label"})
            other.string = "€ 12,991"
            soup.append(other)
    page = parse_mobile(str(soup), "https://www.mobile.de/en/car/bmw/218-active-tourer")
    item = next(row for row in page.listings if row.external_id == "43465045975104")
    assert item.price is None and item.price_type == "unknown_currency"
    assert item.raw["price_currency"] == "UNKNOWN"


def test_fresh_native_quotes_replace_stale_price_warnings_in_isolated_database(tmp_path):
    from car_market.configuration import bind
    from car_market.db import Database

    doc, own, url = native()
    bound = {
        "schema_version": 1,
        "scope": scope(),
        "queries": [{
            "source": "autoscout24_de", "key": "offline",
            "url": "https://www.autoscout24.de/lst/bmw?cy=D",
            "pagination": "mercedes_autoscout_next", "coverage": "enumerate",
            "infer_missing": False,
        }],
        "interval_hours": 6, "fast_interval_minutes": 1, "workers": 1,
        "detail_limit": 1, "max_pages": 1,
    }
    db = Database(tmp_path / "offline.sqlite3")
    try:
        bind(db, bound)
        snapshots = []
        for minute, quote in enumerate([
            {"priceRaw": 199, "priceFormatted": "Leasing € 199 / Monat"},
            {"priceRaw": 16999, "priceFormatted": "€ 16.999"},
            {},
        ]):
            own["price"], own["prices"] = quote, {}
            run = db.start_run("offline")
            db.observe(run, parse(doc, url).listings[0], f"2026-10-11T05:{50 + minute}:00+00:00")
            db.finish_run(run, "partial", {})
            row = db.conn.execute("SELECT * FROM latest_observations").fetchone()
            snapshots.append((dict(row), json.loads(row["details_json"])))
        assert snapshots[0][1]["raw"]["price_warning"] == "lease_price"
        assert snapshots[1][0]["price_eur"] == 16999
        assert snapshots[1][0]["price_type"] == "advertised_price"
        assert snapshots[1][1]["raw"]["price_warning"] is None
        assert snapshots[2][0]["price_eur"] is None
        assert snapshots[2][1]["raw"]["price_currency"] == "UNKNOWN"
        assert db.conn.execute("SELECT count(*) FROM purchase_candidates").fetchone()[0] == 0
        assert db.conn.execute("SELECT availability FROM listings").fetchone()[0] == "observed"
    finally:
        db.conn.close()
