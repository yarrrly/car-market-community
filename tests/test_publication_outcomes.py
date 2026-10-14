"""Publication failures do not rewrite collection facts or imply fresh exports."""

import errno
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from car_market import cli, opportunities, publication
from car_market import db as db_module
from car_market.db import Database
from car_market.model_profiles import bind_profile
from car_market.parse import Listing, parse_page

portable = None
if Path(cli.__file__).with_name("portable.py").is_file():
    from car_market import configuration, configured_collect, portable

ROOT = Path(__file__).parent / "fixtures/quality_primary"
NAME = "autoscout24-789ada1c-ded9-465d-93ff-f0783b445b2e.html"
NATIVE = json.loads((ROOT / "provenance.json").read_text())[NAME]
SYNTHETIC_CLOCK = "2026-10-11T12:00:00+00:00"


def files(root):
    return (
        {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.iterdir()
            if p.is_file()
        }
        if root.exists()
        else {}
    )


def facts(conn):
    tables = (
        "runs",
        "source_runs",
        "fetches",
        "listings",
        "observations",
        "events",
        "query_membership",
        "field_verifications",
        "vehicles",
        "vehicle_aliases",
    )
    data = {
        name: [
            tuple(row)
            for row in conn.execute("SELECT * FROM " + name + " ORDER BY rowid")
        ]
        for name in tables
    }
    data["other_monitor_state"] = [
        tuple(row)
        for row in conn.execute(
            "SELECT * FROM monitor_state WHERE key<>? ORDER BY key", (publication.KEY,)
        )
    ]
    return data


def setup(tmp_path, monkeypatch, interface, collection_status="complete"):
    if interface == "configured" and portable is None:
        pytest.skip("The canonical ABI has no portable collector")
    generic = interface == "configured"
    config = {
        "scope": {"country": "NL", "model_profile": "bmw_218i_active_tourer"},
        "queries": [
            {
                "source": "autoscout24",
                "key": "fixture",
                "url": "https://www.autoscout24.nl/lst/bmw?cy=NL",
                "pagination": "mercedes_autoscout_next",
                "infer_missing": False,
            }
        ],
        "interval_hours": 6,
        "max_pages": 1,
    }
    if generic:
        config.update(schema_version=1, workers=1, detail_limit=0)
        config["scope"].update(
            model_profile="configured_vehicle",
            selector={"make": "BMW", "model_terms": [["218i"], ["Active Tourer"]]},
        )
        configuration.validate(config)
        (tmp_path / "search.json").write_text(json.dumps(config))
    db_path = tmp_path / "data/market.sqlite3"
    expected = []

    class FakeCollector:
        def __init__(self, db, cfg, browser=False, stopped=None):
            assert browser is False
            self.db = db
            configuration.bind(db, cfg) if generic else bind_profile(db, cfg)
            if not db.conn.execute("SELECT 1 FROM observations LIMIT 1").fetchone():
                # Seed existing retained native evidence, not a new live capture.
                body = (ROOT / NAME).read_text()
                run = db.start_run("fixture_native", timestamp=NATIVE["time"])
                fetch = db.save_fetch(
                    run,
                    "autoscout24",
                    NATIVE["url"],
                    NATIVE["final_url"],
                    200,
                    body,
                    NATIVE["time"],
                    "retained_native_projection",
                )
                item = parse_page("autoscout24", body, NATIVE["url"]).listings[0]
                if generic:
                    from car_market.search_profile import qualify

                    qualify(item, cfg["scope"])
                db.observe(run, item, NATIVE["time"], fetch)
                db.finish_run(
                    run, "imported", {"fixture": "retained_native_projection"}
                )

        def run(self, sources=None, details=True, checks=True, fast=False):
            run = self.db.start_run("fast" if fast else "live")
            body = "synthetic publication scenario " + str(run)
            fetch = self.db.save_fetch(
                run,
                "autoscout24",
                "https://example.test/owned",
                "https://example.test/owned",
                200,
                body,
                SYNTHETIC_CLOCK,
                "offline_fake",
            )
            item = Listing(
                "autoscout24",
                "synthetic",
                "https://example.test/owned",
                "BMW 218i Active Tourer",
                price=7000 + run,
                year=2016,
                mileage=100000,
                fuel="petrol",
                transmission="manual",
                raw={"location": {"countryCode": "NL"}},
            ).qualify()
            if generic:
                from car_market.search_profile import qualify

                qualify(item, config["scope"])
            self.db.observe(run, item, SYNTHETIC_CLOCK, fetch, query_key="fixture")
            self.db.finish_source(
                run,
                "autoscout24",
                "fixture",
                SYNTHETIC_CLOCK,
                "complete",
                1,
                1,
                int(item.scope == "target"),
                1,
                infer_missing=False,
                finished_at=SYNTHETIC_CLOCK,
            )
            result = {"run_id": run, "status": collection_status, **self.db.summary()}
            self.db.finish_run(run, collection_status, result)
            expected.append(facts(self.db.conn))
            return result

    monkeypatch.setattr(cli, "Collector", FakeCollector)
    monkeypatch.setattr(opportunities, "publish", lambda *args, **kwargs: None)
    if portable is not None:
        monkeypatch.setattr(configured_collect, "ConfiguredCollector", FakeCollector)
    args = SimpleNamespace(
        db=str(db_path),
        export_dir=str(tmp_path / "exports"),
        command="collect",
        sources=None,
        browser=False,
        no_details=True,
        no_rdw=True,
        fast=False,
    )

    def call(fast=False):
        args.fast = fast
        return (
            portable.collect(args, tmp_path, config, db_path, fast=fast)
            if generic
            else cli.collect_once(args, config)
        )

    def read():
        db = Database.read_only(db_path)
        try:
            assert facts(db.conn) == expected[-1]
            source = db.conn.execute(
                "SELECT fetched_at,content_hash FROM fetches WHERE transport='retained_native_projection'"
            ).fetchone()
            assert source["fetched_at"] == NATIVE["time"]
            assert (
                source["content_hash"]
                == hashlib.sha256((ROOT / NAME).read_bytes()).hexdigest()
            )
            return publication.report(db)
        finally:
            db.conn.close()

    return call, read, db_path, expected, config


