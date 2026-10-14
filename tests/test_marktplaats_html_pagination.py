"""Copied HTML routing and native evidence for unsupported filter spellings."""

import gzip
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from car_market.collect import page_url
from car_market.configuration import validate
from car_market.configured_collect import ConfiguredCollector
from car_market.parse import Page
from car_market.portable import main

FIXTURES = Path(__file__).parent / "fixtures/marktplaats_html_routing"


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "?query=Corolla&sortBy=SORT_INDEX",
        "#q:Corolla|sortBy:SORT_INDEX",
        "?query=Corolla#sortBy:SORT_INDEX",
        "p/3/",
        "p/1/",
    ],
)
def test_page_helper_changes_only_url_components(suffix):
    # This helper contract does not certify the publisher's query/fragment filters.
    url = "https://www.marktplaats.nl/l/auto-s/toyota/q/corolla/" + suffix
    parsed = urlsplit(url)
    actual = urlsplit(page_url({"url": url, "pagination": "marktplaats"}, 2, Page([])))
    assert actual.path == "/l/auto-s/toyota/q/corolla/p/2/"
    assert actual.query == parsed.query
    assert actual.fragment == parsed.fragment
    assert actual.netloc == parsed.netloc


@pytest.mark.parametrize("page", [1, 3, 12])
def test_init_normalizes_copied_html_to_the_first_page(tmp_path, page):
    url = f"https://www.marktplaats.nl/l/auto-s/toyota/q/corolla/p/{page}/"
    assert (
        main(
            [
                "--workspace",
                str(tmp_path),
                "init",
                "--make",
                "Toyota",
                "--model",
                "Corolla",
                "--url",
                url,
            ]
        )
        == 0
    )
    config = json.loads((tmp_path / "search.json").read_text())
    first = urlsplit(config["queries"][0]["url"])
    assert first.path == "/l/auto-s/toyota/q/corolla/"
    assert not first.query and not first.fragment
    validate(config)
    config["queries"][0]["url"] = url
    if page != 1:
        with pytest.raises(ValueError, match="must start at page 1"):
            validate(config)
    assert not (tmp_path / "data/market.sqlite3").exists()


@pytest.mark.parametrize(
    "suffix", ["?query=Corolla&sortBy=SORT_INDEX", "#q:Corolla|sortBy:SORT_INDEX"]
)
def test_uncertified_html_filters_fail_before_workspace_or_database_io(
    tmp_path, capsys, suffix
):
    workspace = tmp_path / "new-search"
    url = "https://www.marktplaats.nl/l/auto-s/toyota/" + suffix
    assert (
        main(
            [
                "--workspace",
                str(workspace),
                "init",
                "--make",
                "Toyota",
                "--model",
                "Corolla",
                "--url",
                url,
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().err)["message"]
    assert "query/fragment filters are not certified" in error
    assert "/lrp/api/search" in error
    assert not workspace.exists()


@pytest.mark.parametrize("suffix", ["?query=Corolla", "#q:Corolla"])
def test_manual_html_filters_are_also_rejected(tmp_path, suffix):
    url = "https://www.marktplaats.nl/l/auto-s/toyota/q/corolla/"
    assert (
        main(
            [
                "--workspace",
                str(tmp_path),
                "init",
                "--make",
                "Toyota",
                "--model",
                "Corolla",
                "--url",
                url,
            ]
        )
        == 0
    )
    config = json.loads((tmp_path / "search.json").read_text())
    config["queries"][0]["url"] += suffix
    with pytest.raises(ValueError, match="filters are not certified"):
        validate(config)


def test_api_control_preserves_filters_and_normalizes_offset(tmp_path):
    url = "https://www.marktplaats.nl/lrp/api/search?query=Toyota+Corolla&limit=30&offset=60"
    assert (
        main(
            [
                "--workspace",
                str(tmp_path),
                "init",
                "--make",
                "Toyota",
                "--model",
                "Corolla",
                "--url",
                url,
            ]
        )
        == 0
    )
    query = json.loads((tmp_path / "search.json").read_text())["queries"][0]
    assert query["pagination"] == "marktplaats_api"
    first = parse_qs(urlsplit(query["url"]).query)
    second = parse_qs(urlsplit(page_url(query, 2, Page([]))).query)
    assert first == {"query": ["Toyota Corolla"], "limit": ["30"], "offset": ["0"]}
    assert second == {**first, "offset": ["30"]}


@pytest.mark.parametrize("name", ["fragment", "query", "path"])
def test_native_html_filter_proofs_preserve_owned_ids_and_counter(name):
    metadata = next(
        row
        for row in json.loads((FIXTURES / "manifest.json").read_text())
        if row["file"] == name + ".native.json.gz"
    )
    compressed = (FIXTURES / metadata["file"]).read_bytes()
    body = gzip.decompress(compressed)
    assert hashlib.sha256(compressed).hexdigest() == metadata["fixture_gzip_sha256"]
    assert hashlib.sha256(body).hexdigest() == metadata["fixture_sha256"]
    assert len(metadata["source_sha256"]) == 64
    assert metadata["public_url"].startswith("https://www.marktplaats.nl/l/auto-s/")
    assert metadata["captured_at"].startswith("2026-10-11T18:04:")
    native = json.loads(body)
    collector = object.__new__(ConfiguredCollector)
    collector.country = "NL"
    collector.config = {
        "scope": {
            "model_profile": "configured_vehicle",
            "country": "NL",
            "selector": {"make": "Toyota", "model_terms": [["Corolla"]]},
        }
    }
    page = collector._parse("marktplaats", body.decode(), metadata["public_url"])
    assert {item.external_id for item in page.listings} == set(metadata["native_ids"])
    assert page.total == native["totalResultCount"] == metadata["native_counter"]
    assert native["searchRequest"].get("searchQuery") == metadata["native_search_query"]
    if name == "path":
        assert page.total == 783 and page.search_filters["searchQuery"] == "corolla"
    else:
        assert page.total == 9962 and "searchQuery" not in page.search_filters
        assert any("yaris" in item.title.lower() for item in page.listings)
