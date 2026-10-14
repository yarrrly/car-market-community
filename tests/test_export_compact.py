"""Small actual export/publisher faults; retained native facts never get renewed."""

import copy
import errno
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_publication_outcomes import setup as publication_setup

from car_market import cli, publication
from car_market.db import Database
from car_market.export_formats import COMPACT_FILES, MANIFEST, OMITTED
from car_market.model_profiles import BMW_PROFILE, MERCEDES_PROFILE, bind_profile

PORTABLE = importlib.util.find_spec("car_market.portable") is not None


def file_set(directory):
    return {
        p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_ino)
        for p in directory.iterdir()
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    }


def configured_scenario(tmp_path, monkeypatch, generic=False):
    call, read, path, _expected, config = publication_setup(
        tmp_path, monkeypatch, "configured" if generic else "legacy"
    )
    choice = {"mode": "full"}
    module = cli
    method = "collect_once"
    if generic:
        from car_market import portable

        module, method = portable, "collect"
    original = getattr(module, method)

    def dispatch(args, *positional, **keywords):
        args.export_mode = choice["mode"]
        return original(args, *positional, **keywords)

    monkeypatch.setattr(module, method, dispatch)
    return call, read, path, config, choice


def test_explicit_format_preserves_full_default_native_SQL_and_raw(
    tmp_path, monkeypatch
):
    call, read, path, _, choice = configured_scenario(tmp_path, monkeypatch)
    call()
    target = tmp_path / "exports"
    old_full = file_set(target)
    assert len(old_full) == 12 and set(OMITTED) <= old_full.keys()
    assert not (target / "compact").exists()
    raw_before = file_set(path.parent / "raw")
    db = Database(path)
    try:
        before = list(db.conn.iterdump())
        queries = []
        db.conn.set_trace_callback(queries.append)
        original_open = Path.open

        def forbid_rich_JSON(p, *args, **kwargs):
            assert p.name != "vehicles.json"
            return original_open(p, *args, **kwargs)

        with monkeypatch.context() as guard:
            guard.setattr(Path, "open", forbid_rich_JSON)
            value = db.export(target, mode="compact")
        db.conn.set_trace_callback(None)
        assert list(db.conn.iterdump()) == before
        receipt = value["export_artifacts"]
        compact = target / "compact"
        assert set(file_set(compact)) == {*COMPACT_FILES, MANIFEST}
        assert json.loads((compact / MANIFEST).read_text()) == receipt
        assert receipt["snapshot_captured_at"] == receipt["derived_as_of"]
        assert receipt["directory"] == str(compact.resolve())
        assert receipt["omitted"] == list(OMITTED)
        assert {f["name"]: f["bytes"] for f in receipt["files"]} == {
            name: (compact / name).stat().st_size for name in COMPACT_FILES
        }
        assert not any("l.vehicle_id,l.url,o.*" in sql for sql in queries)
        with sqlite3.connect(compact / "market.sqlite3") as exported:
            assert list(exported.iterdump()) == before
            assert exported.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert not exported.execute("PRAGMA foreign_key_check").fetchall()
        assert file_set(target) == old_full
        assert file_set(path.parent / "raw") == raw_before
        # Repeating compact and returning to full do not lose any stored facts.
        db.export(target, mode="compact")
        assert list(db.conn.iterdump()) == before
        assert "export_artifacts" not in db.export(target)
        assert len(file_set(target)) == 12
        assert list(db.conn.iterdump()) == before
    finally:
        db.conn.close()
    assert (
        read()["export_format"] == "full"
    )  # manual export is not a new collection attempt
    assert choice["mode"] == "full"


