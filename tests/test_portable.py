"""Portable contracts: arbitrary models, retained native payloads, failures and restarts."""

import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from car_market.configuration import bind, preflight, validate
from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database, now
from car_market.fetch import Fetcher, Response
from car_market.infra_support import FileLock
from car_market.parse import Listing, fuel
from car_market.portable import atomic_json, collect, main
from car_market.search_profile import GENERIC_PROFILE, qualify, search_context

ROOT = Path(__file__).parents[1]
NATIVE = ROOT / "tests/fixtures/mercedes_autoscout"


def config(country="NL", make="Toyota", model="Corolla"):
    source = "autoscout24_de" if country == "DE" else "autoscout24"
    return validate(
        {
            "schema_version": 1,
            "scope": {
                "country": country,
                "model_profile": GENERIC_PROFILE,
                "selector": {"make": make, "model_terms": [[model]]},
            },
            "queries": [
                {
                    "source": source,
                    "key": "all",
                    "url": f"https://www.autoscout24.{country.lower()}/lst/toyota?cy={'D' if country == 'DE' else 'NL'}",
                    "pagination": "mercedes_autoscout_next",
                    "coverage": "enumerate",
                    "infer_missing": False,
                }
            ],
            "interval_hours": 6,
            "fast_interval_minutes": 1,
            "workers": 1,
            "detail_limit": 1,
            "max_pages": 2,
        }
    )


def item(title="Toyota Corolla", country="NL", **extra):
    return Listing(
        "autoscout24" if country == "NL" else "autoscout24_de",
        "test",
        "https://example.test/ad",
        title,
        raw={"location": {"countryCode": country}},
        **extra,
    )


@pytest.mark.parametrize(
    "make,model,title",
    [
        ("Toyota", "Corolla", "Toyota Corolla Touring Sports"),
        ("Mazda", "MX-5", "Mazda MX 5 2.0"),
        ("Tesla", "Model Y", "Tesla Model Y Long Range"),
        ("Škoda", "Octavia", "Skoda Octavia"),
        ("MINI", "Cooper S", "MINI Cooper S R53"),
        ("BMW", "218i", "BMW 218i Active Tourer"),
        ("Mercedes-Benz", "C180", "Mercedes Benz C180 Coupe"),
        ("Dacia", "Duster", "Dacia Duster"),
        ("Peugeot", "208", "Peugeot 208"),
    ],
)
def test_arbitrary_models(make, model, title):
    assert (
        qualify(item(title), config(make=make, model=model)["scope"]).scope == "target"
    )


def test_alias_groups_and_exclusions():
    c = config(make="BMW", model="218i")
    c["scope"]["selector"].update(
        model_terms=[["218i", "218 i"], ["Active Tourer", "ActiveTourer"]],
        exclude_terms=["Gran Tourer", "218d"],
    )
    assert qualify(item("BMW 218 i ActiveTourer"), c["scope"]).scope == "target"
    assert qualify(item("BMW 218i Gran Tourer"), c["scope"]).scope == "excluded"
    assert qualify(item("BMW 218i Coupe"), c["scope"]).scope == "needs_confirmation"


@pytest.mark.parametrize(
    "title", ["Toyota Corolla Cross", "Toyota Corollas", "Toyota Yaris"]
)
def test_model_token_boundaries_and_excluded_variant(title):
    c = config()
    c["scope"]["selector"]["exclude_terms"] = ["Cross"]
    assert qualify(item(title), c["scope"]).scope != "target"


def test_description_cannot_substitute_own_model_or_generation():
    c = config()
    candidate = item("Toyota Yaris", description="Also selling Toyota Corolla E210")
    assert qualify(candidate, c["scope"]).scope != "target"
    c["scope"]["selector"]["generation_terms"] = ["E210"]
    candidate = item(description="Toyota Corolla E210")
    assert qualify(candidate, c["scope"]).scope == "needs_confirmation"


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda x: x.raw.clear(), "needs_confirmation"),
        (lambda x: x.raw.update(model_brand="Honda"), "excluded"),
        (lambda x: x.raw.update(country_code="DE"), "needs_confirmation"),
        (lambda x: x.raw.update(location={"countryCode": "DE"}), "excluded"),
    ],
)
def test_unknown_conflicting_and_foreign_country(mutation, expected):
    candidate = item()
    mutation(candidate)
    assert qualify(candidate, config()["scope"]).scope == expected


