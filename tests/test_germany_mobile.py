import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from car_market.germany_mobile import (
    DEALER_SEARCH,
    DEALER_SOURCE,
    _flight,
    collect_mobile_dealer,
    mobile_ad_id,
    mobile_dealer_ad_id,
    mobile_detail_url,
    mobile_page_url,
    mobile_search_url,
    parse_mobile,
    parse_mobile_dealer,
)
from car_market.parse import walk

FIXTURES = Path(__file__).parent / "fixtures" / "germany_mobile"
SEARCH = "https://www.mobile.de/en/car/bmw/218-active-tourer"
DEALER = "https://home.mobile.de/home/index.html?customerId=12421572"
DETAIL = "https://home.mobile.de/home/vip?id=450021582&customerId=12421572"


def fixture(name):
    return (FIXTURES / name).read_text()


def own_payload():
    body = fixture("detail.html")
    return json.loads(body.split('<script type="application/json">')[1].split("</script>")[0])[0][
        "listing"
    ]


def detail_body(item, **other):
    return (
        '<script type="application/json">'
        + json.dumps([{"eventScope": "page-vip", "listing": item}, other])
        + "</script>"
    )


def test_real_english_search_has_own_identity_and_no_false_completeness():
    page = parse_mobile(fixture("search-en.html"), SEARCH)
    assert page.total == 2010
    assert page.page == 1 and page.pages == 50
    assert page.search_filters["effective_filters"]["dam"] == "false"
    assert page.search_filters["coverage_status"] == "partial"
    assert not page.search_filters["national_geography_verified"]
    listings = {item.external_id: item for item in page.listings}
    manual = listings["43465045975104"]
    assert (manual.price, manual.mileage, manual.year, manual.transmission) == (
        12991,
        115000,
        2018,
        "manual",
    )
    assert manual.raw["first_registration"] == "2018-01"
    assert manual.scope == "needs_confirmation"
    target = listings["47133717515200"]
    assert target.scope == "target" and target.transmission == "automatic"
    assert target.raw["market_country"] == "DE"
    assert all(x.source == "mobile_de" and x.kenteken is None for x in page.listings)
    assert all(mobile_ad_id(x.url) == x.external_id for x in page.listings)
    assert listings["455501656"].scope == "excluded"
    assert target.description is None and target.images


def test_localized_polish_is_same_publication_with_germany_inventory():
    en = {x.external_id: x for x in parse_mobile(fixture("search-en.html"), SEARCH).listings}
    pl = {
        x.external_id: x
        for x in parse_mobile(fixture("search-pl.html"), SEARCH.replace("/en/", "/pl/")).listings
    }
    common = en.keys() & pl.keys()
    assert len(common) >= 10
    for key in common:
        assert en[key].price == pl[key].price
        assert pl[key].raw["market_country"] == "DE"
        assert mobile_ad_id(pl[key].url) == key


def test_real_dealer_controls_are_excluded_and_target_survives():
    page = parse_mobile(fixture("dealer.html"), DEALER)
    assert (page.total, page.pages) == (86, 5)
    assert page.search_filters["inventory_kind"] == "dealer"
    assert not page.search_filters["national_geography_verified"]
    items = {x.external_id: x for x in page.listings}
    assert items["459938158"].scope == "excluded"  # 218i Coupé
    assert items["450021582"].scope == "target"
    assert items["450021582"].price == 35690
    assert items["450021582"].raw["price_components"]["nt"]["amount"] == 29991.6


def test_real_filtered_dealer_pagination_does_not_turn_subset_into_national_market():
    page = parse_mobile(fixture("dealer-filtered.html"), DEALER + "&ms=3500%3B107%3B%3B")
    assert page.total == 2 and len(page.listings) == 2
    assert all(item.scope == "target" for item in page.listings)
    empty = parse_mobile(fixture("dealer-empty-p2.html"), mobile_page_url(DEALER, 2))
    assert empty.page == 2 and empty.total == 2 and not empty.listings
    assert empty.search_filters["coverage_status"] == "partial"


