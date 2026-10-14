"""Terminal summaries stay readable; pipes and --json keep the JSON contract."""

import json
from types import SimpleNamespace

from car_market import human
from car_market.portable import main, show


def test_source_notes_become_plain_sentences():
    notes = human.explain("page_limit_exceeded; count_mismatch:255/11792; 0 errors; 205 deferred")
    assert notes == [
        "stopped at the page limit; raise --max-pages to read more",
        "read 255 of the 11792 results the site reports",
        "205 detail pages left for the next pass",
    ]
    assert human.explain("model_route_not_found_used_make_route")[0].startswith("the site has no page")
    assert human.explain("something_new") == ["something_new"]
    assert human.explain(None) == []


def test_listing_table_shows_price_year_mileage_and_a_next_step():
    text = human.listings(
        {
            "advertisements": [
                {
                    "price_eur": 14950.0,
                    "year": 2020,
                    "mileage_km": 158038,
                    "transmission": "manual",
                    "fuel": "petrol",
                    "title": "Lorem ipsum dolor " * 10,
                    "city": "LOREM",
                }
            ]
        }
    )
    assert "€14 950" in text and "2020" in text and "158 038 km" in text
    assert "…" in text and "Lorem" in text
    assert "car-market vehicle <ID>" in text
    assert "No matching listings" in human.listings({"advertisements": []})


def test_status_summary_without_data_points_to_a_first_command():
    text = human.status({"config": "valid", "database": "not_created", "daemon": "not_running"})
    assert "No data yet" in text and "not running" in text


def test_show_prints_json_when_not_a_terminal_or_when_asked(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    show(SimpleNamespace(json=True), "sources", {"a": 1})
    assert json.loads(capsys.readouterr().out) == {"a": 1}


def test_sources_command_keeps_json_for_pipes(capsys):
    assert main(["sources"]) == 0
    assert "autoscout24" in json.loads(capsys.readouterr().out)
    assert main(["sources", "--json"]) == 0
    assert "marktplaats" in json.loads(capsys.readouterr().out)


def test_vehicle_card_lists_offers_and_points_to_full_evidence():
    text = human.vehicle(
        {
            "id": "cb18aa7f-0000-0000-0000-000000000000",
            "title": "Lorem ipsum",
            "year": 2003,
            "mileage_km": 205184,
            "fuel": "petrol",
            "transmission": "manual",
            "price_min_eur": 1499.0,
            "price_max_eur": 1499.0,
            "offers": [
                {
                    "source": "autoscout24",
                    "price_eur": 1499.0,
                    "is_live": True,
                    "last_seen": "2026-10-14T08:08:57.6+00:00",
                    "url": "https://example.test/ad",
                }
            ],
            "field_conflicts": {"year": [2003, 2004], "city": []},
        }
    )
    assert "205 184 km" in text and "€1 499" in text
    assert "autoscout24: €1 499, live, last seen 2026-10-14 08:08 UTC" in text
    assert "Sources disagree on: year" in text
    assert "--json" in text
    assert "No vehicle" in human.vehicle(None)


def test_version_flag_prints_the_package_version(capsys):
    import pytest

    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.startswith("car-market ")
