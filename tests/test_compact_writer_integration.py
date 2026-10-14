"""Only new compact/publication-writer interactions; no historical suite replay."""

import copy
import errno
import json
import os
import stat
from os import replace as system_replace
from pathlib import Path

import pytest
from test_export_compact import configured_scenario, file_set
from test_publication_outcomes import WriteFailureConnection

from car_market import cli, publication
from car_market.db import Database

CANONICAL = hasattr(publication, "COMMON")


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("metadata", ["available", "both_unavailable"])
def test_post_replace_compact_callback_error_never_certifies_current_pointer(
    tmp_path, monkeypatch, fast, metadata
):
    call, read, path, _, choice = configured_scenario(tmp_path, monkeypatch)
    full = call()
    old_full = file_set(tmp_path / "exports")
    choice["mode"] = "compact"
    compact = call()
    old_receipt = copy.deepcopy(read()["last_successful_export"])
    original_writer = cli.atomic_json
    wanted = OSError(errno.EIO, "controlled post-replace callback failure")
    last = path.parent / "last-run.json"
    if metadata == "both_unavailable":
        original_init = publication.Publication.__init__

        def init(self, db, *args, **kwargs):
            db.conn = WriteFailureConnection(db.conn, lambda: True)
            return original_init(self, db, *args, **kwargs)

        monkeypatch.setattr(publication.Publication, "__init__", init)

    def writer(target, value):
        if (
            metadata == "both_unavailable"
            and Path(target).name == "publication-attempt.json"
        ):
            raise OSError(errno.ENOSPC, "controlled metadata unavailable")
        result = original_writer(target, value)
        if not CANONICAL and Path(target) == last:
            # Portable has no shared directory fsync. This is an explicit
            # failed callback after its actual atomic writer returned.
            raise wanted
        return result

    monkeypatch.setattr(cli, "atomic_json", writer)
    if CANONICAL:
        replaced, synced = system_replace, os.fsync
        armed = [False]

        def replace(source, target):
            value = replaced(source, target)
            if Path(target) == last:
                armed[0] = True
            return value

        def sync(fd):
            if armed[0] and stat.S_ISDIR(os.fstat(fd).st_mode):
                armed[0] = False
                raise wanted
            return synced(fd)

        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(OSError) as raised:
        call(fast=fast)
    assert raised.value is wanted
    visible = json.loads(last.read_text())
    view = read()  # actual native/source/history comparisons in the carried helper
    assert visible["run_id"] > compact["run_id"]
    assert view["last_successful_invocation_run_id"] is None
    assert view["status"] == ("failed" if metadata == "available" else "unresolved")
    assert file_set(tmp_path / "exports") == old_full
    assert view["last_successful_full_export"]["run_id"] == full["run_id"]
    if metadata == "available":
        phase = view["latest_attempt"]["phases"]["last_run"]
        assert phase["status"] == "failed"
        assert phase["error"] == {"type": "OSError", "errno": errno.EIO}
        assert view["latest_attempt"]["mode"] == ("skipped_fast" if fast else "full")
    else:
        assert view["latest_attempt"] is None
    exported = view["last_successful_export"]
    assert exported["phases"]["export"]["format"] == "compact"
    if fast or metadata == "both_unavailable":
        assert exported == old_receipt
    else:
        assert exported["run_id"] == visible["run_id"]
        assert (
            exported["phases"]["export"]["receipt"]["snapshot_latest_run_id"]
            == visible["run_id"]
        )


@pytest.mark.parametrize("fast", [False, True])
def test_before_replace_error_preserves_old_pointer_and_compact_format(
    tmp_path, monkeypatch, fast
):
    call, read, path, _, choice = configured_scenario(tmp_path, monkeypatch)
    choice["mode"] = "compact"
    prior = call()
    last = path.parent / "last-run.json"
    old = last.read_bytes()
    original = cli.atomic_json
    wanted = OSError(errno.ENOSPC, "controlled before-replace failure")

    def fail(target, value):
        if Path(target) == last:
            raise wanted
        return original(target, value)

    monkeypatch.setattr(cli, "atomic_json", fail)
    with pytest.raises(OSError) as raised:
        call(fast=fast)
    assert raised.value is wanted and last.read_bytes() == old
    view = read()
    assert view["status"] == "failed"
    assert view["last_successful_invocation_run_id"] == prior["run_id"]
    assert view["export_format"] == "compact"


