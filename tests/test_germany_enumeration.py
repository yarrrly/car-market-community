"""Public fixtures verify the contract; census scenarios are modeled offline."""

import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from car_market.germany_autoscout import parse_autoscout_de
from car_market.germany_enumeration import autoscout_next_url, collect_capacity_partitions
from car_market.parse import Page

FIXTURES = Path(__file__).parent / "fixtures" / "germany_autoscout"
MANIFEST = {r["fixture"]: r for r in json.loads((FIXTURES / "manifest.json").read_text())}
ROOT_URL = MANIFEST["de-group-all.json"]["url"]
RUN = 17


def retained(name):
    return parse_autoscout_de((FIXTURES / name).read_text(), MANIFEST[name]["url"])


class MemoryDB:
    def finish_source(self, *args, **kwargs):
        self.finished = {"args": args, **kwargs}


class OfflineCollector:
    """No network Fetcher; every request reaches the local model."""

    def __init__(self, page_factory):
        self.country = "DE"
        self.db = MemoryDB()
        self.fetcher = SimpleNamespace(get=self.get)
        self.page_factory = page_factory
        self.requests = []
        self.ingested = []
        self.retained_fetches = []

    def get(self, url, refresh=False):
        self.requests.append(url)
        page = self.page_factory(url, len(self.requests))
        return SimpleNamespace(url=url, final_url=url, page=page, body=page, status=200, error=None)

    def _parse(self, source, body, url):
        return body if isinstance(body, Page) else Page([])

    def fetch_record(self, run, source, response, retain_raw=True):
        self.retained_fetches.append((run, source, response, retain_raw))

    def ingest_response(self, run, source, key, response):
        self.ingested.append((run, source, key, response.url))
        if getattr(response, "error", None):
            self.fetch_record(run, source, response)
            raise ValueError(response.error)
        if isinstance(response.page, Exception):
            raise response.page
        page = response.page
        return page, sum(x.scope != "excluded" for x in page.listings)


def query(**options):
    return {
        "source": "autoscout24_de",
        "key": "de-all-control",
        "url": ROOT_URL,
        "partition": "capacity",
        "coverage": "enumerate",
        **options,
    }


def rows(*groups):
    # Algorithm model only: these IDs do not represent real advertisements.
    result = []
    for price, count in groups:
        for _ in range(count):
            index = len(result)
            result.append(
                SimpleNamespace(
                    external_id=f"offline-id-{index}",
                    price=price,
                    scope="excluded" if index % 3 == 0 else "target",
                    raw={"market_country": "DE"},
                )
            )
    return result