def test_required_fields_remain_unknown_until_proven():
    c = config()
    c["scope"]["selector"].update(
        fuels=["electric"], transmissions=["automatic"], year_from=2020
    )
    candidate = item()
    assert qualify(candidate, c["scope"]).scope == "needs_confirmation"
    candidate.fuel, candidate.transmission, candidate.year = (
        "electric",
        "automatic",
        2021,
    )
    assert qualify(candidate, c["scope"]).scope == "target"
    candidate.fuel = "petrol"
    assert qualify(candidate, c["scope"]).scope == "excluded"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Electric", "electric"),
        ("Elektro", "electric"),
        ("LPG", "lpg"),
        ("Hydrogen", "hydrogen"),
        ("Elektrische ramen", None),
    ],
)
def test_fuels_beyond_petrol(value, expected):
    assert fuel(value) == expected


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.update(workers=0),
        lambda c: c.update(request_delay=-1),
        lambda c: c.update(interval_hours=float("nan")),
        lambda c: c["scope"]["selector"].update(model_terms=[]),
        lambda c: c["scope"]["selector"].update(fule="diesel"),
        lambda c: c["queries"][0].update(url="https://autoscout24.nl.evil.test/lst"),
        lambda c: c["queries"][0].update(
            url="https://user:password@www.autoscout24.nl/lst"
        ),
        lambda c: c["queries"][0].update(url="file:///etc/passwd"),
        lambda c: c["queries"][0].update(infer_missing=True),
        lambda c: c["queries"].append(copy.deepcopy(c["queries"][0])),
        lambda c: c["scope"].update(country="US"),
    ],
)
def test_invalid_config_fails_before_database_write(mutation):
    c = config()
    mutation(c)
    with pytest.raises(ValueError):
        validate(c)


def test_bound_search_is_immutable_and_read_checks_do_not_mutate(tmp_path):
    c = config()
    db = Database(tmp_path / "market.sqlite3")
    bind(db, c)
    run = db.start_run("test")
    db.observe(run, qualify(item(price=18000), c["scope"]), now())
    db.finish_run(run, "complete", {})
    db.conn.close()
    before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    wrong = config(model="Yaris")
    with pytest.raises(ValueError, match="another search"):
        preflight(db.path, wrong)
    assert hashlib.sha256(db.path.read_bytes()).hexdigest() == before
    preflight(db.path, c)


