"""Failed full passes must not amplify network traffic or storage growth."""

import json
import signal
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from car_market import cli, portable
from car_market.db import Database
from car_market.full_schedule import KEY, record_attempt, remaining

STAMP = datetime(2026, 10, 11, 8, tzinfo=UTC)
CONFIG = {"interval_hours": 6, "queries": [{"source": "s", "key": "all"}]}


def test_failure_restart_keeps_attempt_window_and_native_history(tmp_path):
    path = tmp_path / "market.sqlite3"
    db = Database(path)
    record_attempt(db.conn, CONFIG, STAMP.isoformat())
    run = db.start_run("live", STAMP.isoformat())
    db.finish_run(run, "interrupted", {"error": "native contract error"})
    rows = db.conn.execute("SELECT * FROM runs").fetchall()
    db.conn.close()
    with sqlite3.connect(path) as conn:
        assert (
            remaining(
                conn, CONFIG, clock=STAMP + timedelta(minutes=30), missing_queries=True
            )
            == 19800
        )
        assert remaining(conn, CONFIG, clock=STAMP + timedelta(hours=6)) == 0
        assert (
            remaining(
                conn,
                {**CONFIG, "interval_hours": 24},
                clock=STAMP + timedelta(minutes=30),
            )
            == 84600
        )
        assert [tuple(r) for r in rows] == conn.execute("SELECT * FROM runs").fetchall()


def test_new_query_gets_one_immediate_attempt_then_failure_is_bounded(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    record_attempt(db.conn, CONFIG, STAMP.isoformat())
    changed = {**CONFIG, "queries": CONFIG["queries"] + [{"source": "s", "key": "new"}]}
    assert remaining(db.conn, changed, clock=STAMP) == 0
    record_attempt(db.conn, changed, STAMP.isoformat())
    assert remaining(db.conn, changed, clock=STAMP, missing_queries=True) == 21600
    db.conn.close()


@pytest.mark.parametrize(
    "bad",
    [
        "bad json",
        '"string"',
        '{"started_at":"bad"}',
        json.dumps({"started_at": STAMP.isoformat()}),
        json.dumps({"started_at": STAMP.isoformat(), "query_signature": 1}),
        json.dumps({"started_at": STAMP.isoformat(), "query_signature": "bad"}),
    ],
)
def test_corrupt_state_falls_back_to_interrupted_run(tmp_path, bad):
    db = Database(tmp_path / "market.sqlite3")
    record_attempt(db.conn, CONFIG, STAMP.isoformat())
    run = db.start_run("live", STAMP.isoformat())
    db.finish_run(run, "interrupted", {})
    db.conn.execute("UPDATE monitor_state SET value=? WHERE key=?", (bad, KEY))
    assert remaining(db.conn, CONFIG, clock=STAMP + timedelta(hours=1)) == 18000
    db.conn.close()


def test_legacy_daemon_failure_switches_to_fast_without_hiding_error(
    tmp_path, monkeypatch
):
    path = tmp_path / "market.sqlite3"
    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps({**CONFIG, "fast_sources": ["s"]}))
    monkeypatch.setattr(
        sys, "argv", ["car-market", "--db", str(path), "--config", str(cfg), "daemon"]
    )
    ticks = [0.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(
        cli.time, "sleep", lambda seconds: ticks.__setitem__(0, ticks[0] + seconds)
    )
    calls = []

    def collect(args, config):
        calls.append((args.fast, args.sources))
        if len(calls) == 1:
            db = Database(path)
            record_attempt(db.conn, config, datetime.now(UTC).isoformat())
            db.conn.close()
            raise RuntimeError("native parse failed")
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "collect_once", collect)
    with pytest.raises(KeyboardInterrupt):
        cli.main()
    assert calls == [(False, None), (True, ["s"])]


