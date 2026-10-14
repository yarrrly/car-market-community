"""Full export failures retain one generation without changing stored evidence."""

import errno
import gzip
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from car_market import cli, configured_collect, opportunities, portable
from car_market.configuration import bind, validate
from car_market.db import Database, publish_export_files
from car_market.mercedes_autoscout import parse_mercedes_autoscout
from car_market.model_profiles import BMW_PROFILE, MERCEDES_PROFILE, bind_profile
from car_market.parse import Listing
from car_market.publication import KEY as PUBLICATION_KEY
from car_market.search_profile import search_context

CAPTURES = Path(__file__).parent / "fixtures/autoscout_query_binding"
NAMES = ("first.csv", "second.csv", "last.sqlite3")


def collection_evidence_dump(conn):
    # Publication diagnostics intentionally change after collection finishes.
    # Every other table and monitor key remains in the exact dump comparison.
    prefix = f'INSERT INTO "monitor_state" VALUES({PUBLICATION_KEY!r},'
    return [line for line in conn.iterdump() if not line.startswith(prefix)]


def snapshot(root):
    return {
        p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_ino)
        for p in root.iterdir()
        if p.is_file()
    }


def prepared(tmp_path, absent=()):
    target, stage = tmp_path / "exports", tmp_path / "stage"
    target.mkdir()
    stage.mkdir()
    for name in NAMES:
        (stage / name).write_bytes(b"new " + name.encode())
        if name not in absent:
            (target / name).write_bytes(b"old " + name.encode())
    (target / "unrelated.txt").write_bytes(b"preserved")
    return target, stage


@pytest.mark.parametrize("failure_name", NAMES)
def test_helper_restores_first_second_and_last_replacements(
    tmp_path, monkeypatch, failure_name
):
    target, stage = prepared(tmp_path)
    previous = snapshot(target)
    replace = Path.replace

    def fail(path, destination):
        if path == stage / failure_name:
            raise OSError(errno.ENOSPC, "controlled publication full")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="controlled publication full"):
        publish_export_files(stage, target, iter(NAMES))
    assert snapshot(target) == previous
    assert sorted(p.name for p in target.iterdir()) == sorted(previous)
    assert all((target / name).stat().st_nlink == 1 for name in NAMES)


@pytest.mark.parametrize("failure_name", NAMES[1:])
def test_helper_removes_only_newly_created_outputs(tmp_path, monkeypatch, failure_name):
    target, stage = prepared(tmp_path, absent=(NAMES[0],))
    previous = snapshot(target)
    replace = Path.replace

    def fail(path, destination):
        if path == stage / failure_name:
            raise OSError(errno.ENOSPC, "controlled publication full")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        publish_export_files(stage, target, NAMES)
    assert snapshot(target) == previous
    assert sorted(p.name for p in target.iterdir()) == sorted(previous)


@pytest.mark.parametrize("failure_name", (NAMES[0], NAMES[-1]))
def test_recovery_preparation_failure_does_not_publish(
    tmp_path, monkeypatch, failure_name
):
    target, stage = prepared(tmp_path)
    previous = snapshot(target)
    link = os.link

    def fail(source, destination, **kwargs):
        if Path(source).name == failure_name:
            raise OSError(errno.ENOSPC, "controlled recovery full")
        return link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(OSError, match="controlled recovery full"):
        publish_export_files(stage, target, NAMES)
    assert snapshot(target) == previous
    assert all((stage / name).exists() for name in NAMES)
    assert not list(target.glob(".market-export*"))