@pytest.mark.parametrize("interface", ("legacy", "configured"))
@pytest.mark.parametrize("fault", ("signals", "export", "last_run"))
def test_postcollection_failures_are_visible_without_rewriting_facts(
    tmp_path, monkeypatch, interface, fault
):
    if interface == "configured" and fault == "signals":
        pytest.skip("Configured collection has no signals publisher")
    call, read, db_path, _, config = setup(tmp_path, monkeypatch, interface)
    baseline = call()
    last = db_path.parent / "last-run.json"
    before_last = last.read_bytes()
    before_exports = files(tmp_path / "exports")
    wanted = OSError(errno.ENOSPC, "controlled private message must not enter metadata")

    def fail(*args, **kwargs):
        raise wanted

    if fault == "signals":
        monkeypatch.setattr(opportunities, "publish", fail)
    elif fault == "export":
        monkeypatch.setattr(Database, "export", fail)
    else:
        replace = publication.os.replace

        def fail_last(source, destination):
            if Path(destination) == last:
                raise wanted
            return replace(source, destination)

        monkeypatch.setattr(publication.os, "replace", fail_last)
    with pytest.raises(OSError) as raised:
        call()
    assert raised.value is wanted
    view = read()
    assert view["status"] == "failed"
    attempt = view["latest_attempt"]
    assert attempt["run_id"] > baseline["run_id"]
    assert attempt["collection_status"] == "complete"
    assert attempt["mode"] == "full"
    assert attempt["phases"][fault]["status"] == "failed"
    assert attempt["phases"][fault]["error"] == {
        "type": "OSError",
        "errno": errno.ENOSPC,
    }
    assert "private message" not in json.dumps(view)
    assert last.read_bytes() == before_last
    if fault != "last_run":
        assert files(tmp_path / "exports") == before_exports
        assert view["last_successful_full_export"]["run_id"] == baseline["run_id"]
    else:
        assert view["last_successful_full_export"]["run_id"] == attempt["run_id"]
        assert view["matches_latest_full_attempt"] is True
    if portable is not None and interface == "configured":
        doctor = portable.diagnostic(tmp_path, config, db_path)
        assert doctor["healthy"] is True and doctor["market_complete"] is False
        assert doctor["publication_ok"] is False
        assert doctor["publication"]["status"] == "failed"