@pytest.mark.parametrize("corrupt", [False, True])
def test_inprogress_or_invalid_receipt_with_visible_current_pointer_stays_unknown(
    tmp_path, monkeypatch, corrupt
):
    call, _, path, _, choice = configured_scenario(tmp_path, monkeypatch)
    choice["mode"] = "compact"
    current = call()
    doc = json.loads((path.parent / "publication-attempt.json").read_text())
    for key in ["latest", "last_full"]:
        phase = doc[key]["phases"]["last_run"]
        phase.update(status="inprogress", finished_at=None)
    if corrupt:
        for key in ["latest", "last_full", "last_export"]:
            doc[key]["phases"]["export"]["receipt"]["database_key"] = "0" * 64
    db = Database(path)
    try:
        db.conn.execute(
            "UPDATE monitor_state SET value=? WHERE key=?",
            (json.dumps(doc), publication.KEY),
        )
        db.conn.commit()
        publication.atomic_json(path.parent / "publication-attempt.json", doc)
        assert (
            json.loads((path.parent / "last-run.json").read_text())["run_id"]
            == current["run_id"]
        )
        view = publication.report(db)
        assert view["status"] == "unresolved"
        assert view["last_successful_invocation_run_id"] is None
        if not corrupt:
            assert (
                view["latest_attempt"]["phases"]["last_run"]["status"] == "inprogress"
            )
            assert view["export_format"] == "compact"
    finally:
        db.conn.close()


def test_compact_receipt_survives_metadata_directory_sync_failure(
    tmp_path, monkeypatch
):
    if not CANONICAL:
        pytest.skip("Actual COMMON directory fsync belongs to canonical ABI")
    call, read, _path, _, choice = configured_scenario(tmp_path, monkeypatch)
    choice["mode"] = "compact"
    replaced, synced = system_replace, os.fsync
    armed = [False]
    metadata_errors = []

    def replace(source, target):
        value = replaced(source, target)
        if Path(target).name == "publication-attempt.json":
            armed[0] = True
        return value

    def sync(fd):
        if armed[0] and stat.S_ISDIR(os.fstat(fd).st_mode):
            armed[0] = False
            metadata_errors.append(errno.EIO)
            raise OSError(errno.EIO, "controlled metadata directory sync failure")
        return synced(fd)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", sync)
    result = call()
    assert metadata_errors
    view = read()
    assert view["status"] == "success"
    assert view["export_format"] == "compact"
    assert view["last_successful_invocation_run_id"] == result["run_id"]
    receipt = view["last_successful_export"]["phases"]["export"]["receipt"]
    assert receipt == json.loads(
        (tmp_path / "exports/compact/export-manifest.json").read_text()
    )
    assert receipt["snapshot_latest_run_id"] == result["run_id"]


def test_namespace_refusal_keeps_full_receipt_and_manual_oracle(tmp_path, monkeypatch):
    call, read, path, _, choice = configured_scenario(tmp_path, monkeypatch)
    full = call()
    root = tmp_path / "exports"
    old_full = file_set(root)
    (root / "compact").mkdir()
    (root / "compact/events.csv").symlink_to(root / "price-history.csv")
    choice["mode"] = "compact"
    with pytest.raises(ValueError, match="alias"):
        call()
    view = read()
    assert view["status"] == "failed"
    assert view["latest_attempt"]["phases"]["export"]["format"] == "compact"
    assert view["last_successful_full_export"]["run_id"] == full["run_id"]
    assert view["export_format"] == "full"
    assert view["last_successful_invocation_run_id"] == full["run_id"]
    assert file_set(root) == old_full
    (root / "compact/events.csv").unlink()  # only the control's own injected symlink
    db = Database(path)
    try:
        before = list(db.conn.iterdump())
        db.export(root, mode="compact")
        assert (
            list(db.conn.iterdump()) == before
        )  # includes complete failure diagnostics
        assert publication.report(db)["status"] == "failed"
    finally:
        db.conn.close()
