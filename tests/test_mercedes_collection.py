import json
from pathlib import Path

import pytest
from car_market.db import Database
from car_market.fetch import Response
from car_market.mercedes_collect import MercedesCollector
from car_market.model_profiles import (
    MERCEDES_PROFILE,
    bind_profile,
    qualify_mercedes,
    validate_profile_path,
)
from car_market.parse import Listing


def config(country="NL"):
    return {
        "scope": {"country": country, "model_profile": MERCEDES_PROFILE},
        "queries": [],
        "workers": 1,
    }


def own():
    return Listing(
        "autoscout24",
        "own",
        "https://example.test/own",
        "Mercedes C180 Coupé",
        price=20000,
        mileage=90000,
        year=2018,
        transmission="automatic",
        fuel="petrol",
        kenteken="S029FF",
        raw={"market_country": "NL", "generation": "C205"},
    )


def test_rdw_verification_does_not_refresh_advertised_fields(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    collector = MercedesCollector(db, config())
    item = own()
    qualify_mercedes(item, "NL")
    db.observe(db.start_run("fixture"), item, "2026-10-07T10:00:00+00:00")
    checks_before = [tuple(r) for r in db.conn.execute("SELECT * FROM field_verifications")]
    observed_before = [tuple(r) for r in db.conn.execute("SELECT * FROM observations")]
    collector.fetcher.get = lambda url: Response(
        url,
        url,
        200,
        json.dumps(
            [
                {
                    "kenteken": "S029FF",
                    "merk": "MERCEDES-BENZ",
                    "handelsbenaming": "C180",
                    "type": "205",
                    "aantal_deuren": "2",
                }
            ]
        ),
        "2026-10-08T10:00:00+00:00",
    )
    collector.rdw(db.start_run("rdw"))
    assert db.conn.execute("SELECT scope FROM listings").fetchone()[0] == "target"
    assert [tuple(r) for r in db.conn.execute("SELECT * FROM observations")] == observed_before
    assert [tuple(r) for r in db.conn.execute("SELECT * FROM field_verifications")] == checks_before
    assert db.conn.execute("SELECT count(*) FROM vehicle_checks").fetchone()[0] == 1
    proof = db.conn.execute("SELECT * FROM model_scope_verifications").fetchone()
    assert proof["scope"] == "target" and proof["verified_at"] == "2026-10-08T10:00:00+00:00"
    assert json.loads(proof["evidence_json"])["rdw"]["fetch_id"] == proof["fetch_id"]
    from car_market.vehicle_catalog import get_vehicle

    vehicle_id = db.conn.execute("SELECT id FROM vehicles").fetchone()[0]
    snapshot = get_vehicle(db.conn, vehicle_id, {**config(), "as_of": "2026-10-07T12:00:00+00:00"})
    assert snapshot["model_scope_verifications"] == []
    current = get_vehicle(db.conn, vehicle_id, {**config(), "as_of": "2026-10-08T12:00:00+00:00"})
    assert len(current["model_scope_verifications"]) == 1
    db.conn.close()


def test_generation_survives_search_refresh_without_recursive_evidence(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    bind_profile(db, config())
    db.observe(db.start_run("fixture"), qualify_mercedes(own(), "NL"), "2026-10-07T10:00:00+00:00")
    for hour in [11, 12, 13]:
        item = own()
        item.raw.pop("generation")
        qualify_mercedes(item, "NL")
        db.observe(
            db.start_run("fixture"), item, f"2026-10-07T{hour}:00:00+00:00", query_key="search"
        )
        assert db.conn.execute("SELECT scope FROM listings").fetchone()[0] == "target"
    payload = json.loads(
        db.conn.execute("SELECT details_json FROM latest_observations").fetchone()[0]
    )
    assert (
        payload["raw"]["retained_generation_evidence"]["observed_at"] == "2026-10-07T10:00:00+00:00"
    )
    assert payload["raw"]["retained_generation_evidence"]["proof"]["kind"] == "own_generation"
    wrong = own()
    wrong.raw["generation"] = "C204"
    qualify_mercedes(wrong, "NL")
    db.observe(db.start_run("fixture"), wrong, "2026-10-07T14:00:00+00:00")
    assert db.conn.execute("SELECT scope FROM listings").fetchone()[0] == "excluded"
    db.conn.close()


def test_profile_reopen_preserves_automatic_purchase_view(tmp_path):
    path = tmp_path / "market.sqlite3"
    db = Database(path)
    bind_profile(db, config())
    db.observe(db.start_run("fixture"), qualify_mercedes(own(), "NL"), "2026-10-07T10:00:00+00:00")
    db.conn.close()
    db = Database(path)
    assert db.conn.execute("SELECT count(*) FROM purchase_candidates").fetchone()[0] == 1
    db.conn.close()


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_mercedes_reserved_country_directory_rejects_wrong_country_before_creation(country):
    path = Path.home() / "src/mercedes-c180" / country.lower() / "missing-preflight.sqlite3"
    with pytest.raises(ValueError, match="another country"):
        validate_profile_path(path, "DE" if country == "NL" else "NL", MERCEDES_PROFILE)
    assert not path.exists()


def test_same_ad_native_generation_overrules_search_query_and_conflicting_vin():
    car = own()
    car.vin = "WDD2043401F123456"
    qualify_mercedes(car, "NL")
    assert car.scope == "excluded"


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_real_autoscout_search_pagination_model_isolation(tmp_path, country):
    root = Path(__file__).parent / "fixtures/mercedes_autoscout"
    case = next(
        v for v in json.loads((root / "manifest.json").read_text()) if v["country"] == country
    )
    body = (root / case["file"]).read_text()
    native = json.loads(body)["props"]["pageProps"]["listingDetails"]
    db = Database(tmp_path / "market.sqlite3")
    collector = MercedesCollector(db, config(country))
    run = db.start_run("fixture")
    response = Response(
        native["webPage"], native["webPage"], 200, body, "2026-10-07T10:00:00+00:00"
    )
    collector.ingest_response(
        run, "autoscout24_de" if country == "DE" else "autoscout24", "own-detail", response
    )
    assert db.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 1
    assert db.conn.execute("SELECT scope FROM listings").fetchone()[0] == "target"
    assert (
        db.conn.execute("SELECT count(*) FROM fetches WHERE raw_path IS NOT NULL").fetchone()[0]
        == 1
    )
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.conn.close()


@pytest.mark.parametrize("body", ["Kombi", "T-Modell", "T-Model"])
def test_own_estate_body_vetoes_coupe_title_and_vin(body):
    car = own()
    car.vin = "WDD2053401F123456"
    car.raw["body_type"] = body
    assert qualify_mercedes(car, "NL").scope == "excluded"


def test_rdw_wrong_model_vetoes_native_generation():
    car = own()
    car.raw["rdw_model_evidence"] = {
        "kenteken": car.kenteken,
        "fetch_id": 1,
        "data": {
            "merk": "MERCEDES-BENZ",
            "handelsbenaming": "C200",
            "type": "205",
            "aantal_deuren": "2",
        },
    }
    assert qualify_mercedes(car, "NL").scope == "excluded"


def test_model_year_does_not_override_older_first_registration():
    car = own()
    car.raw["first_registration"] = "2015-12-31"
    assert qualify_mercedes(car, "NL").scope == "excluded"


def test_nl_availability_requires_newer_explicit_own_confirmation(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    collector = MercedesCollector(db, config())
    car = own()
    car.raw["availability_status"] = "sold"
    collector._observe(db.start_run("fixture"), car, "2026-10-07T12:00:00+00:00")
    car = own()
    car.raw["availability_status"] = "available"
    collector._observe(db.start_run("fixture"), car, "2026-10-07T11:00:00+00:00")
    assert (
        db.conn.execute("SELECT availability FROM listings").fetchone()[0] == "seller_marked_sold"
    )
    collector._observe(db.start_run("fixture"), own(), "2026-10-07T13:00:00+00:00", key="search")
    assert (
        db.conn.execute("SELECT availability FROM listings").fetchone()[0] == "seller_marked_sold"
    )
    collector._observe(db.start_run("fixture"), car, "2026-10-07T14:00:00+00:00")
    assert db.conn.execute("SELECT availability FROM listings").fetchone()[0] == "observed"
    db.conn.close()


@pytest.mark.parametrize(
    "registration", ["2026-12", "20261201T000000.000Z", "12/2026", "2026-12-01"]
)
def test_known_future_registration_precision_cannot_target(registration, monkeypatch):
    from datetime import UTC, datetime

    from car_market import model_profiles

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 8, tzinfo=UTC)

    monkeypatch.setattr(model_profiles, "datetime", Clock)
    car = own()
    car.year = 2026
    car.raw["first_registration"] = registration
    assert qualify_mercedes(car, "NL").scope == "needs_confirmation"
    assert "future_registration" in car.raw["model_scope_evidence"]["missing"]
    assert car.raw["first_registration"] == registration


@pytest.mark.parametrize("key", ["model_year", "construction_year"])
def test_display_year_is_not_first_registration_proof(key):
    car = own()
    car.raw["mercedes_nl_evidence"] = {key: "2018"}
    assert qualify_mercedes(car, "NL").scope == "needs_confirmation"


@pytest.mark.parametrize("kind", ["rdw", "retained"])
def test_future_model_evidence_clock_cannot_promote(kind):
    car = own()
    car.raw.pop("generation")
    if kind == "rdw":
        car.raw["rdw_model_evidence"] = {
            "kenteken": car.kenteken,
            "fetch_id": 1,
            "checked_at": "2099-01-01T00:00:00+00:00",
            "data": {
                "merk": "MERCEDES-BENZ",
                "handelsbenaming": "C180",
                "type": "205",
                "aantal_deuren": "2",
            },
        }
    else:
        car.raw["retained_generation_evidence"] = {
            "value": "C205",
            "source": car.source,
            "external_id": car.external_id,
            "observed_at": "2099-01-01T00:00:00+00:00",
            "fetch_id": 1,
        }
    assert qualify_mercedes(car, "NL").scope == "needs_confirmation"


def test_failed_parse_keeps_raw_capture_before_error(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    collector = MercedesCollector(db, config())
    run = db.start_run("fixture")

    def fail(*args):
        raise ValueError("parser failed")

    collector._parse = fail
    response = Response(
        "https://example.test/own",
        "https://example.test/own",
        200,
        "actual retained body",
        "2026-10-07T10:00:00+00:00",
    )
    with pytest.raises(ValueError, match="parser failed"):
        collector.ingest_response(run, "autoscout24", "own", response)
    assert (
        db.conn.execute("SELECT count(*) FROM fetches WHERE raw_path IS NOT NULL").fetchone()[0]
        == 1
    )
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 0
    db.conn.close()


def test_rdw_approval_type_204_does_not_conflict_with_native_c205():
    car = own()
    car.raw["rdw_model_evidence"] = {
        "kenteken": car.kenteken,
        "fetch_id": 1,
        "data": {
            "merk": "MERCEDES-BENZ",
            "handelsbenaming": "C-KLASSE",
            "type": "204",
            "aantal_deuren": "2",
        },
    }
    assert qualify_mercedes(car, "NL").scope == "target"
    car.raw.pop("generation")
    assert qualify_mercedes(car, "NL").scope == "needs_confirmation"


def test_known_rdw_older_registration_vetoes_later_advertised_year():
    car = own()
    car.raw["first_registration"] = "2018-01-01"
    car.raw["rdw_model_evidence"] = {
        "kenteken": car.kenteken,
        "data": {
            "merk": "MERCEDES-BENZ",
            "handelsbenaming": "C180",
            "datum_eerste_toelating": "20151231",
        },
        "checked_at": "2026-10-07T12:00:00+00:00",
    }
    assert qualify_mercedes(car, "NL").scope == "excluded"
    assert car.raw["first_registration"] == "2018-01-01"


def test_autohero_inactive_native_marker_survives_collector_qualification(tmp_path):
    from car_market.mercedes_autohero_nl import parse_mercedes_autohero_nl

    root = Path(__file__).parent / "fixtures/mercedes_autohero_nl"
    doc = json.loads((root / "c180-detail.json").read_text())
    native = next(iter(doc["ROOT_QUERY"].values()))
    native["published"] = False
    own_url = "https://www.autohero.com/nl/" + native["carUrlTitle"] + "/id/" + native["adId"] + "/"
    db = Database(tmp_path / "market.sqlite3")
    collector = MercedesCollector(db, config())
    page = collector._parse("autohero", json.dumps(doc), own_url)
    assert len(page.listings) == 1 and page.listings[0].scope == "excluded"
    assert page.listings[0].raw["own_availability_exclusion"] is True
    assert (
        collector._observe(db.start_run("fixture"), page.listings[0], "2026-10-07T10:00:00+00:00")
        is None
    )
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 0
    assert parse_mercedes_autohero_nl(json.dumps(doc), own_url).listings[0].scope == "excluded"
    db.conn.close()