def census_factory(inventory, mutate=None):
    base = retained("de-group-all.json").search_filters

    def factory(url, call):
        parameters = parse_qs(urlsplit(url).query)
        lower = int(parameters["pricefrom"][0]) if "pricefrom" in parameters else None
        upper = int(parameters["priceto"][0]) if "priceto" in parameters else None
        selected = [
            x
            for x in inventory
            if (lower is None or x.price is not None and x.price >= lower)
            and (upper is None or x.price is not None and x.price <= upper)
        ]
        page_number = int(parameters.get("page", ["1"])[0])
        fields = dict(base)
        for name in ("pricefrom", "priceto"):
            if name in parameters:
                fields[name] = parameters[name][0]
        page = Page(
            copy.deepcopy(selected[(page_number - 1) * 20 : page_number * 20]),
            len(selected),
            min((len(selected) + 19) // 20, 200),
            page_number,
            search_filters=fields,
        )
        return mutate(page, parameters, call) if mutate else page

    return factory


def run_census(inventory, mutate=None, **options):
    collector = OfflineCollector(census_factory(inventory, mutate))
    return collector, collect_capacity_partitions(collector, RUN, query(**options))


def assert_partial(collector, result, reason):
    assert result["status"] == "partial"
    assert not result["verified_census"] and not result["infer_missing"]
    assert collector.db.finished["infer_missing"] is False
    assert collector.db.finished["args"][4] == "partial"
    assert any(reason in error for error in result["errors"]), result["errors"]


def test_next_url_from_real_fixture_preserves_path_country_and_all_query_fields():
    first = retained("de-search.html")
    seed = MANIFEST["de-search.html"]["url"]
    result = autoscout_next_url(seed, 122, first)
    assert (
        urlsplit(result).path
        == "/_next/data/as24-search-funnel_main-20261007192840/lst/bmw/218.json"
    )
    expected = parse_qs(urlsplit(seed).query)
    expected["page"] = ["122"]
    assert parse_qs(urlsplit(result).query) == expected
    group = retained("de-group-all.json")
    assert urlsplit(autoscout_next_url(ROOT_URL, 200, group)).path.endswith("/lst/bmw.json")
    assert parse_qs(urlsplit(autoscout_next_url(ROOT_URL, 200, group)).query)[
        "damaged_listing"
    ] == ["include"]


def test_next_url_replaces_existing_build_and_preserves_price_bounds():
    first = retained("de-group-high.json")
    seed = MANIFEST["de-group-high.json"]["url"]
    next_url = autoscout_next_url(seed, 2, first)
    assert parse_qs(urlsplit(next_url).query)["pricefrom"] == ["20001"]
    stale = next_url.replace(first.search_filters["build_id"], "old-build")
    assert autoscout_next_url(stale, 3, first) == next_url.replace("page=2", "page=3")


@pytest.mark.parametrize("page", [0, 201, True, "2", -1])
def test_next_url_never_requests_false_empty_page_201(page):
    with pytest.raises(ValueError):
        autoscout_next_url(ROOT_URL, page, retained("de-group-all.json"))


@pytest.mark.parametrize("build", [None, "", "../evil", "version/path", "version?secret=x"])
def test_next_url_requires_safe_public_first_page_build(build):
    first = retained("de-group-all.json")
    first.search_filters["build_id"] = build
    with pytest.raises(ValueError):
        autoscout_next_url(ROOT_URL, 2, first)


def test_next_url_rejects_non_de_or_foreign_origin_before_requests():
    first = retained("de-group-all.json")
    with pytest.raises(ValueError):
        autoscout_next_url(ROOT_URL.replace("cy=D", "cy=NL"), 2, first)
    with pytest.raises(ValueError):
        autoscout_next_url(
            ROOT_URL.replace("www.autoscout24.de", "www.autoscout24.de.evil.example"), 2, first
        )
    first.search_filters["country"] = "NL"
    with pytest.raises(ValueError):
        autoscout_next_url(ROOT_URL, 2, first)


def test_real_5790_control_splits_have_equal_counters_but_are_not_a_census():
    def factory(url, call):
        p = parse_qs(urlsplit(url).query)
        name = (
            "de-group-low.json"
            if "priceto" in p
            else "de-group-high.json"
            if "pricefrom" in p
            else "de-group-all.json"
        )
        return retained(name)

    collector = OfflineCollector(factory)
    result = collect_capacity_partitions(collector, RUN, query(partition_request_limit=3))
    assert result["expected"] == result["partition_sum"] == 5790
    assert [(x["lower"], x["upper"], x["expected"]) for x in result["leaves"]] == [
        (None, 20000, 2469),
        (20001, None, 3321),
    ]
    assert len(collector.requests) == len(collector.ingested) == 3
    assert result["received"] < 5790
    assert_partial(collector, result, "partition_request_limit")


def test_offline_5790_full_leaf_union_verifies_all_ids_before_missing_inference():
    inventory = rows((13500, 2469), (28000, 3321))
    collector, result = run_census(inventory)
    assert result["status"] == "complete" and result["verified_census"]
    assert result["received"] == result["expected"] == result["partition_sum"] == 5790
    assert set(result["ids"]) == {item.external_id for item in inventory}
    assert set(result["root_ids"]) <= set(result["ids"])
    assert set(result["root_after_ids"]) <= set(result["ids"])
    assert result["targets"] == 3860
    assert len(collector.requests) == len(collector.ingested) == result["pages"] == 293
    assert collector.db.finished["infer_missing"] is True
    assert all(
        entry[:3] == (RUN, "autoscout24_de", "de-all-control") for entry in collector.ingested
    )
    assert collector.requests[0] == collector.requests[-1]
    assert all(
        int(parse_qs(urlsplit(url).query).get("page", ["1"])[0]) <= 200
        for url in collector.requests
    )


def test_recursive_leaf_capacity_splits_preserve_existing_bounds():
    inventory = rows((10000, 3000), (15000, 3000), (25000, 2001), (40000, 2000))
    collector, result = run_census(inventory, partition_request_limit=600)
    assert result["status"] == "complete" and result["received"] == 10001
    assert len(result["leaves"]) == 4
    assert all(x["expected"] <= 4000 for x in result["leaves"])
    assert [(x["lower"], x["upper"]) for x in result["leaves"]] == [
        (None, 10000),
        (10001, 20000),
        (20001, 25000),
        (25001, None),
    ]
    assert all("cy=D" in url for url in collector.requests)
    assert len(collector.requests) == 505


@pytest.mark.parametrize(
    "options,reason",
    [
        ({"partition_request_limit": 2}, "partition_request_limit"),
        ({"partition_depth_limit": 0}, "partition_depth_limit"),
        ({"page_limit": 1}, "page_limit_exceeded"),
    ],
)
def test_bounds_do_not_turn_truncated_work_into_complete(options, reason):
    collector, result = run_census(rows((10000, 2469), (28000, 3321)), **options)
    assert_partial(collector, result, reason)
    assert len(collector.requests) <= options.get("partition_request_limit", 512)


def test_page_200_source_cap_is_a_leaf_boundary_not_empty_inventory():
    collector, result = run_census(rows((10000, 4000)))
    assert (
        result["status"] == "complete" and result["pages"] == 201
    )  # 200 pages plus the root control.
    assert (
        max(int(parse_qs(urlsplit(url).query).get("page", ["1"])[0]) for url in collector.requests)
        == 200
    )


def test_unknown_price_gap_is_partial_even_if_source_heads_were_ingested():
    inventory = rows((None, 1), (10000, 2000), (28000, 2000))
    collector, result = run_census(inventory)
    assert result["expected"] == 4001 and result["partition_sum"] == 4000
    assert_partial(collector, result, "unknown_price_in_split")
    assert any("partition_counter_mismatch" in x for x in result["errors"])


def test_fractional_euro_gap_cannot_certify_integer_price_partitions():
    collector, result = run_census(rows((20000.5, 1), (10000, 2000), (28000, 2000)))
    assert_partial(collector, result, "fractional_price_partition_gap")


def test_cross_leaf_duplicate_ids_fail_even_when_partition_counters_sum():
    inventory = rows((10000, 2001), (28000, 2000))
    inventory[2001].external_id = inventory[0].external_id
    collector, result = run_census(inventory)
    assert result["partition_sum"] == result["expected"] == 4001
    assert result["received"] == 4000
    assert_partial(collector, result, "partition_id_overlap")


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("repeat", "repeated_page_ids"),
        ("country", "country_unconfirmed"),
        ("filter", "search_filters_changed"),
        ("page", "page_number_mismatch"),
        ("counter", "counter_changed"),
        ("empty", "page_card_count_mismatch"),
        ("error", "http_403"),
    ],
)
def test_broken_leaf_pages_never_infer_missing(mutation, reason):
    inventory = rows((10000, 45))

    def mutate(page, parameters, call):
        if parameters.get("page") != ["2"]:
            return page
        if mutation == "repeat":
            page.listings = copy.deepcopy(inventory[:20])
        elif mutation == "country":
            page.listings[0].raw["market_country"] = "NL"
        elif mutation == "filter":
            page.search_filters["damaged_listing"] = "exclude"
        elif mutation == "page":
            page.page = 1
        elif mutation == "counter":
            page.total = 46
        elif mutation == "empty":
            page.listings = []
        elif mutation == "error":
            return ValueError("http_403")
        return page

    collector, result = run_census(inventory, mutate)
    assert_partial(collector, result, reason)


