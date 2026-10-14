import json
from pathlib import Path

import pytest
from car_market.mercedes_autoscout import parse_mercedes_autoscout

R = Path(__file__).parent / "fixtures/mercedes_autoscout"
CASES = json.loads((R / "manifest.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_native_generation_and_country_bound_to_own_ad(case):
    body = (R / case["file"]).read_text()
    own = json.loads(body)["props"]["pageProps"]["listingDetails"]
    page = parse_mercedes_autoscout(body, own["webPage"], case["country"])
    assert len(page.listings) == 1
    car = page.listings[0]
    generation = own["vehicle"]["rawData"]["classification"]["modelGeneration"]["formatted"]
    assert car.scope == ("target" if generation == "205" else "excluded")
    assert car.transmission == "automatic"
    assert car.raw["market_country"] == case["country"]
    assert car.external_id == case["publication_id"]


@pytest.mark.parametrize(
    "mutation",
    ["foreign_host", "wrong_uuid", "wrong_webpage", "inactive", "old_generation", "wrong_country"],
)
def test_detail_mutations_do_not_target(mutation):
    case = next(c for c in CASES if c["country"] == "NL")
    d = json.loads((R / case["file"]).read_text())
    own = d["props"]["pageProps"]["listingDetails"]
    url = own["webPage"]
    if mutation == "foreign_host":
        url = url.replace("www.autoscout24.nl", "foreign.example")
    elif mutation == "wrong_uuid":
        own["id"] = "00000000-0000-0000-0000-000000000000"
    elif mutation == "wrong_webpage":
        own["webPage"] = own["webPage"].replace("www.autoscout24.nl", "foreign.example")
    elif mutation == "inactive":
        own["status"] = "Inactive"
    elif mutation == "old_generation":
        own["vehicle"]["rawData"]["classification"]["modelGeneration"]["formatted"] = "204"
    elif mutation == "wrong_country":
        own["location"]["countryCode"] = "DE"
    page = parse_mercedes_autoscout(json.dumps(d), url, "NL")
    assert not any(c.scope == "target" for c in page.listings)


SEARCH_CASES = json.loads((R / "search-manifest.json").read_text())


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_retained_full_search_pages_reconcile_own_counter(country):
    pages = [
        parse_mercedes_autoscout((R / c["file"]).read_text(), c["url"], country)
        for c in SEARCH_CASES
        if c["country"] == country
    ]
    assert sorted(p.page for p in pages) == list(range(1, pages[0].pages + 1))
    assert len({car.external_id for p in pages for car in p.listings}) == pages[0].total
    assert all(p.total == pages[0].total for p in pages)
    assert all(p.search_filters == pages[0].search_filters for p in pages)
    assert all(car.scope != "target" for p in pages for car in p.listings)


@pytest.mark.parametrize("script_type", ["text/javascript", "text/plain", None])
def test_html_requires_one_typed_owned_state(script_type):
    case = CASES[0]
    body = (R / case["file"]).read_text()
    url = json.loads(body)["props"]["pageProps"]["listingDetails"]["webPage"]
    attr = "" if script_type is None else ' type="' + script_type + '"'
    assert (
        parse_mercedes_autoscout(
            '<script id="__NEXT_DATA__"' + attr + ">" + body + "</script>", url, case["country"]
        ).listings
        == []
    )


@pytest.mark.parametrize("page", [0, 201, True])
def test_next_pagination_never_invents_out_of_bounds_route(page):
    from car_market.mercedes_autoscout import mercedes_autoscout_page_url

    case = SEARCH_CASES[0]
    first = parse_mercedes_autoscout((R / case["file"]).read_text(), case["url"], case["country"])
    with pytest.raises(ValueError):
        mercedes_autoscout_page_url({"url": case["url"]}, page, first)


def test_actual_c205_native_publication_survives_rdw_approval_type_204():
    from car_market.model_profiles import qualify_mercedes

    proof = json.loads((R / "nl-rdw-approval-type-c205.json").read_text())
    case = next(v for v in CASES if v["publication_id"] == "c9ee5a94-fe8d-4d49-8a2c-f5c2f616a5b7")
    doc = json.loads((R / case["file"]).read_text())
    own = doc["props"]["pageProps"]["listingDetails"]
    car = parse_mercedes_autoscout(json.dumps(doc), own["webPage"], "NL").listings[0]
    assert car.kenteken == proof["own_rdw_fields"]["kenteken"]
    assert proof["own_rdw_fields"]["type"] == "204"
    car.raw["rdw_model_evidence"] = {
        "kenteken": car.kenteken,
        "data": proof["own_rdw_fields"],
        "fetch_id": proof["provenance"]["fetch_id"],
        "checked_at": proof["provenance"]["fetched_at"],
    }
    assert qualify_mercedes(car, "NL").scope == "target"
    car.raw["vehicle"]["rawData"]["classification"]["modelGeneration"]["formatted"] = "204"
    assert qualify_mercedes(car, "NL").scope == "excluded"