@pytest.mark.parametrize("restore_failure", ("replace", "unlink"))
def test_failed_rollback_retains_originals_and_both_errors(
    tmp_path, monkeypatch, restore_failure
):
    absent = (NAMES[0],) if restore_failure == "unlink" else ()
    target, stage = prepared(tmp_path, absent=absent)
    previous = snapshot(target)
    replace, unlink = Path.replace, Path.unlink

    def fail_replace(path, destination):
        if path == stage / NAMES[1]:
            raise OSError(errno.ENOSPC, "controlled publication full")
        if restore_failure == "replace" and path.parent.name.startswith(
            ".market-export-recovery-"
        ):
            raise OSError(errno.EACCES, "controlled restore denied")
        return replace(path, destination)

    def fail_unlink(path, *args, **kwargs):
        if path == target / NAMES[0]:
            raise OSError(errno.EACCES, "controlled unlink denied")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", fail_replace)
    if restore_failure == "unlink":
        monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(BaseExceptionGroup, match="recovery files retained") as raised:
        publish_export_files(stage, target, NAMES)
    assert [error.errno for error in raised.value.exceptions] == [
        errno.ENOSPC,
        errno.EACCES,
    ]
    recovery = list(target.glob(".market-export-recovery-*"))
    assert len(recovery) == 1
    assert recovery[0].stat().st_mode & 0o777 == 0o700
    for name in NAMES:
        if name not in absent:
            assert snapshot(recovery[0])[name] == previous[name]
    assert snapshot(target)["unrelated.txt"] == previous["unrelated.txt"]
    assert snapshot(target)[NAMES[1]] == previous[NAMES[1]]


def test_success_cleans_shared_recovery_links(tmp_path):
    target, stage = prepared(tmp_path)
    expected = snapshot(stage)
    unrelated = snapshot(target)["unrelated.txt"]
    publish_export_files(stage, target, iter(NAMES))
    assert snapshot(target) == {**expected, "unrelated.txt": unrelated}
    assert not list(target.glob(".market-export*"))
    assert all((target / name).stat().st_nlink == 1 for name in NAMES)


def native_config(country, configured):
    row = next(
        row
        for row in json.loads((CAPTURES / "manifest.json").read_text())
        if row["case"] == country.lower() + "-bmw-218i"
    )
    query = {
        "source": "autoscout24" if country == "NL" else "autoscout24_de",
        "key": "retained",
        "url": row["url"],
        "pagination": "mercedes_autoscout_next",
        "infer_missing": False,
    }
    config = {
        "scope": {"country": country, "model_profile": BMW_PROFILE},
        "queries": [query],
        "interval_hours": 6,
    }
    if configured:
        config.update(schema_version=1, workers=1, max_pages=1, detail_limit=0)
        config["scope"].update(
            model_profile="configured_vehicle",
            selector={"make": "BMW", "model_terms": [["218i"], ["Active Tourer"]]},
        )
        validate(config)
    return config, row


