"""Independent numeric oracles, read-only snapshots and publication failure checks."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pypdf import PdfReader

from car_market.analytics import (
    analyze_market,
    comparable_groups,
    damaged,
    generate,
    quantiles,
)
from car_market.configuration import bind
from car_market.db import Database, now
from car_market.parse import Listing
from car_market.search_profile import GENERIC_PROFILE, qualify


def database(path, country="NL", model="Corolla", count=5, *, all_unknown=False):
    db = Database(path)
    config = {
        "scope": {
            "model_profile": GENERIC_PROFILE,
            "country": country,
            "selector": {"make": "Toyota", "model_terms": [[model]]},
        }
    }
    bind(db, config)
    run = db.start_run("fixture")
    for index in range(count):
        car = Listing(
            "autoscout24" if country == "NL" else "autoscout24_de",
            str(index),
            "https://example.test/" + str(index),
            "Toyota " + model,
            price=10000 + index * 1000 + (2000 if country == "DE" else 0),
            year=None if all_unknown else 2020,
            mileage=None if all_unknown else 70000,
            transmission=None if all_unknown else "automatic",
            fuel="petrol",
            raw={"location": {"countryCode": country}},
        )
        db.observe(run, qualify(car, config["scope"]), now())
    db.finish_run(run, "complete", {})
    db.conn.close()
    return config


def test_quantile_linear_interpolation_and_invalid_values():
    assert quantiles([0, 10, 20, 30]) == {
        "n": 4,
        "min": 0,
        "q1": 7.5,
        "median": 15,
        "q3": 22.5,
        "max": 30,
    }
    assert quantiles([100, True, float("nan"), float("inf"), None])["n"] == 1
    assert quantiles([])["median"] is None


def test_catalog_id_minimum_is_counted_once_and_missing_values_stay_missing(tmp_path):
    path = tmp_path / "nl.sqlite3"
    config = database(path, count=0)
    db = Database(path)
    for source, price in [("autoscout24", 12000), ("marktplaats", 11000)]:
        run = db.start_run("fixture")
        car = Listing(
            source,
            "same",
            "https://example.test/same",
            "Toyota Corolla",
            price=price,
            kenteken="AB123C",
            raw={"location": {"countryCode": "NL"}},
        )
        db.observe(run, qualify(car, config["scope"]), now())
        db.finish_run(run, "complete", {})
    db.conn.close()
    market = analyze_market(path, "NL", as_of=now())
    assert market["live_target_publications"] == 2
    assert market["live_ids"] == market["priced_ids"] == 1
    assert market["price_stats"]["median"] == 11000
    assert market["unknown_year_ids"] == market["unknown_mileage_ids"] == 1
    assert market["gear_counts"] == {"unknown": 1}


def test_stale_prices_do_not_become_fresh_when_search_is_refreshed(tmp_path):
    path = tmp_path / "nl.sqlite3"
    config = database(path, count=0)
    db = Database(path)
    old = (datetime.now(UTC) - timedelta(days=4)).isoformat()
    for price, stamp in [(10000, old), (None, now())]:
        run = db.start_run("fixture")
        car = Listing(
            "autoscout24",
            "same",
            "https://example.test/same",
            "Toyota Corolla",
            price=price,
            raw={"location": {"countryCode": "NL"}},
        )
        db.observe(run, qualify(car, config["scope"]), stamp)
        db.finish_run(run, "complete", {})
    db.conn.close()
    market = analyze_market(path, "NL", as_of=now())
    assert market["live_ids"] == 1
    assert market["priced_ids"] == 0
    assert market["unpriced_ids"] == 1


def test_current_sold_peer_holds_other_quote_until_newer_explicit_available(tmp_path):
    path = tmp_path / "nl.sqlite3"
    config = database(path, count=0)
    db = Database(path)
    earlier = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    for source, state, stamp in [
        ("autoscout24", "available", earlier),
        ("marktplaats", "sold", now()),
    ]:
        run = db.start_run("fixture")
        car = Listing(
            source,
            "same",
            "https://example.test/same",
            "Toyota Corolla",
            price=10000,
            kenteken="AB123C",
            raw={"location": {"countryCode": "NL"}, "availability_status": state},
        )
        db.observe(run, qualify(car, config["scope"]), stamp)
        db.finish_run(run, "complete", {})
    db.conn.close()
    market = analyze_market(path, "NL", as_of=now())
    assert market["priced_ids"] == 0
    assert market["known_identifier_ids"] == 1
    assert market["provisional_ids"] == 0


def test_matched_groups_use_equal_dimensions_and_support_not_discount():
    markets = {}
    for country, delta in [("NL", 0), ("DE", -1000)]:
        cars = []
        for year, n, price in [(2020, 8, 15000), (2022, 5, 30000)]:
            cars.extend(
                {
                    "year": year,
                    "transmission": "automatic",
                    "mileage_km": 60000,
                    "price_eur": price + delta * (5 if year == 2022 else 1),
                    "damage_declared": False,
                }
                for _ in range(n)
            )
        cars.append(
            {
                "year": 2020,
                "transmission": "manual",
                "mileage_km": 60000,
                "price_eur": 1,
                "damage_declared": True,
            }
        )
        markets[country] = {"cars": cars}
    groups = comparable_groups(markets)
    assert len(groups) == 2
    assert groups[0]["year"] == 2020
    assert groups[0]["weight"] == 8
    assert groups[0]["delta_eur"] == -1000
    assert groups[0]["delta_pct"] == pytest.approx(-1000 / 15000 * 100)


def test_declared_damage_distinguishes_negation_and_typed_current_damage():
    assert damaged({"title": "Motorschaden"})
    assert not damaged({"title": "Kein Motorschaden"})
    assert not damaged({"title": "Unfallfrei"})
    assert damaged({"raw": {"vehicle": {"isCurrentlyDamaged": True}}})


def test_two_country_report_is_english_reproducible_and_readonly(tmp_path):
    nl, de = tmp_path / "nl.sqlite3", tmp_path / "de.sqlite3"
    database(nl)
    database(de, "DE")
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in [nl, de]}
    result = generate(nl, secondary=de, output=tmp_path / "reports")
    folder = Path(result["directory"])
    analysis = json.loads((folder / "analysis.json").read_text())
    assert analysis["markets"]["NL"]["price_stats"]["median"] == 12000
    assert analysis["markets"]["DE"]["price_stats"]["median"] == 14000
    assert analysis["matched_cells"][0]["delta_eur"] == 2000
    assert len(PdfReader(folder / "comparison-editorial-details.pdf").pages) == 8
    text = " ".join(
        page.extract_text()
        for page in PdfReader(folder / "comparison-editorial-details.pdf").pages
    )
    assert "Prices by year" in text and "Netherlands" in text and "Germany" in text
    assert not any("\u0400" <= char <= "\u04ff" for char in text)
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes} == hashes
    manifest = json.loads((folder / "manifest.json").read_text())
    for name, sha in manifest["files"].items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == sha


@pytest.mark.parametrize("count,unknown", [(0, False), (1, True)])
def test_single_market_empty_and_unknown_data_are_renderable(tmp_path, count, unknown):
    path = tmp_path / "nl.sqlite3"
    database(path, count=count, all_unknown=unknown)
    result = generate(path, output=tmp_path / "reports")
    folder = Path(result["directory"])
    assert len(PdfReader(folder / "comparison-editorial-details.pdf").pages) == 7
    text = PdfReader(folder / "comparison-editorial.pdf").pages[0].extract_text()
    assert "No comparable cohort" in text
    assert (folder / "comparison-editorial.png").is_file()


def test_wrong_model_and_future_clock_fail_without_publishing(tmp_path):
    nl, de = tmp_path / "nl.sqlite3", tmp_path / "de.sqlite3"
    database(nl)
    database(de, "DE", model="Yaris")
    with pytest.raises(ValueError, match="different vehicle selectors"):
        generate(nl, secondary=de, output=tmp_path / "reports")
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="non-future"):
        generate(nl, output=tmp_path / "reports", as_of=future)
    assert list((tmp_path / "reports").iterdir()) == []


def test_renderer_failure_preserves_previous_report(tmp_path, monkeypatch):
    from car_market import editorial_report

    path = tmp_path / "nl.sqlite3"
    database(path, count=0)
    previous = tmp_path / "reports/previous"
    previous.mkdir(parents=True)
    (previous / "manifest.json").write_text('{"accepted":true}')

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(editorial_report, "render", fail)
    with pytest.raises(OSError):
        generate(path, output=tmp_path / "reports")
    assert json.loads((previous / "manifest.json").read_text())["accepted"]
    assert list((tmp_path / "reports").iterdir()) == [previous]


def test_large_snapshot_hashing_never_materializes_the_database(tmp_path, monkeypatch):
    from car_market.analytics import file_sha256

    path = tmp_path / "snapshot.sqlite3"
    path.write_bytes(b"snapshot proof")

    def reject_bulk_read(*_args):
        raise AssertionError("A growing database must be hashed in bounded memory")

    monkeypatch.setattr(Path, "read_bytes", reject_bulk_read)
    assert file_sha256(path) == hashlib.sha256(b"snapshot proof").hexdigest()