@pytest.mark.parametrize(
    "generic",
    [
        False,
        pytest.param(
            True, marks=pytest.mark.skipif(not PORTABLE, reason="Portable ABI only")
        ),
    ],
)
def test_actual_collection_compact_fast_and_full_receipts(
    tmp_path, monkeypatch, generic
):
    call, read, path, config, choice = configured_scenario(
        tmp_path, monkeypatch, generic
    )
    first = call()
    full_before = file_set(tmp_path / "exports")
    choice["mode"] = "compact"
    second = call()
    view = read()
    assert view["status"] == "success"
    assert view["latest_attempt"]["mode"] == "full"
    assert view["export_format"] == "compact"
    assert view["last_successful_export"]["run_id"] == second["run_id"]
    assert view["last_successful_full_export"]["run_id"] == first["run_id"]
    assert view["matches_latest_full_attempt"] is True
    assert view["flat_full_export_matches_latest_full_attempt"] is False
    assert file_set(tmp_path / "exports") == full_before
    receipt = copy.deepcopy(view["last_successful_export"]["phases"]["export"])
    compact_before = file_set(tmp_path / "exports/compact")
    call(fast=True)
    after = read()
    assert after["latest_attempt"]["mode"] == "skipped_fast"
    assert after["latest_attempt"]["phases"]["export"]["status"] == "disabled"
    assert after["last_successful_export"]["phases"]["export"] == receipt
    assert file_set(tmp_path / "exports/compact") == compact_before
    assert file_set(tmp_path / "exports") == full_before
    if generic:
        from car_market.portable import diagnostic

        doctor = diagnostic(tmp_path, config, path)
        assert doctor["publication_ok"] is True
        assert doctor["publication"]["export_format"] == "compact"
        assert doctor["publication"]["export_directory"] == str(
            (tmp_path / "exports/compact").resolve()
        )


@pytest.mark.parametrize(
    "failure",
    ["prepare", "market-current.csv", "source-health.csv", MANIFEST, "rollback"],
)
def test_compact_ENOSPC_preserves_generations_and_manual_retry_state(
    tmp_path, monkeypatch, failure
):
    call, read, path, _, choice = configured_scenario(tmp_path, monkeypatch)
    call()
    choice["mode"] = "compact"
    call()
    target = tmp_path / "exports/compact"
    old = file_set(target)
    full = file_set(tmp_path / "exports")
    replace, open_file = Path.replace, Path.open

    def fault_replace(p, destination):
        if failure == "rollback" and p.parent.name.startswith(
            ".market-export-recovery-"
        ):
            raise OSError(errno.EACCES, "controlled compact restore denied")
        wanted = "source-health.csv" if failure == "rollback" else failure
        if (
            p.parent.name.startswith(".market-export-")
            and not p.parent.name.startswith(".market-export-recovery-")
            and p.name == wanted
        ):
            raise OSError(errno.ENOSPC, "controlled compact publication full")
        return replace(p, destination)

    def fault_open(p, *args, **kwargs):
        if p.name == MANIFEST and (args[0] if args else kwargs.get("mode", "r")) == "w":
            raise OSError(errno.ENOSPC, "controlled compact preparation full")
        return open_file(p, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "replace", fault_replace)
        if failure == "prepare":
            fault.setattr(Path, "open", fault_open)
        with pytest.raises(
            BaseExceptionGroup if failure == "rollback" else OSError
        ) as raised:
            call()
    if failure == "rollback":
        assert [e.errno for e in raised.value.exceptions] == [
            errno.ENOSPC,
            errno.EACCES,
        ]
        recovery = list(target.glob(".market-export-recovery-*"))
        assert len(recovery) == 1 and file_set(recovery[0]) == old
        assert recovery[0].stat().st_mode & 0o777 == 0o700
    else:
        assert file_set(target) == old
        assert not list(target.glob(".market-export-*"))
    assert file_set(tmp_path / "exports") == full
    failed = read()
    assert failed["status"] == "failed"
    assert failed["latest_attempt"]["phases"]["export"]["format"] == "compact"
    assert (
        failed["latest_attempt"]["phases"]["export"]["error"]["errno"] == errno.ENOSPC
    )
    assert failed["latest_attempt"]["phases"]["last_run"]["status"] == "unresolved"
    db = Database(path)
    try:
        before = list(db.conn.iterdump())
        db.export(tmp_path / "exports", mode="compact")
        assert list(db.conn.iterdump()) == before
    finally:
        db.conn.close()
    assert read() == failed