def test_real_own_dealer_detail_gross_description_images_seller_and_options():
    page = parse_mobile(fixture("detail.html"), DETAIL)
    assert len(page.listings) == 1
    item = page.listings[0]
    assert item.external_id == "450021582" and item.scope == "target"
    assert item.price == 35690 and item.price_type == "advertised_price"
    assert item.mileage == 0 and item.year is None and item.transmission == "automatic"
    assert item.fuel == "petrol" and item.kenteken is None and item.vin is None
    assert item.seller == "Faba Autowelt GmbH" and item.city == "Mönchengladbach"
    assert "Steptronic" in item.description
    assert "Android Auto" in item.raw["options"]
    assert len(item.images) == 20
    assert item.raw["dealer_stock_id"] == "096363"
    assert item.raw["attributes"]["modelRange"] == "U06"
    assert item.raw["publication_reference"] == {
        "source": "mobile_de",
        "external_id": "450021582",
        "url": item.url,
        "kind": "explicit_own_publication_link",
    }
    assert "home.mobile.de/home/redirect.html?customerId=12421572" in item.links[0]
    assert item.raw["detail_complete"] is True


def test_original_flight_utf8_text_record_is_not_lost_after_description():
    docs = _flight(fixture("detail-flight.html"))
    items = [o["listing"] for d in docs for o in walk(d) if o.get("eventScope") == "page-vip"]
    assert len(items) == 1
    assert items[0]["id"] == 450021582
    assert "Irrtümer und Zwischenverkauf" in items[0]["htmlDescription"]


@pytest.mark.parametrize("country", [None, "NL", "AT", "CH"])
def test_country_must_come_from_own_inventory(country):
    item = own_payload()
    item["contact"]["country"] = country
    assert not parse_mobile(detail_body(item), DETAIL).listings


def test_country_conflict_does_not_use_language_or_german_edition_as_proof():
    item = own_payload()
    item["attr"] = {"cn": "NL", "eu": "Deutsche Ausführung"}
    assert not parse_mobile(detail_body(item), DETAIL).listings


@pytest.mark.parametrize(
    "change", ["request", "id", "publication_url", "recommendation_only", "multiple_own"]
)
def test_detail_identity_never_falls_back_to_recommendations(change):
    item = own_payload()
    url = DETAIL
    body = None
    if change == "request":
        url = DETAIL.replace("450021582", "450021583")
    elif change == "id":
        item["id"] = 450021583
    elif change == "publication_url":
        item["url"] = item["url"].replace("450021582", "450021583")
    elif change == "recommendation_only":
        body = (
            '<script type="application/json">'
            + json.dumps({"recommendedListing": item})
            + "</script>"
        )
    else:
        another = own_payload()
        another["id"] = 450021583
        body = detail_body(item, eventScope="page-vip", listing=another)
    assert not parse_mobile(body or detail_body(item), url).listings


def test_recommendation_cannot_supply_description_vin_price_or_images():
    item = own_payload()
    other = own_payload()
    other.update(id=450021583, vin="WBA2A11070V123456", htmlDescription="OTHER CAR")
    actual = parse_mobile(detail_body(item, recommendedListing=other), DETAIL).listings[0]
    assert actual.vin is None and "OTHER CAR" not in actual.description
    assert actual.price == 35690 and actual.external_id == "450021582"


@pytest.mark.parametrize(
    "price,kind,amount",
    [
        ({"nt": {"amount": 29991.6, "currency": "EUR"}}, "net_price", 29991.6),
        ({"grs": {"amount": 299}, "type": "LEASING"}, "monthly_payment", None),
        ({"type": "ON_REQUEST"}, "on_request", None),
        ({"grs": {"amount": 35690, "currency": "CHF"}}, "non_eur", None),
    ],
)
def test_financial_price_types_never_become_cash(price, kind, amount):
    item = own_payload()
    item["price"] = price
    actual = parse_mobile(detail_body(item), DETAIL).listings[0]
    assert (actual.price_type, actual.price) == (kind, amount)


@pytest.mark.parametrize(
    "vin,expected",
    [("WBA2A11070V123456", "WBA2A11070V123456"), ("V123456", None), ("WBA2A11070I123456", None)],
)
def test_only_full_own_vin_is_accepted(vin, expected):
    item = own_payload()
    item["vin"] = vin
    assert parse_mobile(detail_body(item), DETAIL).listings[0].vin == expected