@pytest.mark.parametrize("interface", ("legacy", "configured"))
def test_success_then_fast_preserves_dated_full_outcome(
    tmp_path, monkeypatch, interface
):
    call, read, db_path, _, _ = setup(tmp_path, monkeypatch, interface)
    first = call()
    before = files(tmp_path / "exports")
    assert read()["status"] == "success"
    second = call(fast=True)
    view = read()
    assert view["status"] == "success"
    assert view["latest_attempt"]["run_id"] == second["run_id"]
    assert view["latest_attempt"]["mode"] == "skipped_fast"
    assert view["latest_attempt"]["phases"]["export"]["status"] == "disabled"
    assert view["last_successful_full_export"]["run_id"] == first["run_id"]
    assert view["matches_latest_full_attempt"] is True
    assert files(tmp_path / "exports") == before
    assert (
        json.loads((db_path.parent / "last-run.json").read_text())["run_id"]
        == second["run_id"]
    )


@pytest.mark.parametrize("interface", ("legacy", "configured"))
def test_fast_success_does_not_hide_previous_full_failure(
    tmp_path, monkeypatch, interface
):
    call, read, _, _, _ = setup(tmp_path, monkeypatch, interface)
    baseline = call()
    with monkeypatch.context() as fault:
        fault.setattr(
            Database,
            "export",
            lambda *args: (_ for _ in ()).throw(
                OSError(errno.ENOSPC, "controlled full failure")
            ),
        )
        with pytest.raises(OSError):
            call()
    fast = call(fast=True)
    view = read()
    assert view["latest_attempt"]["run_id"] == fast["run_id"]
    assert view["latest_attempt"]["phases"]["last_run"]["status"] == "success"
    assert view["latest_full_attempt"]["phases"]["export"]["status"] == "failed"
    assert view["status"] == "failed"
    assert view["last_successful_full_export"]["run_id"] == baseline["run_id"]
    assert view["matches_latest_full_attempt"] is False


def test_metadata_failure_cannot_mask_primary_phase_error(tmp_path, monkeypatch):
    call, _, db_path, _, _ = setup(tmp_path, monkeypatch, "legacy")
    call()
    original = publication.Publication._persist
    wanted = OSError(errno.ENOSPC, "original export error")

    def persist(self, *, failing=False):
        if failing:
            raise OSError(errno.EACCES, "secondary metadata error")
        return original(self, failing=failing)

    monkeypatch.setattr(publication.Publication, "_persist", persist)
    monkeypatch.setattr(Database, "export", lambda *args: (_ for _ in ()).throw(wanted))
    with pytest.raises(OSError) as raised:
        call()
    assert raised.value is wanted
    db = Database.read_only(db_path)
    try:
        assert publication.report(db)["status"] in {"inprogress", "unresolved"}
    finally:
        db.conn.close()


