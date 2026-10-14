"""Schema-seven rollout and bounded actual-origin refusal lookup on real SQLite."""

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from car_market.bmw_detail_policy import BMWDetailPolicy, DetailDeferred
from car_market.db import Database
from car_market.germany_dealer_inventory import WIVEDA_SOURCE
from car_market.germany_mobile import DEALER_SOURCE
from car_market.provenance import timestamp

SPEC = importlib.util.spec_from_file_location(
    "blocked_index_native_setup",
    Path(__file__).with_name("test_bmw_dedicated_policy.py"),
)
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)
GLOBAL_INDEX = "fetch_blocked_actual_time"
SOURCE_INDEX = "fetch_source_blocked_actual_time"


def row_history(conn):
    tables = [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    return {
        table: [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
        for table in tables
    }


def history_sha(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def legacy_six(tmp_path, monkeypatch):
    db, collector, _, queries, *_ = native.setup_native(
        tmp_path, monkeypatch, [DEALER_SOURCE, WIVEDA_SOURCE]
    )
    for query in queries:
        native.collect(collector, query)
    run = db.start_run("injected-redirect-refusal")
    denied = db.save_fetch(
        run,
        DEALER_SOURCE,
        "https://auto-straussberger.de/injected-redirect",
        "HTTPS://WIVEDA.DE:0443?native=1#refused",
        429,
        "Injected refusal; native inventory and accepted details remain unchanged",
        "2026-10-08T07:29:59+02:00",
        "offline",
        "http_429",
    )
    # Reconstruct the exact v6 index set; no production database is opened.
    db.conn.execute(f"DROP INDEX {GLOBAL_INDEX}")
    db.conn.execute("PRAGMA user_version=6")
    db.conn.commit()
    before = row_history(db.conn)
    path = db.path
    db.conn.close()
    return path, before, denied


def reader_policy(reader):
    return BMWDetailPolicy(
        SimpleNamespace(db=reader, config={"detail_error_backoff_hours": 6}), None
    )


def test_fresh_database_has_seven_and_both_blocked_indexes(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 7
        indexes = {row[1] for row in db.conn.execute("PRAGMA index_list(fetches)")}
        assert {GLOBAL_INDEX, SOURCE_INDEX} <= indexes
        assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()
        assert db.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        db.conn.close()


def test_six_upgrade_and_reopen_preserve_all_native_rows(tmp_path, monkeypatch):
    path, before, denied = legacy_six(tmp_path, monkeypatch)
    for _ in range(2):
        db = Database(path)
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert row_history(db.conn) == before
        assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()
        assert db.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        db.conn.close()
    reader = Database.read_only(path)
    try:
        with pytest.raises(DetailDeferred) as error:
            reader_policy(reader)._host_backoff(
                WIVEDA_SOURCE, "https://wiveda.de/next", timestamp(native.CLOCK)
            )
        assert error.value.capture["fetch_id"] == denied
        assert row_history(reader.conn) == before
    finally:
        reader.conn.close()


def test_readonly_six_never_migrates_or_rewrites_native_history(tmp_path, monkeypatch):
    path, before, denied = legacy_six(tmp_path, monkeypatch)
    file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    reader = Database.read_only(path)
    try:
        assert reader.conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert not reader.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (GLOBAL_INDEX,)
        ).fetchone()
        with pytest.raises(DetailDeferred) as error:
            reader_policy(reader)._host_backoff(
                WIVEDA_SOURCE, "https://wiveda.de/next", timestamp(native.CLOCK)
            )
        assert error.value.capture["fetch_id"] == denied
        assert row_history(reader.conn) == before
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.conn.execute("CREATE INDEX forbidden ON fetches(status)")
    finally:
        reader.conn.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == file_sha


@pytest.mark.parametrize("failure_stage", ["index", "version", "commit"])
def test_failed_seven_migration_rolls_back_index_version_and_every_row(
    tmp_path, monkeypatch, failure_stage
):
    path, before, _ = legacy_six(tmp_path, monkeypatch)
    connect = sqlite3.connect
    connections = []

    def guarded_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)

        def authorize(action, arg1, arg2, _database, _trigger):
            reject = (
                (
                    failure_stage == "index"
                    and action == sqlite3.SQLITE_CREATE_INDEX
                    and arg1 == GLOBAL_INDEX
                )
                or (
                    failure_stage == "version"
                    and action == sqlite3.SQLITE_PRAGMA
                    and arg1 == "user_version"
                    and arg2 == "7"
                )
                or (
                    failure_stage == "commit"
                    and action == sqlite3.SQLITE_TRANSACTION
                    and arg1 == "COMMIT"
                )
            )
            return sqlite3.SQLITE_DENY if reject else sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        connections.append(conn)
        return conn

    with monkeypatch.context() as patcher:
        patcher.setattr("car_market.db.sqlite3.connect", guarded_connect)
        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            Database(path)
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    reader = Database.read_only(path)
    try:
        assert reader.conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert not reader.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (GLOBAL_INDEX,)
        ).fetchone()
        assert row_history(reader.conn) == before
        assert reader.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        reader.conn.close()
    # A failed attempt must not prevent a subsequent genuine migration.
    recovered = Database(path)
    assert recovered.conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert row_history(recovered.conn) == before
    recovered.conn.close()


def measured_lookup(reader, *, matching):
    statements, ticks = [], [0]

    def progress():
        ticks[0] += 1
        return 0

    reader.conn.set_trace_callback(statements.append)
    reader.conn.set_progress_handler(progress, 10)
    try:
        policy = reader_policy(reader)
        if matching:
            with pytest.raises(DetailDeferred) as error:
                policy._host_backoff(
                    WIVEDA_SOURCE, "https://wiveda.de/next", timestamp(native.CLOCK)
                )
            selected = error.value.capture["fetch_id"]
        else:
            policy._host_backoff(WIVEDA_SOURCE, "https://wiveda.de/next", timestamp(native.CLOCK))
            selected = None
    finally:
        reader.conn.set_progress_handler(None, 0)
        reader.conn.set_trace_callback(None)
    query = next(sql for sql in statements if sql.startswith("SELECT * FROM fetches WHERE status"))
    plan = [row[3] for row in reader.conn.execute("EXPLAIN QUERY PLAN " + query)]
    assert any("SEARCH fetches USING INDEX " + GLOBAL_INDEX in step for step in plan), plan
    assert not any("SCAN" in step or "TEMP B-TREE" in step for step in plan), plan
    assert ticks[0] * 10 < 2000
    return {
        "vm_step_upper_bound": ticks[0] * 10 + 9,
        "plan": plan,
        "selected": selected,
    }


def scaling_pair(tmp_path, *, matching):
    path = tmp_path / "market.sqlite3"
    db = Database(path)
    db.conn.executemany(
        "INSERT INTO fetches(source,url,final_url,fetched_at,status) VALUES (?,?,?,?,200)",
        [
            (
                WIVEDA_SOURCE,
                "https://wiveda.de/ok/" + str(i),
                "https://wiveda.de",
                native.CLOCK,
            )
            for i in range(15000)
        ],
    )
    current = [
        (DEALER_SOURCE, "https://wiveda.de", "2026-10-08T05:29:58Z", 403),
        (
            DEALER_SOURCE,
            "https://WIVEDA.DE:0443?x=1#blocked" if matching else "https://wiveda.de:8443",
            "2026-10-08T07:29:59+02:00",
            429,
        ),
        (WIVEDA_SOURCE, "https://foreign.example", native.CLOCK, 403),
        (WIVEDA_SOURCE, "https://wiveda.de:invalid", native.CLOCK, 429),
        (DEALER_SOURCE, "https://wiveda.de", "2026-10-08T05:30:01Z", 403),
        (DEALER_SOURCE, "https://wiveda.de", "2026-10-07T23:30:00Z", 429),
        (DEALER_SOURCE, "https://wiveda.de", "malformed-clock", 403),
    ]
    if not matching:
        current[0] = (DEALER_SOURCE, "http://wiveda.de", current[0][2], 403)
    db.conn.executemany(
        "INSERT INTO fetches(source,url,final_url,fetched_at,status) VALUES (?,'https://original.example/request',?,?,?)",
        current,
    )
    newest = db.conn.execute(
        "SELECT id FROM fetches WHERE status=429 ORDER BY id LIMIT 1"
    ).fetchone()[0]
    db.conn.commit()
    db.conn.close()
    measured = []
    for expired_count in [0, 12000]:
        if expired_count:
            writer = Database(path)
            writer.conn.executemany(
                "INSERT INTO fetches(source,url,final_url,fetched_at,status) VALUES (?,'https://original.example/stale',?,?,?)",
                [
                    (
                        DEALER_SOURCE if i % 2 else WIVEDA_SOURCE,
                        "https://wiveda.de",
                        "2026-10-07T00:00:00Z" if i % 2 else "2026-10-07T02:00:00+02:00",
                        403 if i % 2 else 429,
                    )
                    for i in range(expired_count)
                ],
            )
            writer.conn.commit()
            writer.conn.close()
        reader = Database.read_only(path)
        try:
            before = history_sha(row_history(reader.conn))
            result = measured_lookup(reader, matching=matching)
            assert result["selected"] == (newest if matching else None)
            assert history_sha(row_history(reader.conn)) == before
            measured.append({"expired_refusals": expired_count, **result})
        finally:
            reader.conn.close()
    assert abs(measured[1]["vm_step_upper_bound"] - measured[0]["vm_step_upper_bound"]) <= 100
    return {"successful_history": 15000, "matching": matching, "measurements": measured}


@pytest.mark.parametrize("matching", [True, False])
def test_readonly_global_lookup_cost_is_independent_of_expired_failures(tmp_path, matching):
    scaling_pair(tmp_path, matching=matching)