@pytest.mark.parametrize("interface", ("legacy", "configured"))
@pytest.mark.parametrize("country", ("NL", "DE"))
@pytest.mark.parametrize(
    "failure_name",
    ("market-current.csv", "price-history.csv", "market.sqlite3", "restore-denied"),
)
def test_actual_collect_export_failure_preserves_evidence_and_manual_retry(
    tmp_path, monkeypatch, interface, country, failure_name
):
    configured = interface == "configured"
    config, native = native_config(country, configured)
    blob = (CAPTURES / native["file"]).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == native["fixture_sha256"]
    body = gzip.decompress(blob)
    assert hashlib.sha256(body).hexdigest() == native["decoded_sha256"]
    if configured:
        with search_context(config["scope"]):
            page = parse_mercedes_autoscout(body.decode(), native["url"], country)
        targets = {
            item.external_id: item
            for item in page.listings
            if item.scope == "target" and item.raw.get("market_country") == country
        }
        assert len(targets) >= 2
        items = list(targets.values())[:2]
    else:
        # Synthetic legacy admission avoids reinterpreting a generic native parser
        # as the BMW adapter. Native clocks/own fields are exercised above.
        items = [
            Listing(
                config["queries"][0]["source"],
                "fixture-" + str(index),
                "https://example.test/owned/" + str(index),
                "BMW 218i Active Tourer",
                price=7000 + index,
                year=2016,
                fuel="petrol",
                transmission="manual",
                mileage=100000,
                raw={
                    "location": {"countryCode": country},
                    "market_country": country,
                    "proof_role": "synthetic_legacy",
                },
            ).qualify()
            for index in (1, 2)
        ]
        body = b"<html>synthetic legacy storage control</html>"
    database = tmp_path / "data/market.sqlite3"
    target = tmp_path / "exports"
    original_before_export = []

    class FakeCollector:
        def __init__(self, db, cfg, browser=False, stopped=None):
            assert browser is False
            self.db = db
            bind(db, cfg) if configured else bind_profile(db, cfg)

        def run(self, sources=None, details=True, checks=True, fast=False):
            assert not fast
            run = self.db.start_run()
            item = items[run - 1]
            fetch = self.db.save_fetch(
                run,
                item.source,
                native["url"],
                native["final_url"],
                200,
                body.decode(),
                native["observed_at"],
                "offline_fixture",
            )
            self.db.observe(
                run, item, native["observed_at"], fetch, query_key="retained"
            )
            self.db.finish_source(
                run,
                item.source,
                "retained",
                native["observed_at"],
                "partial",
                None,
                1,
                int(item.scope == "target"),
                1,
                infer_missing=False,
                finished_at=native["observed_at"],
            )
            summary = self.db.summary()
            self.db.finish_run(run, "partial", summary)
            original_before_export.append(collection_evidence_dump(self.db.conn))
            return {"run_id": run, "status": "partial", **summary}

    monkeypatch.setattr(cli, "Collector", FakeCollector)
    monkeypatch.setattr(configured_collect, "ConfiguredCollector", FakeCollector)
    monkeypatch.setattr(opportunities, "publish", lambda *args, **kwargs: None)
    args = SimpleNamespace(
        db=str(database),
        export_dir=str(target),
        command="collect",
        sources=None,
        browser=False,
        fast=False,
        no_details=True,
        no_rdw=True,
    )

    def call():
        if configured:
            return portable.collect(args, tmp_path, config, database)
        return cli.collect_once(args, config)

    assert call()["run_id"] == 1
    previous = snapshot(target)
    assert len(previous) == 12
    last_path = database.parent / "last-run.json"
    previous_last = last_path.read_bytes()
    raw_before = snapshot(database.parent / "raw")
    replace = Path.replace
    denied_restore = failure_name == "restore-denied"
    publication_failure = "price-history.csv" if denied_restore else failure_name

    def fail(path, destination):
        if denied_restore and path.parent.name.startswith(".market-export-recovery-"):
            raise OSError(errno.EACCES, "controlled full export restore denied")
        if (
            path.parent.name.startswith(".market-export-")
            and not path.parent.name.startswith(".market-export-recovery-")
            and path.name == publication_failure
        ):
            raise OSError(errno.ENOSPC, "controlled full export ENOSPC")
        return replace(path, destination)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "replace", fail)
        expected = BaseExceptionGroup if denied_restore else OSError
        message = (
            "recovery files retained"
            if denied_restore
            else "controlled full export ENOSPC"
        )
        with pytest.raises(expected, match=message) as raised:
            call()
    retained = list(target.glob(".market-export-recovery-*"))
    if denied_restore:
        assert [error.errno for error in raised.value.exceptions] == [
            errno.ENOSPC,
            errno.EACCES,
        ]
        assert len(retained) == 1
        assert snapshot(retained[0]) == previous
        assert retained[0].stat().st_mode & 0o777 == 0o700
        assert (
            snapshot(target)["market-current.csv"][0]
            != previous["market-current.csv"][0]
        )
        assert all(
            snapshot(target)[name] == value
            for name, value in previous.items()
            if name != "market-current.csv"
        )
    else:
        assert snapshot(target) == previous
        assert not list(target.glob(".market-export*"))
    assert last_path.read_bytes() == previous_last
    assert snapshot(database.parent / "raw") == raw_before
    with sqlite3.connect(database) as conn:
        assert collection_evidence_dump(conn) == original_before_export[-1]
        publication = json.loads(
            conn.execute(
                "SELECT value FROM monitor_state WHERE key=?", (PUBLICATION_KEY,)
            ).fetchone()[0]
        )
        latest = publication["latest"]
        assert latest["run_id"] == 2
        assert latest["mode"] == "full"
        assert latest["collection_status"] == "partial"
        assert (
            latest["collection_finished_at"]
            == conn.execute("SELECT finished_at FROM runs WHERE id=2").fetchone()[0]
        )
        assert publication["last_full"] == latest
        assert latest["phases"]["export"]["status"] == "failed"
        assert latest["phases"]["export"]["error"] == {
            "type": "OSError",
            "errno": errno.ENOSPC,
        }
        assert latest["phases"]["last_run"]["status"] == "unresolved"
        assert latest["phases"]["signals"]["status"] == (
            "disabled" if configured else "success"
        )
        assert publication["last_export"]["run_id"] == 1
        assert publication["last_export"]["phases"]["export"]["status"] == "success"
        assert (
            json.loads((database.parent / "publication-attempt.json").read_text())
            == publication
        )
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
        assert conn.execute(
            "SELECT DISTINCT observed_at FROM observations"
        ).fetchall() == [(native["observed_at"],)]
        assert conn.execute(
            "SELECT DISTINCT fetched_at,content_hash FROM fetches"
        ).fetchall() == [(native["observed_at"], hashlib.sha256(body).hexdigest())]
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        post_failure_dump = list(conn.iterdump())
    db = Database(database)
    try:
        db.export(target)
        assert collection_evidence_dump(db.conn) == original_before_export[-1]
        assert list(db.conn.iterdump()) == post_failure_dump
    finally:
        db.conn.close()
    assert len(snapshot(target)) == 12
    if denied_restore:
        # A successful retry must not delete an earlier incomplete recovery.
        assert snapshot(retained[0]) == previous
    assert (
        snapshot(target)["market-current.csv"][0] != previous["market-current.csv"][0]
    )
    assert snapshot(database.parent / "raw") == raw_before
    with sqlite3.connect(target / "market.sqlite3") as exported:
        assert exported.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