@pytest.mark.parametrize(
    "variant",
    (
        "absent",
        "malformed",
        "future",
        "foreign",
        "naive",
        "missing_receipt",
        "early_attempt",
        "early_phase",
        "late_phase",
        "overflow",
    ),
)
def test_legacy_or_invalid_metadata_never_means_success(tmp_path, monkeypatch, variant):
    call, _, db_path, _, _ = setup(tmp_path, monkeypatch, "legacy")
    call()
    db = Database(db_path)
    path = db_path.parent / "publication-attempt.json"
    doc = json.loads(path.read_text())
    if variant == "future":
        doc["updated_at"] = "2099-01-01T00:00:00+00:00"
    elif variant == "naive":
        doc["updated_at"] = "2026-10-11T00:00:00"
    elif variant == "overflow":
        doc["updated_at"] = "9999-12-31T23:59:59-23:59"
    elif variant == "foreign":
        doc["database_key"] = "wrong database"
    elif variant == "malformed":
        doc["latest"]["phases"]["export"]["status"] = []
    elif variant == "missing_receipt":
        doc["last_export"] = None
    elif variant == "early_attempt":
        doc["latest"]["started_at"] = SYNTHETIC_CLOCK
    elif variant == "early_phase":
        doc["latest"]["phases"]["export"]["started_at"] = SYNTHETIC_CLOCK
    elif variant == "late_phase":
        doc["latest"]["phases"]["export"]["finished_at"] = "2099-01-01T00:00:00+00:00"
    if variant == "absent":
        db.conn.execute("DELETE FROM monitor_state WHERE key=?", (publication.KEY,))
        path.unlink()
    else:
        db.conn.execute(
            "UPDATE monitor_state SET value=? WHERE key=?",
            (json.dumps(doc), publication.KEY),
        )
        path.write_text(json.dumps(doc))
    db.conn.commit()
    try:
        assert publication.report(db)["status"] == "unresolved"
    finally:
        db.conn.close()


@pytest.mark.parametrize("interface", ("legacy", "configured"))
def test_successful_publication_keeps_partial_collection_partial(
    tmp_path, monkeypatch, interface
):
    call, read, _, _, _ = setup(
        tmp_path, monkeypatch, interface, collection_status="partial"
    )
    result = call()
    assert result["status"] == "partial"
    assert read()["status"] == "success"
    assert read()["latest_attempt"]["collection_status"] == "partial"


class WriteFailureConnection:
    def __init__(self, conn, enabled):
        self.conn, self.enabled = conn, enabled

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def __enter__(self):
        self.conn.__enter__()
        return self

    def __exit__(self, *args):
        return self.conn.__exit__(*args)

    def execute(self, statement, parameters=()):
        if (
            self.enabled()
            and statement.startswith("INSERT OR REPLACE INTO monitor_state")
            and parameters
            and parameters[0] == publication.KEY
        ):
            raise sqlite3.OperationalError("controlled metadata SQL failure")
        return self.conn.execute(statement, parameters)


@pytest.mark.parametrize("backend", ("db", "file", "both"))
@pytest.mark.parametrize("interface", ("legacy", "configured"))
@pytest.mark.parametrize("fault", ("signals", "export", "last_run"))
def test_metadata_backends_are_best_effort_and_cannot_create_success(
    tmp_path, monkeypatch, backend, interface, fault
):
    if interface == "configured" and fault == "signals":
        pytest.skip("Configured collection has no signals publisher")
    call, read, db_path, _, config = setup(tmp_path, monkeypatch, interface)
    baseline = call()
    assert read()["status"] == "success"
    old_file = (db_path.parent / "publication-attempt.json").read_bytes()
    old_last = (db_path.parent / "last-run.json").read_bytes()
    with sqlite3.connect(db_path) as conn:
        old_db = conn.execute(
            "SELECT value FROM monitor_state WHERE key=?", (publication.KEY,)
        ).fetchone()[0]
    original_init = publication.Publication.__init__
    module = portable if interface == "configured" else cli
    original_writer = module.atomic_json

    def init(self, db, *args, **kwargs):
        db.conn = WriteFailureConnection(db.conn, lambda: backend in {"db", "both"})
        return original_init(self, db, *args, **kwargs)

    def writer(path, value):
        if (
            backend in {"file", "both"}
            and Path(path).name == "publication-attempt.json"
        ):
            raise OSError(errno.EACCES, "controlled metadata file failure")
        return original_writer(path, value)

    monkeypatch.setattr(publication.Publication, "__init__", init)
    monkeypatch.setattr(module, "atomic_json", writer)
    wanted = OSError(errno.ENOSPC, "primary phase failure")

    def fail(*args, **kwargs):
        raise wanted

    if fault == "signals":
        monkeypatch.setattr(opportunities, "publish", fail)
    elif fault == "export":
        monkeypatch.setattr(Database, "export", fail)
    else:
        replace = publication.os.replace

        def fail_last(source, destination):
            if Path(destination) == db_path.parent / "last-run.json":
                raise wanted
            return replace(source, destination)

        monkeypatch.setattr(publication.os, "replace", fail_last)
    with pytest.raises(OSError) as raised:
        call()
    assert raised.value is wanted
    view = read()
    assert view["status"] == ("unresolved" if backend == "both" else "failed")
    assert (db_path.parent / "last-run.json").read_bytes() == old_last
    if backend == "both":
        assert view["latest_attempt"] is None and view["latest_full_attempt"] is None
        assert view["last_successful_full_export"]["run_id"] == baseline["run_id"]
        assert view["matches_latest_full_attempt"] is False
        assert (db_path.parent / "publication-attempt.json").read_bytes() == old_file
        with sqlite3.connect(db_path) as conn:
            assert (
                conn.execute(
                    "SELECT value FROM monitor_state WHERE key=?", (publication.KEY,)
                ).fetchone()[0]
                == old_db
            )
    if portable is not None:
        doctor = portable.diagnostic(tmp_path, config, db_path)
        assert doctor["publication_ok"] is False
        assert doctor["publication"]["status"] == view["status"]