@pytest.mark.parametrize("change", ["counter", "same_count_new_id"])
def test_root_refresh_detects_drift_after_complete_leaf_count(change):
    def mutate(page, parameters, call):
        if call == 4:  # 45 cards: three pages, then repeat the root.
            if change == "counter":
                page.total = 46
            else:
                page.listings[0].external_id = "new-id-after-census"
        return page

    collector, result = run_census(rows((10000, 45)), mutate)
    assert result["received"] == 45
    assert_partial(
        collector, result, "root_changed" if change == "counter" else "root_after_ids_not_covered"
    )


def test_no_budget_for_final_root_refresh_keeps_complete_union_partial():
    collector, result = run_census(rows((10000, 45)), partition_request_limit=3)
    assert result["received"] == result["expected"] == 45
    assert_partial(collector, result, "partition_request_limit")


def test_final_root_bypasses_fetcher_cache_and_detects_live_drift():
    inventory = rows((10000, 45))
    factory = census_factory(inventory)
    collector = OfflineCollector(factory)
    cache, calls, fresh = {}, [], []

    def cached_get(url, refresh=False):
        calls.append((url, refresh))
        if url in cache and not refresh:
            return cache[url]
        page = factory(url, len(calls))
        if refresh:
            fresh.append(url)
            # Without a cache refresh, this new own ID would remain unseen.
            page.listings[0].external_id = "live-replacement-after-census"
        response = SimpleNamespace(url=url, final_url=url, page=page)
        cache[url] = response
        return response

    collector.fetcher.get = cached_get
    result = collect_capacity_partitions(collector, RUN, query())
    assert len(calls) == 4 and len(collector.ingested) == 4
    assert fresh == [calls[0][0]]
    assert calls[0][1] is False and calls[-1] == (calls[0][0], True)
    assert "live-replacement-after-census" in result["root_after_ids"]
    assert_partial(collector, result, "root_after_ids_not_covered")


