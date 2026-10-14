"""A search without --url starts at the model route and falls back to the make."""

import json

import pytest

from car_market.configuration import validate
from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database, now
from car_market.fetch import Response
from car_market.portable import main


def init(tmp_path, *extra):
    args = ["--workspace", str(tmp_path), "init", "--make", "Mercedes-Benz", "--model", "C180"]
    assert main([*args, *extra]) == 0
    return json.loads((tmp_path / "search.json").read_text())


@pytest.mark.parametrize("country,host,cy", [("NL", "nl", "NL"), ("DE", "de", "D")])
def test_default_search_uses_model_route_with_make_fallback(tmp_path, country, host, cy):
    query = init(tmp_path, "--country", country)["queries"][0]
    assert query["url"].startswith(f"https://www.autoscout24.{host}/lst/mercedes-benz/c-180?")
    assert query["fallback_url"].startswith(f"https://www.autoscout24.{host}/lst/mercedes-benz?")
    assert f"cy={cy}" in query["url"] and f"cy={cy}" in query["fallback_url"]
    assert main(["--workspace", str(tmp_path), "validate"]) == 0


def test_copied_url_has_no_generated_fallback(tmp_path):
    url = "https://www.autoscout24.nl/lst/toyota/corolla?cy=NL"
    query = init(tmp_path, "--url", url)["queries"][0]
    assert "fallback_url" not in query


@pytest.mark.parametrize(
    "fallback",
    [
        "http://www.autoscout24.nl/lst/mercedes-benz?cy=NL",
        "https://www.autoscout24.de/lst/mercedes-benz?cy=NL",
        "https://www.autoscout24.nl/lst/mercedes-benz?cy=D",
        "https://example.test/lst/mercedes-benz?cy=NL",
    ],
)
def test_fallback_must_stay_on_the_same_search_host_and_country(tmp_path, fallback):
    config = init(tmp_path)
    config["queries"][0]["fallback_url"] = fallback
    with pytest.raises(ValueError, match="fallback_url"):
        validate(config)


def test_unknown_model_route_is_recorded_and_the_make_route_is_used(tmp_path):
    config = init(tmp_path)
    query = config["queries"][0]
    db = Database(tmp_path / "market.sqlite3")
    collector = ConfiguredCollector(db, config)
    requested = []

    def get(url, refresh=False, **_):
        requested.append(url)
        status = 404 if url == query["url"] else 503
        return Response(url, url, status, "<html></html>", now(), "http_" + str(status))

    collector.fetcher.get = get
    try:
        collector.run(details=False, checks=False)
        assert requested[:2] == [query["url"], query["fallback_url"]]
        statuses = [row[0] for row in db.conn.execute("SELECT status FROM fetches ORDER BY id")]
        assert statuses[:2] == [404, 503]
        note = db.conn.execute("SELECT error FROM source_runs").fetchone()[0]
        assert "model_route_not_found_used_make_route" in note
    finally:
        db.conn.close()


def test_unknown_make_stops_with_a_spelling_hint(tmp_path):
    config = init(tmp_path)
    db = Database(tmp_path / "market.sqlite3")
    collector = ConfiguredCollector(db, config)
    collector.fetcher.get = lambda url, refresh=False, **_: Response(url, url, 404, "<html></html>", now(), "http_404")
    try:
        collector.run(details=False, checks=False)
        note = db.conn.execute("SELECT error FROM source_runs").fetchone()[0]
        assert "make_route_not_found" in note
        assert db.conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 2
    finally:
        db.conn.close()