def test_legacy_automatic_pass_rechecks_competing_full_attempt_under_lock(
    tmp_path, monkeypatch
):
    from car_market.infra_support import FileLock

    path = tmp_path / "market.sqlite3"
    db = Database(path)
    config = {**CONFIG, "scope": {"country": "DE"}, "fast_sources": ["s"]}
    args = SimpleNamespace(
        db=str(path),
        command="daemon",
        fast=False,
        full_sources=None,
        sources=None,
        browser=False,
        no_details=True,
        no_rdw=True,
        export_dir=str(tmp_path / "exports"),
    )
    with FileLock(Path(str(path) + ".lock"), blocking=False):
        assert cli.collect_once(args, config) == {"status": "already_running"}
        record_attempt(db.conn, config, datetime.now(UTC).isoformat())
    stored = db.conn.execute(
        "SELECT value FROM monitor_state WHERE key=?", (KEY,)
    ).fetchone()[0]
    db.conn.close()
    calls = []

    class Worker:
        def __init__(self, db, *args, **kwargs):
            self.db = db

        def run(self, sources, details, checks, *, fast):
            calls.append((sources, fast))
            run = self.db.start_run("fast" if fast else "live")
            result = {"status": "partial", "run_id": run}
            self.db.finish_run(run, "partial", result)
            return result

    monkeypatch.setattr(cli, "Collector", Worker)
    monkeypatch.setattr(
        "car_market.opportunities.publish", lambda *args, **kwargs: None
    )
    cli.collect_once(args, config)
    assert calls == [(["s"], True)]
    assert not (tmp_path / "exports").exists()
    db = Database(path)
    assert (
        db.conn.execute(
            "SELECT value FROM monitor_state WHERE key=?", (KEY,)
        ).fetchone()[0]
        == stored
    )
    db.conn.close()


def test_running_legacy_daemon_reloads_query_schedule(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite3"
    db = Database(path)
    record_attempt(db.conn, CONFIG, datetime.now(UTC).isoformat())
    db.conn.close()
    changed = {
        **CONFIG,
        "queries": CONFIG["queries"] + [{"source": "s", "key": "new"}],
        "fast_sources": ["s"],
    }
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _: changed if calls else {**CONFIG, "fast_sources": ["s"]},
    )
    monkeypatch.setattr(sys, "argv", ["car-market", "--db", str(path), "daemon"])
    ticks = [0.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(
        cli.time, "sleep", lambda seconds: ticks.__setitem__(0, ticks[0] + seconds)
    )
    calls = []

    def collect(args, config):
        calls.append(args.fast)
        if len(calls) == 1:
            return {"status": "partial"}
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "collect_once", collect)
    with pytest.raises(KeyboardInterrupt):
        cli.main()
    assert calls == [True, False]