def test_verified_census_respects_explicit_disabled_missing_inference():
    collector, result = run_census(rows((10000, 45)), infer_missing=False)
    assert result["status"] == "complete" and result["verified_census"] is True
    assert result["received"] == result["expected"] == 45
    assert result["infer_missing"] is False
    assert collector.db.finished["args"][4] == "complete"
    assert collector.db.finished["infer_missing"] is False


@pytest.mark.parametrize("failure", [404, 410, "wrong_page", "wrong_filter"])
def test_retained_failed_next_then_real_html_payload_can_verify_census(failure):
    # Real own advertisements from two public pages. Only the counter
    # is capped at 40 for the offline scenario; the build models a deployment change.
    def document(name):
        body = (FIXTURES / name).read_text()
        return json.loads(re.search(r"<script[^>]*>(.*?)</script>", body, re.DOTALL)[1])

    first_doc, second_doc = document("de-search.html"), document("de-page2.html")
    for doc in (first_doc, second_doc):
        doc["props"]["pageProps"]["numberOfResults"] = 40
        doc["props"]["pageProps"]["numberOfPages"] = 2
    second_doc["buildId"] = "new-public-build"
    wrong_doc = copy.deepcopy(second_doc)
    if failure == "wrong_page":
        wrong_doc["props"]["pageProps"]["pageQuery"]["page"] = "1"
    elif failure == "wrong_filter":
        wrong_doc["props"]["pageProps"]["pageQuery"]["damaged_listing"] = "include"

    class PublicHTMLCollector(OfflineCollector):
        def get(self, url, refresh=False):
            self.requests.append(url)
            next_route = urlsplit(url).path.startswith("/_next/data/")
            if next_route:
                status = failure if isinstance(failure, int) else 200
                body = "Deployment not found" if status != 200 else json.dumps(wrong_doc)
                problem = "http_" + str(status) if status != 200 else None
            else:
                status, problem = 200, None
                doc = (
                    second_doc if parse_qs(urlsplit(url).query).get("page") == ["2"] else first_doc
                )
                if refresh and doc is first_doc:
                    doc = {**first_doc, "buildId": "new-public-build"}
                body = (
                    '<script id="__NEXT_DATA__" type="application/json">'
                    + json.dumps(doc)
                    + "</script>"
                )
            return SimpleNamespace(url=url, final_url=url, status=status, body=body, error=problem)

        def _parse(self, source, body, url):
            return parse_autoscout_de(body, url)

        def ingest_response(self, run, source, key, response):
            self.ingested.append((run, source, key, response.url))
            if response.error:
                self.fetch_record(run, source, response)
                raise ValueError(response.error)
            page = self._parse(source, response.body, response.final_url)
            assert page.search_filters.get("country") == "DE"
            return page, sum(x.scope != "excluded" for x in page.listings)

    collector = PublicHTMLCollector(None)
    seed = MANIFEST["de-search.html"]["url"]
    result = collect_capacity_partitions(collector, RUN, query(url=seed))
    assert result["status"] == "complete" and result["received"] == result["expected"] == 40
    assert result["pages"] == len(collector.requests) == 4
    assert len(collector.ingested) == 3  # Invalid Next data cannot create own observations.
    assert len(collector.retained_fetches) == 1
    _, source, failed, raw_retained = collector.retained_fetches[0]
    assert source == "autoscout24_de" and raw_retained is True
    assert failed.body and failed.status == (failure if isinstance(failure, int) else 200)
    recovery = result["fallbacks"][0]
    assert recovery["recovered"] is True and len(result["fallbacks"]) == 1
    assert recovery["next_url"] == collector.requests[1]
    assert recovery["html_url"] == collector.requests[2]
    assert urlsplit(recovery["html_url"]).path == urlsplit(seed).path
    expected_query = parse_qs(urlsplit(seed).query)
    expected_query["page"] = ["2"]
    assert parse_qs(urlsplit(recovery["html_url"]).query) == expected_query
    assert collector.db.finished["infer_missing"] is True