@pytest.mark.parametrize("bad", [None, False, 1, "Compact", "gzip"])
def test_invalid_mode_does_not_create_or_mutate(tmp_path, bad):
    db = Database(tmp_path / "data/market.sqlite3")
    try:
        before = list(db.conn.iterdump())
        with pytest.raises(ValueError, match="Export mode"):
            db.export(tmp_path / "exports", mode=bad)
        assert list(db.conn.iterdump()) == before
        assert not (tmp_path / "exports").exists()
    finally:
        db.conn.close()


@pytest.mark.parametrize("alias", ["root", "hardlink", "symlink"])
def test_compact_cannot_replace_working_SQL(tmp_path, alias):
    db = Database(tmp_path / "working/market.sqlite3")
    target = db.path.parent
    if alias != "root":
        target = tmp_path / "exports"
        (target / "compact").mkdir(parents=True)
        link = target / "compact/market.sqlite3"
        os.link(db.path, link) if alias == "hardlink" else link.symlink_to(db.path)
    try:
        before = list(db.conn.iterdump())
        with pytest.raises(ValueError, match="different files"):
            db.export(target, mode="compact")
        assert list(db.conn.iterdump()) == before
        assert not list(target.glob(".market-export-*"))
    finally:
        db.conn.close()


@pytest.mark.parametrize("location", ["base", "compact"])
@pytest.mark.parametrize("foreign", ["country", "profile"])
def test_existing_foreign_SQL_remains_rejected(tmp_path, location, foreign):
    db = Database(tmp_path / "working/market.sqlite3")
    bind_profile(db, {"scope": {"country": "NL", "model_profile": BMW_PROFILE}})
    target = tmp_path / "exports"
    backup = Database(
        (target if location == "base" else target / "compact") / "market.sqlite3"
    )
    bind_profile(
        backup,
        {
            "scope": {
                "country": "DE" if foreign == "country" else "NL",
                "model_profile": MERCEDES_PROFILE
                if foreign == "profile"
                else BMW_PROFILE,
            }
        },
    )
    backup.conn.close()
    old = file_set(backup.path.parent)
    try:
        with pytest.raises(ValueError, match="country|profile"):
            db.export(target, mode="compact")
        assert file_set(backup.path.parent) == old
    finally:
        db.conn.close()


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "future",
        "naive",
        "old",
        "foreign_DB",
        "scope",
        "run",
        "path",
        "duplicate",
        "bool_bytes",
        "omitted",
        "phase_extra",
    ],
)
def test_doctor_never_accepts_bad_compact_receipt(tmp_path, monkeypatch, corruption):
    call, _, path, _, choice = configured_scenario(tmp_path, monkeypatch)
    choice["mode"] = "compact"
    call()
    document = json.loads((path.parent / "publication-attempt.json").read_text())
    for key in ["latest", "last_full", "last_export"]:
        phase = document[key]["phases"]["export"]
        receipt = phase["receipt"]
        if corruption == "missing":
            phase["receipt"] = None
        elif corruption in {"future", "naive", "old"}:
            value = (
                "2099-01-01T00:00:00+00:00"
                if corruption == "future"
                else "2020-01-01T00:00:00"
                if corruption == "naive"
                else "2020-01-01T00:00:00+00:00"
            )
            receipt["snapshot_captured_at"] = receipt["derived_as_of"] = value
        elif corruption == "foreign_DB":
            receipt["database_key"] = "0" * 64
        elif corruption == "scope":
            receipt["scope"] = {"country": "DE", "model_profile": BMW_PROFILE}
        elif corruption == "run":
            receipt["snapshot_latest_run_id"] += 1
        elif corruption == "path":
            receipt["directory"] = str(tmp_path / "foreign/compact")
        elif corruption == "duplicate":
            receipt["files"][1] = receipt["files"][0]
        elif corruption == "bool_bytes":
            receipt["files"][0]["bytes"] = True
        elif corruption == "omitted":
            receipt["omitted"].append("market.sqlite3")
        else:
            phase["unexpected"] = True
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE monitor_state SET value=? WHERE key=?",
            (json.dumps(document), publication.KEY),
        )
    (path.parent / "publication-attempt.json").write_text(json.dumps(document))
    db = Database.read_only(path)
    try:
        assert publication.report(db)["status"] == "unresolved"
    finally:
        db.conn.close()