@pytest.mark.parametrize("hardlink", (False, True))
def test_export_still_rejects_own_working_database(tmp_path, hardlink):
    db = Database(tmp_path / "working/market.sqlite3")
    try:
        target = db.path.parent
        if hardlink:
            target = tmp_path / "alias"
            target.mkdir()
            os.link(db.path, target / "market.sqlite3")
        before = list(db.conn.iterdump())
        with pytest.raises(ValueError, match="must be different files"):
            db.export(target)
        assert list(db.conn.iterdump()) == before
        assert not list(target.glob(".market-export*"))
    finally:
        db.conn.close()


@pytest.mark.parametrize("foreign", ("country", "profile"))
def test_export_still_rejects_foreign_bound_backup(tmp_path, foreign):
    db = Database(tmp_path / "working/market.sqlite3")
    bind_profile(db, {"scope": {"country": "NL", "model_profile": BMW_PROFILE}})
    target = tmp_path / "exports"
    other = Database(target / "market.sqlite3")
    bind_profile(
        other,
        {
            "scope": {
                "country": "DE" if foreign == "country" else "NL",
                "model_profile": MERCEDES_PROFILE
                if foreign == "profile"
                else BMW_PROFILE,
            }
        },
    )
    other.conn.close()
    previous = snapshot(target)
    before = list(db.conn.iterdump())
    try:
        with pytest.raises(ValueError, match="country|profile"):
            db.export(target)

        # A readonly WAL validation may update shared-memory reader metadata.
        # That is not a change to the retained database generation.
        def durable(values):
            return {
                name: value
                for name, value in values.items()
                if not name.endswith(("-wal", "-shm"))
            }

        assert durable(snapshot(target)) == durable(previous)
        assert list(db.conn.iterdump()) == before
        assert not list(target.glob(".market-export*"))
    finally:
        db.conn.close()