def test_portable_daemon_failure_preserves_fast_schedule(tmp_path, monkeypatch, capsys):
    root = tmp_path
    (root / "data").mkdir()
    path = root / "data/market.sqlite3"
    db = Database(path)
    db.conn.close()
    config = {**CONFIG, "fast_interval_minutes": 1}
    (root / "search.json").write_text(json.dumps(config))
    args = SimpleNamespace(config=None)
    ticks = [0.0]
    handlers = {}
    monkeypatch.setattr(portable, "load", lambda _: config)
    monkeypatch.setattr(portable, "preflight", lambda *_: None)
    monkeypatch.setattr(portable.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(
        portable.signal,
        "signal",
        lambda sig, handler: handlers.setdefault(sig, handler),
    )
    monkeypatch.setattr(
        portable.threading.Event,
        "wait",
        lambda self, seconds: ticks.__setitem__(0, ticks[0] + seconds) or self.is_set(),
    )
    monkeypatch.setattr(portable.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(portable.threading.Thread, "join", lambda self, **kwargs: None)
    calls = []

    def collect(args, root, config, db_path, *, fast, stopped, automatic):
        assert automatic
        calls.append(fast)
        if len(calls) == 1:
            db = Database(path)
            record_attempt(db.conn, config, datetime.now(UTC).isoformat())
            db.conn.close()
            raise RuntimeError("native parse failed")
        handlers[signal.SIGTERM](None, None)
        result = {"status": "partial", "kind": "fast"}
        # Real portable.collect publishes last-run itself before returning.
        portable.atomic_json(root / "data/last-run.json", result)
        return result

    monkeypatch.setattr(portable, "collect", collect)
    portable.daemon(args, root, config, path)
    assert calls == [False, True]
    assert json.loads((root / "data/last-run.json").read_text())["kind"] == "fast"
    assert '"status": "error"' in capsys.readouterr().out


def test_portable_automatic_pass_rechecks_competing_attempt(tmp_path, monkeypatch):
    from car_market import configured_collect
    from car_market.infra_support import FileLock

    assert (
        portable.main(
            [
                "--workspace",
                str(tmp_path),
                "init",
                "--make",
                "Toyota",
                "--model",
                "Corolla",
            ]
        )
        == 0
    )
    config = json.loads((tmp_path / "search.json").read_text())
    path = tmp_path / "data/market.sqlite3"
    args = SimpleNamespace(sources=None, browser=False, no_details=True)
    with FileLock(Path(str(path) + ".lock"), blocking=False):
        with pytest.raises(BlockingIOError):
            portable.collect(args, tmp_path, config, path, automatic=True)
        db = Database(path)
        record_attempt(db.conn, config, datetime.now(UTC).isoformat())
        stored = db.conn.execute(
            "SELECT value FROM monitor_state WHERE key=?", (KEY,)
        ).fetchone()[0]
        db.conn.close()
    calls = []

    def collect(self, sources, details, checks, *, fast):
        calls.append(fast)
        run = self.db.start_run("fast" if fast else "live")
        result = {"status": "partial", "run_id": run}
        self.db.finish_run(run, "partial", result)
        return result

    monkeypatch.setattr(configured_collect.ConfiguredCollector, "run", collect)
    result = portable.collect(args, tmp_path, config, path, automatic=True, fast=False)
    assert calls == [True] and result["kind"] == "fast"
    assert not (tmp_path / "exports/market.sqlite3").exists()
    db = Database(path)
    assert (
        db.conn.execute(
            "SELECT value FROM monitor_state WHERE key=?", (KEY,)
        ).fetchone()[0]
        == stored
    )
    db.conn.close()


def test_running_portable_daemon_reloads_query_schedule(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    path = tmp_path / "data/market.sqlite3"
    config = {**CONFIG, "fast_interval_minutes": 1}
    changed = {**config, "queries": CONFIG["queries"] + [{"source": "s", "key": "new"}]}
    db = Database(path)
    record_attempt(db.conn, config, datetime.now(UTC).isoformat())
    db.conn.close()
    args = SimpleNamespace(config=None)
    calls = []
    ticks = [0.0]
    handlers = {}
    monkeypatch.setattr(portable, "load", lambda _: changed if calls else config)
    monkeypatch.setattr(portable, "preflight", lambda *_: None)
    monkeypatch.setattr(portable.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(
        portable.signal,
        "signal",
        lambda sig, handler: handlers.setdefault(sig, handler),
    )
    monkeypatch.setattr(
        portable.threading.Event,
        "wait",
        lambda self, seconds: ticks.__setitem__(0, ticks[0] + seconds) or self.is_set(),
    )
    monkeypatch.setattr(portable.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(portable.threading.Thread, "join", lambda self, **kwargs: None)

    def collect(args, root, config, db_path, *, fast, stopped, automatic):
        assert automatic
        calls.append(fast)
        if len(calls) == 2:
            handlers[signal.SIGTERM](None, None)
        return {"status": "partial", "kind": "fast" if fast else "full"}

    monkeypatch.setattr(portable, "collect", collect)
    portable.daemon(args, tmp_path, config, path)
    assert calls == [True, False]