@pytest.mark.parametrize("interface", ("legacy", "configured"))
@pytest.mark.parametrize("fast", (False, True))
def test_new_completed_run_with_both_metadata_writers_failed_is_unresolved(
    tmp_path, monkeypatch, interface, fast
):
    call, read, db_path, _, config = setup(tmp_path, monkeypatch, interface)
    first = call()
    assert read()["status"] == "success"
    prior = read()
    metadata_path = db_path.parent / "publication-attempt.json"
    prior_file = metadata_path.read_bytes()
    with sqlite3.connect(db_path) as conn:
        prior_db = conn.execute(
            "SELECT value FROM monitor_state WHERE key=?", (publication.KEY,)
        ).fetchone()[0]
    module = portable if interface == "configured" else cli
    original_init, original_writer = (
        publication.Publication.__init__,
        module.atomic_json,
    )

    def init(self, db, *args, **kwargs):
        db.conn = WriteFailureConnection(db.conn, lambda: True)
        return original_init(self, db, *args, **kwargs)

    def writer(path, value):
        if Path(path).name == "publication-attempt.json":
            raise OSError(errno.ENOSPC, "controlled metadata failure")
        return original_writer(path, value)

    monkeypatch.setattr(publication.Publication, "__init__", init)
    monkeypatch.setattr(module, "atomic_json", writer)
    second = call(fast=fast)
    view = read()
    assert second["status"] == "complete" and second["run_id"] > first["run_id"]
    assert view["status"] == "unresolved" and view["latest_attempt"] is None
    assert view["last_successful_full_export"]["run_id"] == first["run_id"]
    visible = json.loads((db_path.parent / "last-run.json").read_text())
    assert visible["run_id"] == second["run_id"] and visible["status"] == "complete"
    # The visible file has no matching successful phase receipt: both writers
    # failed. Preserve the prior receipt without certifying the current call.
    assert view["last_successful_invocation_run_id"] is None
    assert view["last_successful_full_export"] == prior["last_successful_full_export"]
    assert view["last_successful_export"] == prior["last_successful_export"]
    # Fast does not replace the previous full collection or its export receipt.
    assert view["matches_latest_full_attempt"] is fast
    assert metadata_path.read_bytes() == prior_file
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT value FROM monitor_state WHERE key=?", (publication.KEY,)
            ).fetchone()[0]
            == prior_db
        )
    if portable is not None:
        doctor = portable.diagnostic(tmp_path, config, db_path)
        assert doctor["publication_ok"] is False
        assert doctor["publication"]["status"] == "unresolved"
        assert doctor["publication"]["last_successful_invocation_run_id"] is None
        assert read() == view