def test_helpers_preserve_geography_and_partitions_without_purchase_constraints():
    url = mobile_search_url(registration="2014:2017", price="5000:9999")
    query = parse_qs(urlsplit(mobile_page_url(url, 3)).query)
    assert query["cn"] == ["DE"] and query["ms"] == ["3500;107;;"]
    assert query["pageNumber"] == ["3"] and query["fr"] == ["2014:2017"]
    assert query["p"] == ["5000:9999"]
    assert not {"dam", "tr", "rd", "ll", "gn", "ft"} & query.keys()
    assert mobile_detail_url("450021582", customer_id=12421572) == DETAIL
    with pytest.raises(ValueError):
        mobile_page_url(SEARCH, 2)
    with pytest.raises(ValueError):
        mobile_page_url(url, 0)
    assert mobile_ad_id("https://evil.example/auto-inserat/bmw/450021582.html") is None
    assert mobile_ad_id(DETAIL + "&id=450021583") is None


def test_access_denied_empty_or_foreign_pages_do_not_claim_complete():
    for body, url in [
        ("Access denied", SEARCH),
        ("", SEARCH),
        (fixture("detail.html"), "https://evil.example/"),
    ]:
        page = parse_mobile(body, url)
        assert not page.listings and page.total is None and page.pages is None
        assert page.search_filters["coverage_status"] == "unverified"


RECOVERY = json.loads((FIXTURES / "recovery-provenance.json").read_text())
NATIVE_URL = RECOVERY[1]["metadata"]["requested_url"]


def native_soup():
    from bs4 import BeautifulSoup

    return BeautifulSoup(fixture("straussberger-detail-51908193.html"), "html.parser")


def native_state():
    body = fixture("straussberger-search.html")
    return json.JSONDecoder().raw_decode(body.split(" = ")[1])[0]


def native_search(state):
    return "<script>window.FWP_JSON = " + json.dumps(state) + ";</script>"


def test_native_dealer_frontier_has_fourteen_own_ids_but_no_country_inference():
    page = parse_mobile_dealer(fixture("straussberger-search.html"), DEALER_SEARCH)
    assert (page.total, page.page, page.pages, len(page.links)) == (14, 1, 1, 14)
    assert not page.listings
    assert not page.search_filters["country_filter_verified"]
    assert not page.search_filters["national_geography_verified"]
    assert page.search_filters["parse_status"] == "discovery_requires_own_detail"
    assert all(mobile_dealer_ad_id(url) for url in page.links)
    assert all(mobile_ad_id(url) is None for url in page.links)
    # Three WP GUIDs retain the old slug; their own post IDs are unchanged.
    assert any("/51912206/" in url for url in page.links)


@pytest.mark.parametrize("field", ["id", "object_id", "guid", "permalink", "post_status"])
def test_native_frontier_rejects_unbound_foreign_and_unpublished_items(field):
    state = native_state()
    item = state["preload_data"]["settings"]["listify"]["listings"][0]
    if field == "id":
        item["id"] = 123
    elif field == "object_id":
        item["object"]["ID"] = 123
    elif field == "guid":
        item["object"]["guid"] = item["object"]["guid"].replace("51908193", "123")
    elif field == "permalink":
        item["permalink"] = item["permalink"].replace(
            "fahrzeugmarkt.auto-straussberger.de", "evil.example"
        )
    else:
        item["object"]["post_status"] = "draft"
    page = parse_mobile_dealer(native_search(state), DEALER_SEARCH)
    assert len(page.links) == 13 and page.total == 14
    assert not page.listings


def test_live_native_own_detail_retains_exact_dealer_identity_price_and_stock_country():
    page = parse_mobile_dealer(str(native_soup()), NATIVE_URL)
    item = page.listings[0]
    assert (item.source, item.external_id, item.scope) == (DEALER_SOURCE, "51908193", "target")
    assert (item.price, item.price_type, item.mileage, item.year) == (
        24090,
        "advertised_price",
        46100,
        2025,
    )
    assert (item.transmission, item.fuel, item.seller, item.city) == (
        "automatic",
        "petrol",
        "Autohaus Straußberger",
        "Roßtal",
    )
    assert item.vin is None and item.kenteken is None
    assert item.raw["dealer_stock_id"] == "645"
    assert item.raw["country_evidence"] == [
        {
            "kind": "own_detail_stock_location",
            "text": "DE-90574 Roßtal",
            "url": NATIVE_URL,
            "own_id": "51908193",
        }
    ]
    assert item.raw["mobile_publication_identity_verified"] is False
    assert "Driving Assistant Plus" in item.description
    assert item.images and not item.raw.get("publication_reference")