def test_cannot_relabel_legacy_populated_database(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run("test")
    db.observe(
        run,
        Listing(
            "test", "old", "url", "BMW 218i Active Tourer", fuel="petrol"
        ).qualify(),
        now(),
    )
    db.conn.close()
    with pytest.raises(ValueError):
        preflight(db.path, config())


def test_context_does_not_leak_to_legacy_parsers():
    with search_context(config()["scope"]):
        assert item().qualify().scope == "target"
    assert item().qualify().scope == "excluded"


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_real_retained_native_search_is_model_independent(tmp_path, country):
    c = config(country=country, make="Mercedes-Benz", model="C180")
    c["scope"]["selector"]["model_terms"] = [["C180", "C 180"]]
    db = Database(tmp_path / "market.sqlite3")
    try:
        collector = ConfiguredCollector(db, c)
        entries = json.loads((NATIVE / "search-manifest.json").read_text())
        page_case = next(e for e in entries if e["country"] == country)
        body = (NATIVE / page_case["file"]).read_text()
        page = collector._parse(c["queries"][0]["source"], body, page_case["url"])
        assert page.total > 0
        assert page.listings
        assert any(i.scope == "target" for i in page.listings)
        assert all(i.raw["model_profile"] == GENERIC_PROFILE for i in page.listings)
        assert (
            all(i.kenteken is None for i in page.listings) if country == "DE" else True
        )
    finally:
        db.conn.close()


def test_generic_database_price_history_and_strong_identity(tmp_path):
    c = config()
    db = Database(tmp_path / "market.sqlite3")
    bind(db, c)
    try:
        vehicle_ids = []
        for index, price in enumerate([18000, 18000, 17000]):
            run = db.start_run("test")
            candidate = item(
                price=price,
                kenteken="AB123C",
                transmission="automatic",
                fuel="hybrid",
                year=2021,
            )
            candidate.external_id = str(index % 2)
            listing_id = db.observe(run, qualify(candidate, c["scope"]), now())
            vehicle_ids.append(
                db.conn.execute(
                    "SELECT vehicle_id FROM listings WHERE id=?", (listing_id,)
                ).fetchone()[0]
            )
            db.finish_run(run, "complete", {})
        assert len(set(vehicle_ids)) == 1
        assert db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2
        assert (
            db.conn.execute(
                "SELECT COUNT(DISTINCT price_eur) FROM observations"
            ).fetchone()[0]
            == 2
        )
        assert (
            db.conn.execute("SELECT COUNT(*) FROM purchase_candidates").fetchone()[0]
            == 2
        )
        db.export(tmp_path / "exports")
        assert len(json.loads((tmp_path / "exports/vehicles.json").read_text())) == 1
    finally:
        db.conn.close()


def test_pending_country_does_not_invent_vehicle_and_keeps_proof(tmp_path):
    c = config()
    db = Database(tmp_path / "market.sqlite3")
    collector = ConfiguredCollector(db, c)
    try:
        run = db.start_run("test")
        candidate = item()
        candidate.raw.clear()
        assert collector._observe(run, candidate, now()) is None
        assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0
        assert (
            db.conn.execute(
                "SELECT COUNT(*) FROM monitor_state WHERE key LIKE 'pending:%'"
            ).fetchone()[0]
            == 1
        )
        candidate.raw["location"] = {"countryCode": "NL"}
        assert collector._observe(run, candidate, now()) is not None
        assert (
            db.conn.execute(
                "SELECT COUNT(*) FROM monitor_state WHERE key LIKE 'pending:%'"
            ).fetchone()[0]
            == 0
        )
    finally:
        db.conn.close()


def test_busy_writer_leaves_database_untouched(tmp_path):
    args = SimpleNamespace(browser=False, sources=None, no_details=True)
    path = tmp_path / "data/market.sqlite3"
    with FileLock(Path(str(path) + ".lock"), blocking=False):
        with pytest.raises(BlockingIOError):
            collect(args, tmp_path, config(), path)
    assert not path.exists()


def test_cross_process_lock_and_owner_survives_failed_acquisition(tmp_path):
    path = tmp_path / "writer.lock"
    with FileLock(path, blocking=False):
        before = path.read_text()
        code = "from pathlib import Path; from car_market.infra_support import FileLock; FileLock(Path(__import__('sys').argv[1]), blocking=False).acquire()"
        child = subprocess.run(
            [sys.executable, "-c", code, str(path)], capture_output=True, timeout=10
        )
        assert child.returncode != 0
        assert path.read_text() == before
    with FileLock(path, blocking=False):
        pass


def test_sigterm_cancels_fetch_without_network():
    stopped = threading.Event()
    stopped.set()
    fetcher = Fetcher()
    fetcher.stopped = stopped
    with pytest.raises(KeyboardInterrupt):
        fetcher.get("https://www.autoscout24.nl/lst/toyota")


def test_source_failure_and_restart_backoff_keep_history(tmp_path, monkeypatch):
    c = config()
    db = Database(tmp_path / "market.sqlite3")
    collector = ConfiguredCollector(db, c)
    query = c["queries"][0]
    run = db.start_run("test")
    db.save_fetch(
        run,
        query["source"],
        query["url"],
        query["url"],
        429,
        None,
        now(),
        "http",
        "http_429",
    )
    collector.fetcher.get = lambda *_a, **_kw: pytest.fail(
        "Rate-limited source must not refetch"
    )
    result = collector.run(details=False, checks=False)
    assert result["status"] == "partial"
    assert (
        db.conn.execute("SELECT status FROM source_health").fetchone()[0] == "backoff"
    )
    db.conn.close()


def test_operator_init_readonly_status_backup_and_wrong_profile(tmp_path, capsys):
    workspace = tmp_path / "search"
    base = ["--workspace", str(workspace)]
    assert main(base + ["init", "--make", "Toyota", "--model", "Corolla"]) == 0
    assert main(base + ["init", "--make", "Honda", "--model", "Civic"]) == 2
    assert main(base + ["validate"]) == 0
    assert main(base + ["status"]) == 0
    assert not (workspace / "data/market.sqlite3").exists()
    c = json.loads((workspace / "search.json").read_text())
    db = Database(workspace / "data/market.sqlite3")
    bind(db, c)
    run = db.start_run("test")
    db.observe(run, qualify(item(price=18000), c["scope"]), now())
    db.finish_run(run, "complete", {})
    db.conn.close()
    backup = tmp_path / "backup.sqlite3"
    assert main(base + ["backup", "--output", str(backup)]) == 0
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    assert main(base + ["backup", "--output", str(backup)]) == 2
    assert main(base + ["list", "--max-price", "20000"]) == 0
    assert main(base + ["collect", "--sources", "typo"]) == 2
    assert main(base + ["doctor", "--require-daemon"]) == 5
    capsys.readouterr()


def test_atomic_publish_preserves_previous_file_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    atomic_json(path, {"state": "good"})
    import car_market.portable as portable

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(portable.os, "replace", fail)
    with pytest.raises(OSError):
        atomic_json(path, {"state": "bad"})
    assert json.loads(path.read_text()) == {"state": "good"}
    assert not list(tmp_path.glob("*.tmp-*"))


def test_compact_model_numbers_and_suffixes():
    c = config(make="Mercedes-Benz", model="C180")
    assert qualify(item("Mercedes Benz C 180"), c["scope"]).scope == "target"
    assert qualify(item("Mercedes Benz C 1800"), c["scope"]).scope != "target"
    c = config(make="BMW", model="218i")
    assert qualify(item("BMW 218 i Active Tourer"), c["scope"]).scope == "target"
    assert qualify(item("BMW 218 id Active Tourer"), c["scope"]).scope != "target"


@pytest.mark.parametrize("value,expected", [("Benzin/Elektro", "hybrid"), ("Benzine/LPG", "lpg")])
def test_native_bifuel_labels(value, expected):
    assert fuel(value) == expected


@pytest.mark.parametrize(
    "outcome", ["resolved", "blocked", "wrong_identity", "unknown"]
)
def test_discovery_details_are_bounded_and_require_own_identity(
    tmp_path, outcome, monkeypatch
):
    from car_market.parse import Page

    c = config()
    db = Database(tmp_path / "market.sqlite3")
    collector = ConfiguredCollector(db, c)
    run = db.start_run("test")
    original = item()
    original.raw.clear()
    collector._observe(run, original, now())
    returned = item(price=18000)
    if outcome == "wrong_identity":
        returned.external_id = "recommendation"
    if outcome == "unknown":
        returned.raw.clear()
    calls = []

    def get(url, **_kw):
        calls.append(url)
        return Response(
            url,
            url,
            403 if outcome == "blocked" else 200,
            None if outcome == "blocked" else "own payload",
            now(),
            "http_403" if outcome == "blocked" else None,
        )

    collector.fetcher.get = get
    monkeypatch.setattr(collector, "_parse", lambda *_a: Page([returned]))
    monkeypatch.setattr(collector, "_detail_matches", lambda *_a: True)
    result = collector._pending_details()
    assert result["attempted"] == 1
    vehicles = db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
    assert vehicles == (1 if outcome == "resolved" else 0)
    if outcome != "resolved":
        # A fresh search must retain the previous attempt clock rather than defeating backoff.
        collector._observe(run, original, now())
        second = collector._pending_details()
        assert second["attempted"] == 0
        assert len(calls) == 1
    db.conn.close()


def test_lock_metadata_error_releases_operating_system_lock(tmp_path, monkeypatch):
    path = tmp_path / "writer.lock"
    lock = FileLock(path, blocking=False)

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(lock, "refresh_metadata", fail)
    with pytest.raises(OSError):
        lock.acquire()
    assert lock.file is None
    with FileLock(path, blocking=False):
        pass


def test_rate_limit_on_later_page_backs_off_the_source(tmp_path):
    c = config()
    db = Database(tmp_path / "market.sqlite3")
    collector = ConfiguredCollector(db, c)
    run = db.start_run("test")
    query = c["queries"][0]
    db.save_fetch(
        run,
        query["source"],
        query["url"] + "&page=2",
        query["url"] + "&page=2",
        429,
        None,
        now(),
        "http",
        "http_429",
    )
    collector.fetcher.get = lambda *_a, **_kw: pytest.fail(
        "Blocked pagination must back off the source"
    )
    result = collector.run(details=False, checks=False)
    assert result["status"] == "partial"
    assert (
        db.conn.execute("SELECT status FROM source_health").fetchone()[0] == "backoff"
    )
    db.conn.close()