@pytest.mark.parametrize(
    "status,body,problem",
    [
        (403, "Forbidden", "http_403"),
        (429, "Rate limit", "http_429"),
        (200, "CAPTCHA required", "captcha"),
        (404, "CAPTCHA required", "http_404"),
    ],
)
def test_no_html_fallback_for_access_blocks_or_captcha(status, body, problem):
    collector = OfflineCollector(census_factory(rows((10000, 40))))
    original_get = collector.get

    def blocked_get(url, refresh=False):
        if urlsplit(url).path.startswith("/_next/data/"):
            collector.requests.append(url)
            return SimpleNamespace(
                url=url, final_url=url, status=status, body=body, error=problem, page=Page([])
            )
        return original_get(url, refresh=refresh)

    collector.fetcher.get = blocked_get
    result = collect_capacity_partitions(collector, RUN, query())
    assert_partial(collector, result, problem)
    assert result["fallbacks"] == []
    assert len(collector.requests) == 2 and len(collector.retained_fetches) == 1


def test_expired_next_retained_before_request_budget_blocks_html_fallback():
    collector = OfflineCollector(census_factory(rows((10000, 40))))
    original_get = collector.get

    def expired_get(url, refresh=False):
        if urlsplit(url).path.startswith("/_next/data/"):
            collector.requests.append(url)
            return SimpleNamespace(
                url=url, final_url=url, status=404, body="Expired deployment", error="http_404"
            )
        return original_get(url, refresh=refresh)

    collector.fetcher.get = expired_get
    result = collect_capacity_partitions(collector, RUN, query(partition_request_limit=2))
    assert_partial(collector, result, "partition_request_limit")
    assert len(collector.requests) == 2 and len(collector.retained_fetches) == 1
    assert len(result["fallbacks"]) == 1 and result["fallbacks"][0]["recovered"] is False


def test_invalid_html_fallback_is_not_retried_or_counted_as_recovery():
    def mutation(page, parameters, call):
        if parameters.get("page") == ["2"]:
            page.page = 1
        return page

    collector, result = run_census(rows((10000, 40)), mutation)
    assert_partial(collector, result, "page_number_mismatch")
    assert len(result["fallbacks"]) == 1 and not result["fallbacks"][0]["recovered"]
    assert len(collector.retained_fetches) == 1 and len(collector.requests) == 3


def test_verified_html_build_is_used_for_next_page_without_repeating_expired_build():
    collector = OfflineCollector(census_factory(rows((10000, 45))))
    original_get = collector.get
    old_build = retained("de-group-all.json").search_filters["build_id"]

    def deploy_get(url, refresh=False):
        next_route = urlsplit(url).path.startswith("/_next/data/")
        if next_route and old_build in url:
            collector.requests.append(url)
            return SimpleNamespace(
                url=url, final_url=url, status=410, body="Expired build", error="http_410"
            )
        response = original_get(url, refresh=refresh)
        if next_route or refresh:
            response.page.search_filters["build_id"] = "fresh-build"
        return response

    collector.fetcher.get = deploy_get
    result = collect_capacity_partitions(collector, RUN, query())
    assert result["status"] == "complete" and result["received"] == 45
    assert len(result["fallbacks"]) == 1 and result["fallbacks"][0]["recovered"]
    assert len(collector.requests) == 5
    assert "/_next/data/fresh-build/" in collector.requests[3]


def test_zero_counter_requires_two_country_confirmed_observations():
    collector, result = run_census([])
    assert result["status"] == "complete" and result["expected"] == result["received"] == 0
    assert len(collector.ingested) == 2 and collector.db.finished["infer_missing"] is True


def test_single_zero_euro_bucket_above_capacity_is_unsplittable_not_empty():
    collector, result = run_census(rows((0, 4001)), partition_depth_limit=20)
    assert_partial(collector, result, "unsplittable_price_capacity")
    assert len(collector.requests) < 50


@pytest.mark.parametrize(
    "options",
    [
        {"partition_request_limit": True},
        {"partition_request_limit": 2001},
        {"partition_depth_limit": 21},
        {"url": ROOT_URL.replace("cy=D", "cy=NL")},
    ],
)
def test_invalid_limits_or_country_fail_before_any_network_style_request(options):
    collector, result = run_census(rows((10000, 1)), **options)
    assert result["status"] == "partial" and not collector.requests
    assert collector.db.finished["infer_missing"] is False