@pytest.mark.parametrize(
    "mutation",
    [
        "body_id",
        "own_review",
        "review_url",
        "webpage",
        "heading",
        "location_missing",
        "location_foreign",
        "schema_country",
    ],
)
def test_native_detail_country_and_identity_cannot_come_from_other_page_parts(mutation):
    soup = native_soup()
    if mutation == "body_id":
        soup.body["class"] = [x.replace("51908193", "999") for x in soup.body["class"]]
    elif mutation in {"own_review", "review_url", "webpage", "schema_country"}:
        for script in soup.select('script[type="application/ld+json"]'):
            data = json.loads(script.string)
            if data.get("@type") == "Review":
                if mutation == "own_review":
                    data["@id"] = data["@id"].replace("51908193", "999")
                elif mutation == "review_url":
                    data["url"]["@id"] = data["url"]["@id"].replace("51908193", "999")
                elif mutation == "schema_country":
                    data["itemReviewed"]["address"]["addressCountry"] = "NL"
            if mutation == "webpage" and "@graph" in data:
                for obj in data["@graph"]:
                    if obj.get("@type") == "WebPage":
                        obj["url"] = obj["url"].replace("51908193", "999")
            script.string = json.dumps(data)
    elif mutation == "heading":
        soup.select_one("h1.job_listing-title").string = "OTHER CAR"
    elif mutation == "location_missing":
        soup.select_one(".listing_details_data-section-location").decompose()
        # Country in an unrelated footer cannot restore a missing own location.
        footer = soup.new_tag("footer")
        footer.string = "DE-90574 Roßtal"
        soup.body.append(footer)
    else:
        country = soup.select_one(".listing_details_data-section-location")
        country.string = "Standort: Autohaus Straußberger NL-90574 Roßtal"
    assert not parse_mobile_dealer(str(soup), NATIVE_URL).listings


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("net", (24090, "net_price")),
        ("monthly", (None, "monthly_payment")),
        ("currency", (None, "non_eur")),
        ("conflict", (None, "ambiguous_price")),
    ],
)
def test_native_finance_is_classified_from_own_price_boxes(kind, expected):
    soup = native_soup()
    sections = soup.select(".listing_details_data-section-price")
    for index, section in enumerate(sections):
        if kind == "net":
            section.select_one("p").string = "zzgl. MwSt."
        elif kind == "monthly":
            section.select_one("p").string = "pro Monat"
        elif kind == "currency":
            section.select_one(".currency_symbol").string = "CHF"
        elif index == 0:
            section.select_one(".listing_details_price").contents[0].replace_with("29.990 ")
    item = parse_mobile_dealer(str(soup), NATIVE_URL).listings[0]
    assert (item.price, item.price_type) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("WBA2A11070V123456", "WBA2A11070V123456"), ("V123456", None), ("WBA2A11070I123456", None)],
)
def test_native_vin_must_be_full_and_in_own_specifics(value, expected):
    soup = native_soup()
    dl = soup.select_one(".listify_widget_panel_listing_specifics dl")
    dd = soup.new_tag("dd", attrs={"data-title": "fahrgestellnummer"})
    dd.string = value
    dl.append(dd)
    item = parse_mobile_dealer(str(soup), NATIVE_URL).listings[0]
    assert item.vin == expected and item.kenteken is None


def test_native_recommendation_cannot_supply_country_vin_price_or_model():
    soup = native_soup()
    recommendation = soup.new_tag("aside")
    recommendation.string = "BMW 218i Active Tourer DE-90574 Roßtal WBA2A11070V123456 EUR 300"
    soup.body.append(recommendation)
    item = parse_mobile_dealer(str(soup), NATIVE_URL).listings[0]
    assert item.price == 24090 and item.vin is None
    soup.select_one("#secondary .job_listing-title").string = "BMW 218 Gran Tourer"
    assert parse_mobile_dealer(str(soup), NATIVE_URL).listings[0].scope == "excluded"


def native_transport():
    from car_market.fetch import Response

    captured = {record["metadata"]["requested_url"]: record for record in RECOVERY}
    calls = []

    def get(url):
        calls.append(url)
        record = captured[url]
        meta = record["metadata"]
        return Response(url, url, 200, fixture(record["fixture"]), meta["fetched_at"])

    return get, calls