def test_legacy_valid_metadata_and_current_compact_clock_edges(tmp_path, monkeypatch):
    call, read, path, _, choice = configured_scenario(tmp_path, monkeypatch)
    call()
    doc = json.loads((path.parent / "publication-attempt.json").read_text())
    doc.pop("last_full_format_export")
    for key in ["latest", "last_full", "last_export"]:
        phase = doc[key]["phases"]["export"]
        for field in ["format", "directory", "receipt"]:
            phase.pop(field)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE monitor_state SET value=? WHERE key=?",
            (json.dumps(doc), publication.KEY),
        )
    (path.parent / "publication-attempt.json").write_text(json.dumps(doc))
    assert read()["status"] == "success" and read()["export_directory"] is None
    choice["mode"] = "compact"
    call()
    db = Database.read_only(path)
    try:
        state = read()["last_successful_export"]
        now = datetime.now(UTC)
        for edge in ["started_at", "finished_at"]:
            changed = copy.deepcopy(state)
            receipt = changed["phases"]["export"]["receipt"]
            receipt["snapshot_captured_at"] = receipt["derived_as_of"] = changed[
                "phases"
            ]["export"][edge]
            assert publication._valid_state(db.conn, changed, now, path)
        changed = copy.deepcopy(state)
        receipt = changed["phases"]["export"]["receipt"]
        before = publication.timestamp(
            changed["phases"]["export"]["started_at"]
        ) - timedelta(microseconds=1)
        receipt["snapshot_captured_at"] = receipt["derived_as_of"] = before.isoformat()
        assert not publication._valid_state(db.conn, changed, now, path)
    finally:
        db.conn.close()


def test_portable_report_SQL_reader_works_without_omitted_files(tmp_path, monkeypatch):
    if not PORTABLE:
        pytest.skip("Portable analytics ABI only")
    from car_market.analytics import analyze_market

    call, _, path, _, _ = configured_scenario(tmp_path, monkeypatch)
    call()
    db = Database(path)
    try:
        result = db.export(tmp_path / "exports", mode="compact")
    finally:
        db.conn.close()
    clock = result["export_artifacts"]["derived_as_of"]
    compact = tmp_path / "exports/compact/market.sqlite3"
    assert analyze_market(compact, "NL", as_of=clock) == analyze_market(
        path, "NL", as_of=clock
    )


@pytest.mark.parametrize(
    "generic",
    [
        False,
        pytest.param(
            True, marks=pytest.mark.skipif(not PORTABLE, reason="Portable ABI only")
        ),
    ],
)
def test_actual_manual_CLI_format_and_SQL_import(
    tmp_path, monkeypatch, capsys, generic
):
    call, read, path, config, _ = configured_scenario(tmp_path, monkeypatch, generic)
    call()
    capsys.readouterr()
    if generic:
        from car_market import portable

        assert (
            portable.main(
                ["--workspace", str(tmp_path), "export", "--export-mode", "compact"]
            )
            == 0
        )
    else:
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config))
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "car-market",
                "--db",
                str(path),
                "--config",
                str(config_path),
                "--export-dir",
                str(tmp_path / "exports"),
                "--export-mode",
                "compact",
                "export",
            ],
        )
        cli.main()
    manual = json.loads(capsys.readouterr().out)
    assert manual["export_artifacts"]["mode"] == "compact"
    assert read()["export_format"] == "full"  # no new collector/publication attempt
    if generic:
        return  # configurable profile import is not an existing legacy importer contract
    source = tmp_path / "exports/compact/market.sqlite3"
    destination = tmp_path / "imported/market.sqlite3"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "car-market",
            "--db",
            str(destination),
            "--config",
            str(config_path),
            "--export-dir",
            str(tmp_path / "import-export"),
            "import-db",
            str(source),
        ],
    )
    cli.main()
    with sqlite3.connect(source) as original, sqlite3.connect(destination) as imported:
        assert (
            imported.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            == original.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        )
        assert (
            imported.execute(
                "SELECT fetched_at,content_hash FROM fetches ORDER BY id"
            ).fetchall()
            == original.execute(
                "SELECT fetched_at,content_hash FROM fetches ORDER BY id"
            ).fetchall()
        )
        assert (
            imported.execute(
                "SELECT observed_at,price_eur,price_type,year FROM observations ORDER BY id"
            ).fetchall()
            == original.execute(
                "SELECT observed_at,price_eur,price_type,year FROM observations ORDER BY id"
            ).fetchall()
        )
        assert imported.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert imported.execute("PRAGMA foreign_key_check").fetchall() == []