def test_equal_clock_file_failure_state_wins_older_db_revision(tmp_path, monkeypatch):
    call, read, _, _, _ = setup(tmp_path, monkeypatch, "legacy")
    call()
    clock = publication._now()
    monkeypatch.setattr(publication, "_now", lambda: clock)
    monkeypatch.setattr(db_module, "now", lambda: clock)
    original_init = publication.Publication.__init__
    flag = [False]

    def init(self, db, *args, **kwargs):
        db.conn = WriteFailureConnection(db.conn, lambda: flag[0])
        return original_init(self, db, *args, **kwargs)

    def fail(*args):
        flag[0] = True
        raise OSError(errno.ENOSPC, "controlled export failure")

    monkeypatch.setattr(publication.Publication, "__init__", init)
    monkeypatch.setattr(Database, "export", fail)
    with pytest.raises(OSError):
        call()
    view = read()
    assert view["status"] == "failed"
    assert view["latest_attempt"]["phases"]["export"]["status"] == "failed"


def test_declared_signals_noop_is_disabled(tmp_path, monkeypatch):
    call, read, _, _, _ = setup(tmp_path, monkeypatch, "legacy")
    monkeypatch.setattr(
        opportunities,
        "publish",
        lambda *args, **kwargs: {"status": "not_configured_for_model_profile"},
    )
    call()
    view = read()
    assert view["status"] == "success"
    assert view["latest_attempt"]["phases"]["signals"]["status"] == "disabled"


@pytest.mark.parametrize("interface", ("legacy", "configured"))
@pytest.mark.parametrize("failure", ("csv", "rename"))
def test_actual_export_failure_records_phase_after_transaction_cleanup(
    tmp_path, monkeypatch, interface, failure
):
    call, read, db_path, _, _ = setup(tmp_path, monkeypatch, interface)
    baseline = call()
    old_last = (db_path.parent / "last-run.json").read_bytes()
    wanted = OSError(errno.ENOSPC, "controlled actual export full")
    if failure == "csv":
        writer = db_module.csv.writer

        class FailingWriter:
            def __init__(self, stream, *args, **kwargs):
                self.writer = writer(stream, *args, **kwargs)

            def writerow(self, row):
                return self.writer.writerow(row)

            def writerows(self, rows):
                next(iter(rows), None)
                raise wanted

        def factory(stream, *args, **kwargs):
            if Path(stream.name).name == "price-history.csv":
                return FailingWriter(stream, *args, **kwargs)
            return writer(stream, *args, **kwargs)

        monkeypatch.setattr(db_module.csv, "writer", factory)
    else:
        replace = Path.replace

        def fail(path, destination):
            if (
                path.name == "price-history.csv"
                and path.parent.name.startswith(".market-export-")
                and not path.parent.name.startswith(".market-export-recovery-")
            ):
                raise wanted
            return replace(path, destination)

        monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError) as raised:
        call()
    assert raised.value is wanted
    view = read()
    assert view["status"] == "failed"
    assert view["latest_attempt"]["phases"]["export"]["error"]["errno"] == errno.ENOSPC
    assert view["last_successful_full_export"]["run_id"] == baseline["run_id"]
    assert view["matches_latest_full_attempt"] is False
    assert (db_path.parent / "last-run.json").read_bytes() == old_last
    assert not list((tmp_path / "exports").glob(".market-export-*"))


def test_grouped_export_error_keeps_primary_errno_and_original_exception(
    tmp_path, monkeypatch
):
    call, read, _, _, _ = setup(tmp_path, monkeypatch, "legacy")
    call()
    wanted = ExceptionGroup(
        "controlled rollback failure",
        [OSError(errno.ENOSPC, "primary"), OSError(errno.EACCES, "restore")],
    )
    monkeypatch.setattr(Database, "export", lambda *args: (_ for _ in ()).throw(wanted))
    with pytest.raises(ExceptionGroup) as raised:
        call()
    assert raised.value is wanted
    assert read()["latest_attempt"]["phases"]["export"]["error"] == {
        "type": "OSError",
        "errno": errno.ENOSPC,
    }