def test_native_bounded_import_reaches_all_fourteen_own_details_without_fuzzy_merges(tmp_path):
    from car_market.collect import Collector
    from car_market.db import Database
    from car_market.germany import observe_german_item

    get, calls = native_transport()
    result = collect_mobile_dealer(get, max_details=14)
    assert result["status"] == "complete_dealer_subset"
    assert (result["expected_count"], result["received_count"], result["target_count"]) == (
        14,
        14,
        14,
    )
    assert len(calls) == len(set(calls)) == 15
    assert not result["national_coverage_verified"] and result["mobile_publications_verified"] == 0
    db = Database(tmp_path / "market.sqlite3")
    Collector(db, {"scope": {"country": "DE"}, "workers": 1})
    run = db.start_run()
    for item, response in zip(result["listings"], result["responses"][1:]):
        observe_german_item(db, run, item, response.timestamp)
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 14
    assert db.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 14
    assert not db.conn.execute("SELECT * FROM vehicle_aliases").fetchall()
    assert not db.conn.execute("SELECT * FROM vehicles WHERE kenteken IS NOT NULL").fetchall()
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize(
    "failure", ["budget", "blocked", "redirect", "counter", "pages", "country"]
)
def test_native_collection_never_certifies_budget_blocks_redirects_or_unverified_rows(failure):
    from car_market.fetch import Response

    get, calls = native_transport()

    def changed(url):
        response = get(url)
        if url == DEALER_SEARCH and failure in {"counter", "pages"}:
            state = native_state()
            pager = state["preload_data"]["settings"]["pager"]
            pager["total_rows" if failure == "counter" else "total_pages"] += 1
            response.body = native_search(state)
        elif url == NATIVE_URL:
            if failure == "blocked":
                return Response(url, url, 403, "Access denied", response.timestamp)
            if failure == "redirect":
                response.final_url = url.replace("51908193", "999")
            if failure == "country":
                response.body = response.body.replace("DE-90574", "NL-90574")
        return response

    result = collect_mobile_dealer(changed, max_details=1 if failure == "budget" else 14)
    assert result["status"] == "partial" and result["errors"]
    assert len(calls) == (2 if failure in {"budget", "blocked"} else 15)
    assert not result["national_coverage_verified"]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/suche/",
        "https://fahrzeugmarkt.auto-straussberger.de.evil.example/suche/",
        "https://user@fahrzeugmarkt.auto-straussberger.de/suche/",
    ],
)
def test_native_collector_rejects_unobserved_url_before_network(url):
    calls = []
    with pytest.raises(ValueError):
        collect_mobile_dealer(calls.append, url)
    assert not calls


@pytest.mark.parametrize("field", ["preload_data", "settings", "pager", "listify"])
def test_native_malformed_state_is_unparsed_and_does_not_invent_zero_stock(field):
    state = native_state()
    node = state
    if field != "preload_data":
        node = node["preload_data"]
    if field in {"pager", "listify"}:
        node = node["settings"]
    node[field] = ["unexpected structure"]
    page = parse_mobile_dealer(native_search(state), DEALER_SEARCH)
    assert page.total is None and not page.links and not page.listings


def test_native_own_schema_url_malformed_is_rejected_without_borrowing_recommendations():
    soup = native_soup()
    for script in soup.select('script[type="application/ld+json"]'):
        data = json.loads(script.string)
        if data.get("@type") == "Review":
            data["url"] = NATIVE_URL
            script.string = json.dumps(data)
    assert not parse_mobile_dealer(str(soup), NATIVE_URL).listings


def test_native_search_has_no_purchase_constraints_and_detail_accepts_manual_damaged_old_car():
    filters = parse_mobile_dealer(
        fixture("straussberger-search.html"), DEALER_SEARCH
    ).search_filters
    assert set(filters["effective_filters"]) == {"fwp_klasse", "_modell"}
    soup = native_soup()
    for key, value in {
        "getriebeart": "Schaltgetriebe",
        "zustand": "Unfallfahrzeug",
        "erstzulassung": "12/2014",
    }.items():
        soup.select_one('dd[data-title="' + key + '"]').string = value
    item = parse_mobile_dealer(str(soup), NATIVE_URL).listings[0]
    assert item.scope == "target" and item.transmission == "manual" and item.year == 2014
    assert item.raw["condition"] == "Unfallfahrzeug"


def test_native_own_seller_country_conflict_rejects_german_stock_location():
    soup = native_soup()
    seller = soup.select_one("#main .lc_listing_seller_contact_info")
    assert seller is not None
    seller.string = "Autohaus Straußberger NL-90574 Roßtal"
    assert not parse_mobile_dealer(str(soup), NATIVE_URL).listings
