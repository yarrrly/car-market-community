"""Copied native search URLs must not silently create unparseable searches."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from car_market.configuration import validate
from car_market.portable import main
from car_market.parse import parse_page


@pytest.mark.parametrize("country,cy", [("NL", "NL"), ("DE", "D")])
def test_init_normalizes_country_for_a_copied_canonical_search(tmp_path, capsys, country, cy):
    url = f"https://www.autoscout24.{country.lower()}/lst/toyota/corolla?sort=age&desc=1"
    assert main(["--workspace", str(tmp_path), "init", "--make", "Toyota", "--model", "Corolla", "--country", country, "--url", url]) == 0
    cfg = json.loads((tmp_path / "search.json").read_text())
    assert json.loads(capsys.readouterr().out)["search_urls"] == [cfg["queries"][0]["url"]]
    query = parse_qs(urlsplit(cfg["queries"][0]["url"]).query)
    assert query == {"sort": ["age"], "desc": ["1"], "cy": [cy]}
    assert main(["--workspace", str(tmp_path), "validate"]) == 0


@pytest.mark.parametrize("bad", ["", "?cy=DE", "?cy=NL", "?cy=D&cy=NL"])
def test_manual_german_config_rejects_missing_or_conflicting_country_before_io(tmp_path, bad):
    assert main(["--workspace", str(tmp_path), "init", "--make", "Toyota", "--model", "Corolla", "--country", "DE"]) == 0
    cfg = json.loads((tmp_path / "search.json").read_text())
    cfg["queries"][0]["url"] = "https://www.autoscout24.de/lst/toyota/corolla" + bad
    with pytest.raises(ValueError, match="cy="):
        validate(cfg)
    assert not (tmp_path / "data/market.sqlite3").exists()


def test_copied_marktplaats_api_uses_offset_pagination_and_starts_at_the_first_page(tmp_path):
    url = "https://www.marktplaats.nl/lrp/api/search?query=Toyota+Corolla&limit=30&offset=60"
    assert main(["--workspace", str(tmp_path), "init", "--make", "Toyota", "--model", "Corolla", "--url", url]) == 0
    cfg = json.loads((tmp_path / "search.json").read_text())
    assert cfg["queries"][0]["pagination"] == "marktplaats_api"
    assert parse_qs(urlsplit(cfg["queries"][0]["url"]).query)["offset"] == ["0"]
    cfg["queries"][0]["pagination"] = "marktplaats"
    with pytest.raises(ValueError, match="needs marktplaats_api"):
        validate(cfg)


def test_marktplaats_native_cap_is_not_the_query_page_count():
    payload = {
        "searchRequest": {"pagination": {"offset": 0, "limit": 30}},
        "totalResultCount": 795,
        "maxAllowedPageNumber": 167,
        "listings": [],
    }
    page = parse_page("marktplaats", json.dumps(payload), "https://www.marktplaats.nl/lrp/api/search?offset=0&limit=30")
    assert page.pages == 27
    assert page.search_filters["page_cap"] == 167
    payload["totalResultCount"] = 7862
    page = parse_page("marktplaats", json.dumps(payload), "https://www.marktplaats.nl/lrp/api/search?offset=0&limit=30")
    assert page.pages == 167