def test_uncommitted_compact_never_creates_generation(tmp_path):
    db = Database(tmp_path / "data/market.sqlite3")
    try:
        db.conn.execute(
            "INSERT INTO vehicles(id,created_at) VALUES ('uncommitted','synthetic')"
        )
        with pytest.raises(ValueError, match="committed"):
            db.export(tmp_path / "exports", mode="compact")
        assert db.conn.in_transaction
        assert not (tmp_path / "exports/compact").exists()
    finally:
        db.conn.close()


@pytest.mark.parametrize(
    "alias",
    ["directory", "indirect_parent", "hardlink", "file_symlink", "manifest_symlink"],
)
def test_compact_namespace_cannot_alias_retained_full_outputs(
    tmp_path, monkeypatch, alias
):
    call, _, path, _, _ = configured_scenario(tmp_path, monkeypatch)
    call()
    root = tmp_path / "exports"
    full_before = file_set(root)
    compact = root / "compact"
    if alias == "directory":
        compact.symlink_to(root, target_is_directory=True)
    elif alias == "indirect_parent":
        parent = tmp_path / "redirect"
        parent.symlink_to(tmp_path, target_is_directory=True)
        compact.symlink_to(parent / "exports", target_is_directory=True)
    else:
        compact.mkdir()
        selected = compact / (
            MANIFEST if alias == "manifest_symlink" else "source-health.csv"
        )
        retained = root / "price-history.csv"  # different output name must still reject
        os.link(retained, selected) if alias == "hardlink" else selected.symlink_to(
            retained
        )
    db = Database(path)
    try:
        before = list(db.conn.iterdump())
        with pytest.raises(ValueError, match="separate|alias"):
            db.export(root, mode="compact")
        assert list(db.conn.iterdump()) == before
        assert file_set(root) == full_before
        assert not list(root.glob(".market-export-*"))
        assert not list(compact.glob(".market-export-*"))
    finally:
        db.conn.close()


def test_parent_symlink_keeps_independent_output_inodes(tmp_path, monkeypatch):
    call, _, path, _, _ = configured_scenario(tmp_path, monkeypatch)
    call()
    real = tmp_path / "exports"
    alias = tmp_path / "exports-parent"
    alias.symlink_to(tmp_path, target_is_directory=True)
    db = Database(path)
    try:
        db.export(alias / "exports", mode="compact")
        assert all(
            not (real / "compact" / selected).samefile(real / retained)
            for selected in (*COMPACT_FILES, MANIFEST)
            for retained in (*COMPACT_FILES, *OMITTED)
        )
    finally:
        db.conn.close()


def test_namespace_alias_added_during_preparation_is_rejected_before_promotion(
    tmp_path, monkeypatch
):
    from car_market import export_formats

    call, _, path, _, _ = configured_scenario(tmp_path, monkeypatch)
    call()
    root = tmp_path / "exports"
    before = file_set(root)
    manifest = export_formats.compact_manifest

    def replace_with_alias(*args, **kwargs):
        result = manifest(*args, **kwargs)
        (root / "compact/events.csv").symlink_to(root / "vehicles.json")
        return result

    monkeypatch.setattr(export_formats, "compact_manifest", replace_with_alias)
    db = Database(path)
    try:
        with pytest.raises(ValueError, match="alias"):
            db.export(root, mode="compact")
        assert file_set(root) == before
        assert not list((root / "compact").glob(".market-export-*"))
    finally:
        db.conn.close()
