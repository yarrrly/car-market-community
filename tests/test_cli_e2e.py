"""CLI integration: local HTTP → parser → SQLite → export and repeat collection."""

import csv
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from car_market.infra_support import FileLock

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def market(tmp_path):
    state = {"price": 8500, "blocked": False, "calls": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state["calls"].append(self.path)
            page = 2 if "page=2" in self.path else 1
            if state["blocked"] and page == 2:
                self.send_response(429)
                self.end_headers()
                return
            listing = {
                "id": str(page),
                "url": "/aanbod/" + str(page),
                "vehicle": {
                    "motorTypeName": "218i",
                    "variant": "Active Tourer",
                    "fuel": "Benzine",
                    "transmission": "Handgeschakeld" if page == 1 else "Automaat",
                    "mileageInKm": 120000,
                },
                "price": {"priceRaw": state["price"] if page == 1 else 22000},
            }
            body = (
                '<script type="application/json">'
                + json.dumps(
                    {
                        "props": {
                            "pageProps": {
                                "numberOfResults": 2,
                                "numberOfPages": 2,
                                "pageQuery": {"page": page},
                                "listings": [listing],
                            }
                        }
                    }
                )
                + "</script>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "sources.json"
    config.write_text(
        json.dumps(
            {
                "workers": 1,
                "queries": [
                    {
                        "source": "autoscout24",
                        "key": "all",
                        "url": f"http://127.0.0.1:{server.server_port}/lst",
                        "pagination": "page",
                        "coverage": "enumerate",
                    }
                ],
            }
        )
    )
    db = tmp_path / "data/market.sqlite3"
    exports = tmp_path / "exports"

    def command(*args, success=True):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "car_market.cli",
                "--db",
                str(db),
                "--config",
                str(config),
                "--export-dir",
                str(exports),
                *args,
            ],
            cwd=ROOT,
            env={**os.environ, "E2E_TELEMETRY_DISABLED": "1"},
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if success:
            assert result.returncode == 0, result.stderr
        return result

    def query(sql):
        return json.loads(command("query", sql).stdout)

    yield state, db, exports, command, query
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def collect(command):
    return command("collect", "--no-details", "--no-rdw")


def test_cli_collect_paginates_and_exports_all_prices_and_gears(market):
    state, db, exports, command, query = market
    collect(command)
    assert state["calls"] == ["/lst", "/lst?page=2"]
    assert query("SELECT status,received_count,pages FROM source_health") == [
        {"status": "complete", "received_count": 2, "pages": 2}
    ]
    assert query("SELECT price_eur,transmission FROM market_current ORDER BY price_eur") == [
        {"price_eur": 8500.0, "transmission": "manual"},
        {"price_eur": 22000.0, "transmission": "automatic"},
    ]
    assert json.loads(command("status").stdout)["purchase_candidates"] == 1
    with (exports / "market-current.csv").open(newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 2
    with sqlite3.connect(exports / "market.sqlite3") as backup:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
    assert db.exists()


def test_cli_repeat_and_price_change_preserve_history(market):
    state, _, _, command, query = market
    collect(command)
    collect(command)
    assert query("SELECT COUNT(*) AS n FROM observations") == [{"n": 2}]
    state["price"] = 8000
    collect(command)
    assert query("SELECT COUNT(*) AS n FROM observations") == [{"n": 3}]
    assert query(
        "SELECT CAST(previous_value AS REAL) AS previous_value,CAST(current_value AS REAL) AS current_value FROM events WHERE kind='price_eur_changed'"
    ) == [{"previous_value": 8500.0, "current_value": 8000.0}]
    assert query("SELECT COUNT(*) AS n FROM query_membership WHERE missed_complete_runs<>0") == [
        {"n": 0}
    ]


def test_cli_failed_pagination_keeps_existing_inventory(market):
    state, _, _, command, query = market
    collect(command)
    state["blocked"] = True
    state["calls"].clear()
    collect(command)
    assert state["calls"] == ["/lst", "/lst?page=2"]
    assert query("SELECT status FROM source_health") == [{"status": "partial"}]
    assert query("SELECT COUNT(*) AS n FROM listings WHERE availability='observed'") == [{"n": 2}]
    assert query("SELECT COUNT(*) AS n FROM events WHERE kind='not_seen_in_complete_query'") == [
        {"n": 0}
    ]


def test_cli_query_rejects_mutation(market):
    _, _, _, command, query = market
    collect(command)
    result = command("query", "DELETE FROM listings", success=False)
    assert result.returncode != 0
    assert "readonly" in result.stderr
    assert query("SELECT COUNT(*) AS n FROM listings") == [{"n": 2}]


def test_cli_busy_writer_does_not_fetch_or_change_runs(market):
    state, db, _, command, query = market
    collect(command)
    before = query("SELECT COUNT(*) AS n FROM runs")
    state["calls"].clear()
    with FileLock(Path(str(db) + ".lock"), blocking=False):
        assert json.loads(collect(command).stdout) == {"status": "already_running"}
    assert state["calls"] == []
    assert query("SELECT COUNT(*) AS n FROM runs") == before


def test_cli_resumes_interrupted_pass_without_losing_observations(market):
    _, db, _, command, query = market
    collect(command)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO runs(started_at,kind,status) VALUES ('2026-10-05T00:00:00+00:00','live','running')"
        )
    collect(command)
    assert query(
        "SELECT COUNT(*) AS n FROM runs WHERE status='interrupted' AND finished_at IS NOT NULL"
    ) == [{"n": 1}]
    assert query("SELECT COUNT(*) AS n FROM observations") == [{"n": 2}]


def test_export_includes_uncheckpointed_wal(tmp_path):
    from car_market.db import Database
    from car_market.parse import Listing

    db = Database(tmp_path / "data/market.sqlite3")
    try:
        db.conn.execute("PRAGMA wal_autocheckpoint=0")
        run = db.start_run()
        db.observe(
            run,
            Listing(
                "fixture",
                "a",
                "https://example.test/a",
                "BMW 218i Active Tourer",
                fuel="petrol",
                price=8500,
            ).qualify(),
            "2026-10-05T08:00:00+00:00",
        )
        assert Path(str(db.path) + "-wal").stat().st_size > 0
        db.export(tmp_path / "exports")
        with sqlite3.connect(tmp_path / "exports/market.sqlite3") as backup:
            assert backup.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.conn.close()


@pytest.mark.parametrize("include_raw", [False, True])
def test_cli_snapshot_import_preserves_price_history(market, tmp_path, include_raw):
    state, database, exports, command, _ = market
    collect(command)
    state["price"] = 8000
    collect(command)
    if include_raw:
        shutil.copytree(database.parent / "raw", exports / "raw")
    destination = tmp_path / "imported/market.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "car_market.cli",
            "--db",
            str(destination),
            "--export-dir",
            str(tmp_path / "imported-exports"),
            "import-db",
            str(exports / "market.sqlite3"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3
        assert conn.execute(
            "SELECT price_eur FROM observations WHERE listing_id=(SELECT id FROM listings WHERE external_id='1') ORDER BY observed_at,id"
        ).fetchall() == [(8500.0,), (8000.0,)]
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE kind='price_eur_changed'").fetchone()[0]
            == 1
        )
        with sqlite3.connect(exports / "market.sqlite3") as source:
            assert (
                conn.execute("SELECT content_hash FROM fetches ORDER BY id").fetchall()
                == source.execute("SELECT content_hash FROM fetches ORDER BY id").fetchall()
            )
        assert conn.execute("SELECT COUNT(*) FROM fetches WHERE raw_path IS NOT NULL").fetchone()[
            0
        ] == (3 if include_raw else 0)


@pytest.mark.parametrize("include_raw", [False, True])
@pytest.mark.parametrize("partial_update", [False, True])
def test_cli_repeated_snapshot_import_deduplicates_history(
    market, tmp_path, include_raw, partial_update
):
    state, database, exports, command, _ = market
    config_path = tmp_path / "sources.json"
    config = json.loads(config_path.read_text())
    config["queries"][0]["key"] = "218i_all"
    config_path.write_text(json.dumps(config))
    collect(command)
    state["price"] = 8000
    collect(command)
    snapshot = tmp_path / "snapshot/market.sqlite3"
    snapshot.parent.mkdir()
    shutil.copy2(exports / "market.sqlite3", snapshot)
    if include_raw:
        shutil.copytree(database.parent / "raw", snapshot.parent / "raw")
    source_bytes = snapshot.read_bytes()
    destination = tmp_path / "imported/market.sqlite3"
    destination_exports = tmp_path / "imported-exports"

    def import_snapshot():
        command(
            "--db",
            str(destination),
            "--export-dir",
            str(destination_exports),
            "import-db",
            str(snapshot),
        )

    import_snapshot()
    with sqlite3.connect(destination) as conn:
        observations = conn.execute("SELECT * FROM observations ORDER BY id").fetchall()
        events = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        listings = conn.execute("SELECT * FROM listings ORDER BY id").fetchall()
        vehicles = conn.execute("SELECT * FROM vehicles ORDER BY id").fetchall()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind='not_seen_in_complete_query'"
            ).fetchone()[0]
            == 0
        )
    csv_before = (destination_exports / "market-current.csv").read_bytes()
    raw_before = {p.name: p.read_bytes() for p in (destination.parent / "raw").iterdir()}
    import_snapshot()
    assert snapshot.read_bytes() == source_bytes
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT * FROM observations ORDER BY id").fetchall() == observations
        assert conn.execute("SELECT * FROM events ORDER BY id").fetchall() == events
        assert conn.execute("SELECT * FROM listings ORDER BY id").fetchall() == listings
        assert conn.execute("SELECT * FROM vehicles ORDER BY id").fetchall() == vehicles
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert (destination_exports / "market-current.csv").read_bytes() == csv_before
    assert {p.name: p.read_bytes() for p in (destination.parent / "raw").iterdir()} == raw_before

    # The new snapshot includes prior history and one new price; repeating preserves it.
    state["price"] = 7500
    state["blocked"] = partial_update
    collect(command)
    shutil.copy2(exports / "market.sqlite3", snapshot)
    if include_raw:
        shutil.copytree(database.parent / "raw", snapshot.parent / "raw", dirs_exist_ok=True)
    for _ in range(2):
        import_snapshot()
        with sqlite3.connect(destination) as conn:
            assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 4
            assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 2
            assert conn.execute("SELECT status FROM source_health").fetchall() == [
                ("partial" if partial_update else "complete",)
            ]
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='not_seen_in_complete_query'"
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM listings WHERE availability='observed'"
                ).fetchone()[0]
                == 2
            )
            assert conn.execute(
                "SELECT price_eur FROM observations WHERE listing_id=(SELECT id FROM listings WHERE external_id='1') ORDER BY observed_at,id"
            ).fetchall() == [(8500.0,), (8000.0,), (7500.0,)]
            assert conn.execute(
                "SELECT CAST(previous_value AS REAL),CAST(current_value AS REAL) FROM events WHERE kind='price_eur_changed' ORDER BY id"
            ).fetchall() == [(8500.0, 8000.0), (8000.0, 7500.0)]
            assert (
                conn.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0] == 0
            )
        with sqlite3.connect(destination_exports / "market.sqlite3") as backup:
            assert backup.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 4


@pytest.mark.parametrize("alias", ["same_path", "symlink", "hardlink"])
def test_cli_export_rejects_working_database_as_backup(market, tmp_path, alias):
    _, database, _, command, _ = market
    collect(command)
    target = database.parent if alias == "same_path" else tmp_path / "alias-export"
    target.mkdir(exist_ok=True)
    if alias == "symlink":
        (target / "market.sqlite3").symlink_to(database)
    elif alias == "hardlink":
        os.link(database, target / "market.sqlite3")
    sentinel = target / "market-current.csv"
    sentinel.write_text("previous export\n")
    source_before = database.read_bytes()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "car_market.cli",
                "--db",
                str(database),
                "--export-dir",
                str(target),
                "export",
            ],
            cwd=ROOT,
            env={**os.environ, "E2E_TELEMETRY_DISABLED": "1"},
            text=True,
            capture_output=True,
            check=False,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("Export attempted to back up the working database onto itself and hung")
    assert result.returncode != 0
    assert "Export backup and working database must be different files" in result.stderr
    assert database.read_bytes() == source_before
    assert sentinel.read_text() == "previous export\n"
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("failure", ["csv", "backup"])
def test_cli_import_export_failure_preserves_previous_files_and_can_retry(
    market, tmp_path, failure
):
    state, database, exports, command, _ = market
    config_path = tmp_path / "sources.json"
    config = json.loads(config_path.read_text())
    config["queries"][0]["key"] = "218i_all"
    config_path.write_text(json.dumps(config))
    collect(command)
    snapshot = tmp_path / "snapshot/market.sqlite3"
    snapshot.parent.mkdir()
    shutil.copy2(exports / "market.sqlite3", snapshot)
    shutil.copytree(database.parent / "raw", snapshot.parent / "raw")
    destination = tmp_path / "imported/market.sqlite3"
    destination_exports = tmp_path / "imported-exports"
    args = ["--db", str(destination), "--export-dir", str(destination_exports)]
    command(*args, "import-db", str(snapshot))
    previous = {p.name: p.read_bytes() for p in destination_exports.iterdir()}
    assert len(previous) == 12
    assert {"vehicles.json", "vehicles.csv", "vehicle-id-aliases.csv"} <= previous.keys()

    state["price"] = 8000
    state["blocked"] = True
    collect(command)
    shutil.copy2(exports / "market.sqlite3", snapshot)
    shutil.copytree(database.parent / "raw", snapshot.parent / "raw", dirs_exist_ok=True)
    source_before = {
        str(p.relative_to(snapshot.parent)): p.read_bytes()
        for p in snapshot.parent.rglob("*")
        if p.is_file()
    }
    calls_before = list(state["calls"])
    # Failure after the actual write: the real import has already committed.
    driver = """
import csv
import errno
import os
import sqlite3
from pathlib import Path
from car_market.cli import main

def fail():
    Path(os.environ["FIRSTCAR_FAILURE_MARKER"]).write_text("write_attempted")
    raise OSError(errno.ENOSPC, "fixture export disk full")

writer = csv.writer
class FailingWriter:
    def __init__(self, stream, *args, **kwargs):
        self.writer = writer(stream, *args, **kwargs)
    def writerow(self, row):
        return self.writer.writerow(row)
    def writerows(self, rows):
        first = next(iter(rows), None)
        if first is not None:
            self.writer.writerow(first)
        fail()

def csv_writer(stream, *args, **kwargs):
    if os.environ["FIRSTCAR_EXPORT_FAILURE"] == "csv" and Path(stream.name).name == "source-health.csv":
        return FailingWriter(stream, *args, **kwargs)
    return writer(stream, *args, **kwargs)
csv.writer = csv_writer

connect = sqlite3.connect
class FailingBackup(sqlite3.Connection):
    def backup(self, target, *args, **kwargs):
        super().backup(target, *args, **kwargs)
        fail()

def backup_connect(path, *args, **kwargs):
    if os.environ["FIRSTCAR_EXPORT_FAILURE"] == "backup":
        kwargs["factory"] = FailingBackup
    return connect(path, *args, **kwargs)
sqlite3.connect = backup_connect
main()
"""
    marker = tmp_path / "failure-marker"
    result = subprocess.run(
        [sys.executable, "-c", driver, *args, "import-db", str(snapshot)],
        cwd=ROOT,
        env={
            **os.environ,
            "E2E_TELEMETRY_DISABLED": "1",
            "FIRSTCAR_EXPORT_FAILURE": failure,
            "FIRSTCAR_FAILURE_MARKER": str(marker),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0
    assert "fixture export disk full" in result.stderr
    assert marker.read_text() == "write_attempted"
    assert state["calls"] == calls_before
    assert {
        str(p.relative_to(snapshot.parent)): p.read_bytes()
        for p in snapshot.parent.rglob("*")
        if p.is_file()
    } == source_before
    with FileLock(Path(str(destination) + ".lock"), blocking=False):
        pass
    with sqlite3.connect(destination) as conn:
        committed_observations = conn.execute("SELECT * FROM observations ORDER BY id").fetchall()
        committed_events = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        assert len(committed_observations) == 3
        assert conn.execute("SELECT status FROM source_health").fetchall() == [("partial",)]
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        committed_dump = list(conn.iterdump())
    after_failure = {p.name: p.read_bytes() for p in destination_exports.iterdir() if p.is_file()}
    changed = sorted(name for name in previous if previous[name] != after_failure.get(name))
    assert after_failure == previous, f"Previous export changed after {failure} failure: {changed}"
    assert sorted(p.name for p in destination_exports.iterdir()) == sorted(previous)

    # Exports can be recovered without another import or HTTP request.
    command(*args, "export")
    with sqlite3.connect(destination) as conn:
        assert list(conn.iterdump()) == committed_dump
    with sqlite3.connect(destination_exports / "market.sqlite3") as backup:
        assert list(backup.iterdump()) == committed_dump
    with (destination_exports / "price-history.csv").open(newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 3
    with (destination_exports / "source-health.csv").open(newline="") as stream:
        assert [row["status"] for row in csv.DictReader(stream)] == ["partial"]
    for _ in range(2):
        command(*args, "import-db", str(snapshot))
        with sqlite3.connect(destination) as conn:
            assert (
                conn.execute("SELECT * FROM observations ORDER BY id").fetchall()
                == committed_observations
            )
            assert conn.execute("SELECT * FROM events ORDER BY id").fetchall() == committed_events
            assert (
                conn.execute("SELECT COUNT(*) FROM observations WHERE fetch_id IS NULL").fetchone()[
                    0
                ]
                == 0
            )
            assert conn.execute(
                "SELECT CAST(previous_value AS REAL),CAST(current_value AS REAL) FROM events WHERE kind='price_eur_changed'"
            ).fetchall() == [(8500.0, 8000.0)]
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='not_seen_in_complete_query'"
                ).fetchone()[0]
                == 0
            )
            for digest, relative in conn.execute(
                "SELECT content_hash,raw_path FROM fetches WHERE raw_path IS NOT NULL"
            ):
                assert (
                    hashlib.sha256(
                        gzip.decompress((destination.parent / relative).read_bytes())
                    ).hexdigest()
                    == digest
                )
        with sqlite3.connect(destination_exports / "market.sqlite3") as backup:
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
            assert (
                backup.execute("SELECT * FROM observations ORDER BY id").fetchall()
                == committed_observations
            )
            assert backup.execute("SELECT status FROM source_health").fetchall() == [("partial",)]
        with (destination_exports / "price-history.csv").open(newline="") as stream:
            assert len(list(csv.DictReader(stream))) == 3
        with (destination_exports / "market-current.csv").open(newline="") as stream:
            assert [float(row["price_eur"]) for row in csv.DictReader(stream)] == [8000.0, 22000.0]
    assert state["calls"] == calls_before
    assert {
        str(p.relative_to(snapshot.parent)): p.read_bytes()
        for p in snapshot.parent.rglob("*")
        if p.is_file()
    } == source_before


@pytest.mark.parametrize("writer_boundary", ["observations", "source_runs"])
def test_cli_snapshot_import_reads_one_source_version(market, tmp_path, writer_boundary):
    state, source_path, _, command, _ = market
    config_path = tmp_path / "sources.json"
    config = json.loads(config_path.read_text())
    config["queries"][0]["key"] = "218i_all"
    config_path.write_text(json.dumps(config))
    collect(command)
    with sqlite3.connect(source_path) as source:
        original_observations = source.execute(
            "SELECT observed_at,details_json FROM observations ORDER BY observed_at,id"
        ).fetchall()
        original_fetches = source.execute("SELECT content_hash FROM fetches ORDER BY id").fetchall()

    state["price"] = 7500
    state["blocked"] = True
    destination = tmp_path / "imported/market.sqlite3"
    destination_exports = tmp_path / "imported-exports"
    writer_args = [
        sys.executable,
        "-m",
        "car_market.cli",
        "--db",
        str(source_path),
        "--config",
        str(config_path),
        "--export-dir",
        str(tmp_path / "writer-exports"),
        "collect",
        "--no-details",
        "--no-rdw",
    ]
    # The barrier only controls timing: a second real CLI writes to the source WAL.
    driver = """
import json
import os
import sqlite3
import subprocess
from car_market.cli import main

connect = sqlite3.connect
class SourceConnection(sqlite3.Connection):
    updated = False
    def execute(self, sql, *args, **kwargs):
        boundary = "SELECT * FROM " + os.environ["FIRSTCAR_WRITER_BOUNDARY"]
        if sql.startswith(boundary) and not self.updated:
            self.updated = True
            writer = subprocess.run(
                json.loads(os.environ["FIRSTCAR_WRITER_ARGS"]),
                capture_output=True, text=True, timeout=20, check=False,
            )
            assert writer.returncode == 0, writer.stderr
            print("concurrent_writer_committed", flush=True)
        return super().execute(sql, *args, **kwargs)

def source_connect(path, *args, **kwargs):
    if isinstance(path, str) and "?mode=ro" in path:
        kwargs["factory"] = SourceConnection
    return connect(path, *args, **kwargs)
sqlite3.connect = source_connect
main()
"""
    args = [
        "--db",
        str(destination),
        "--export-dir",
        str(destination_exports),
        "import-db",
        str(source_path),
    ]
    result = subprocess.run(
        [sys.executable, "-c", driver, *args],
        cwd=ROOT,
        env={
            **os.environ,
            "E2E_TELEMETRY_DISABLED": "1",
            "FIRSTCAR_WRITER_BOUNDARY": writer_boundary,
            "FIRSTCAR_WRITER_ARGS": json.dumps(writer_args),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "concurrent_writer_committed" in result.stdout
    assert state["calls"] == ["/lst", "/lst?page=2", "/lst", "/lst?page=2"]
    with sqlite3.connect(source_path) as source:
        assert source.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3
        assert source.execute("SELECT status FROM source_health").fetchall() == [("partial",)]

    with sqlite3.connect(destination) as imported:
        assert (
            imported.execute(
                "SELECT observed_at,details_json FROM observations ORDER BY observed_at,id"
            ).fetchall()
            == original_observations
        )
        assert (
            imported.execute("SELECT content_hash FROM fetches ORDER BY id").fetchall()
            == original_fetches
        )
        assert imported.execute("SELECT status FROM source_health").fetchall() == [("complete",)]
        assert (
            imported.execute("SELECT COUNT(*) FROM observations WHERE fetch_id IS NULL").fetchone()[
                0
            ]
            == 0
        )
        assert (
            imported.execute(
                "SELECT COUNT(*) FROM events WHERE kind='price_eur_changed'"
            ).fetchone()[0]
            == 0
        )
        assert imported.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert imported.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(destination_exports / "market.sqlite3") as backup:
        assert backup.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
        assert backup.execute("SELECT status FROM source_health").fetchall() == [("complete",)]

    # The next import sees the completed update, preserves links and avoids duplicate prices.
    for _ in range(2):
        command(*args)
        with sqlite3.connect(destination) as imported:
            assert imported.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3
            assert imported.execute("SELECT status FROM source_health").fetchall() == [("partial",)]
            assert (
                imported.execute(
                    "SELECT COUNT(*) FROM observations WHERE fetch_id IS NULL"
                ).fetchone()[0]
                == 0
            )
            assert imported.execute(
                "SELECT CAST(previous_value AS REAL),CAST(current_value AS REAL) FROM events WHERE kind='price_eur_changed'"
            ).fetchall() == [(8500.0, 7500.0)]
            for digest, relative in imported.execute(
                "SELECT DISTINCT f.content_hash,f.raw_path FROM observations o JOIN fetches f ON f.id=o.fetch_id"
            ):
                assert relative is not None
                raw = gzip.decompress((destination.parent / relative).read_bytes())
                assert hashlib.sha256(raw).hexdigest() == digest
            assert (
                imported.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='not_seen_in_complete_query'"
                ).fetchone()[0]
                == 0
            )
            assert imported.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert imported.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("existing_raw", [False, True])
def test_cli_snapshot_import_preserves_exact_raw_bytes(market, tmp_path, existing_raw):
    _, database, exports, command, _ = market
    collect(command)
    shutil.copytree(database.parent / "raw", exports / "raw")
    snapshot = exports / "market.sqlite3"
    # Existing archives may contain CRLF and UTF-8; import preserves exact bytes.
    body = "<html>\r\nBMW 218i — €8500\r\n</html>\r\n".encode()
    digest = hashlib.sha256(body).hexdigest()
    raw_path = f"raw/{digest}.html.gz"
    (exports / raw_path).write_bytes(gzip.compress(body))
    with sqlite3.connect(snapshot) as source:
        source.execute(
            "UPDATE fetches SET raw_path=?,content_hash=? WHERE id=1", (raw_path, digest)
        )
        expected = source.execute(
            "SELECT content_hash,raw_path FROM fetches ORDER BY id"
        ).fetchall()
    source_bytes = snapshot.read_bytes()
    destination = tmp_path / "imported/market.sqlite3"
    reused = {}
    if existing_raw:
        (destination.parent / "raw").mkdir(parents=True)
        for _, existing_path in expected:
            existing = destination.parent / existing_path
            existing.write_bytes(
                gzip.compress(gzip.decompress((exports / existing_path).read_bytes()), mtime=1)
            )
            reused[existing_path] = existing.read_bytes()
    imported = command(
        "--db",
        str(destination),
        "--export-dir",
        str(tmp_path / "imported-exports"),
        "import-db",
        str(snapshot),
    )
    assert imported.returncode == 0
    assert snapshot.read_bytes() == source_bytes
    assert all(
        (destination.parent / name).read_bytes() == content for name, content in reused.items()
    )
    with sqlite3.connect(destination) as conn:
        assert (
            conn.execute("SELECT content_hash,raw_path FROM fetches ORDER BY id").fetchall()
            == expected
        )
        for content_hash, imported_path in expected:
            raw = gzip.decompress((destination.parent / imported_path).read_bytes())
            assert hashlib.sha256(raw).hexdigest() == content_hash
            assert raw == gzip.decompress((exports / imported_path).read_bytes())
    assert gzip.decompress((destination.parent / raw_path).read_bytes()) == body


@pytest.mark.parametrize(
    "damage",
    [
        "gzip",
        "truncated_gzip",
        "hash_mismatch",
        "missing_hash",
        "invalid_utf8",
        "destination_hash_mismatch",
        "destination_gzip",
        "destination_truncated_gzip",
        "json",
        "identity",
        "source_table",
        "process_exit",
    ],
)
def test_cli_failed_snapshot_import_rolls_back_and_can_retry(market, tmp_path, damage):
    from car_market.db import Database
    from car_market.parse import Listing

    state, database, exports, command, _ = market
    collect(command)
    state["price"] = 8000
    collect(command)
    shutil.copytree(database.parent / "raw", exports / "raw")
    snapshot = exports / "market.sqlite3"
    destination = tmp_path / "imported/market.sqlite3"
    db = Database(destination)
    run = db.start_run()
    fetch = db.save_fetch(
        run,
        "fixture",
        "https://example.test/existing",
        "https://example.test/existing",
        200,
        "existing evidence",
        "2026-10-04T08:00:00+00:00",
    )
    db.observe(
        run,
        Listing(
            "fixture",
            "existing",
            "https://example.test/existing",
            "BMW 218i Active Tourer",
            price=9000,
            kenteken="GB178T",
            vin="WBA12345678901234",
        ).qualify(),
        "2026-10-04T08:00:00+00:00",
        fetch_id=fetch,
    )
    db.finish_run(run, "complete", db.summary())
    before = list(db.conn.iterdump())
    existing_raw = {p.name: p.read_bytes() for p in db.raw.iterdir()}
    db.conn.close()
    destination_exports = tmp_path / "imported-exports"
    destination_exports.mkdir()
    sentinel = destination_exports / "market-current.csv"
    sentinel.write_text("previous export\n")

    with sqlite3.connect(snapshot) as source:
        if damage.startswith("destination_"):
            relative_raw = source.execute(
                "SELECT raw_path FROM fetches WHERE raw_path IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            collision = destination.parent / relative_raw
            original = (exports / relative_raw).read_bytes()
            broken = {
                "destination_hash_mismatch": gzip.compress(b"stale corrupt destination evidence"),
                "destination_gzip": b"invalid gzip",
                "destination_truncated_gzip": original[:-8],
            }[damage]
            collision.write_bytes(broken)
            existing_raw[collision.name] = broken
        elif damage in ("gzip", "truncated_gzip", "hash_mismatch", "missing_hash", "invalid_utf8"):
            raw = (
                exports
                / source.execute(
                    "SELECT raw_path FROM fetches WHERE raw_path IS NOT NULL ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            )
            original = raw.read_bytes()
            if damage == "missing_hash":
                source.execute(
                    "UPDATE fetches SET content_hash=NULL WHERE raw_path=?",
                    (str(raw.relative_to(exports)),),
                )
            elif damage == "hash_mismatch":
                raw.write_bytes(gzip.compress(b"valid gzip with substituted evidence"))
            elif damage == "invalid_utf8":
                body = b"\xff\xfeinvalid UTF-8"
                raw.write_bytes(gzip.compress(body))
                source.execute(
                    "UPDATE fetches SET content_hash=? WHERE raw_path=?",
                    (hashlib.sha256(body).hexdigest(), str(raw.relative_to(exports))),
                )
            else:
                raw.write_bytes(b"invalid gzip" if damage == "gzip" else original[:-8])
        elif damage == "source_table":
            source.execute("ALTER TABLE source_runs RENAME TO missing_source_runs")
        elif damage != "process_exit":
            observation, original = source.execute(
                "SELECT id,details_json FROM observations ORDER BY id DESC LIMIT 1"
            ).fetchone()
            broken = (
                "{"
                if damage == "json"
                else json.dumps(
                    {**json.loads(original), "kenteken": "GB178T", "vin": "WBA12345678901235"}
                )
            )
            source.execute(
                "UPDATE observations SET details_json=? WHERE id=?", (broken, observation)
            )
    damaged_snapshot = snapshot.read_bytes()
    damaged_raw = {p.name: p.read_bytes() for p in (exports / "raw").iterdir()}
    args = [
        sys.executable,
        "-m",
        "car_market.cli",
        "--db",
        str(destination),
        "--export-dir",
        str(destination_exports),
        "import-db",
        str(snapshot),
    ]
    if damage == "process_exit":
        # Forced exit after the first write bypasses Python finally and rollback.
        crash = """
import os
from car_market.cli import main
from car_market.db import Database
save = Database.save_fetch
def exit_after_fetch(self, *args, **kwargs):
    save(self, *args, **kwargs)
    os._exit(73)
Database.save_fetch = exit_after_fetch
main()
"""
        failed_args = [sys.executable, "-c", crash, *args[3:]]
    else:
        failed_args = args
    failed = subprocess.run(
        failed_args, cwd=ROOT, text=True, capture_output=True, check=False, timeout=30
    )
    assert failed.returncode != 0
    if damage == "process_exit":
        assert failed.returncode == 73
    else:
        assert {
            "gzip": "BadGzipFile",
            "truncated_gzip": "EOFError",
            "hash_mismatch": "Raw content hash mismatch",
            "missing_hash": "Raw content hash mismatch",
            "invalid_utf8": "UnicodeDecodeError",
            "destination_hash_mismatch": "Existing raw content hash mismatch",
            "destination_gzip": "BadGzipFile",
            "destination_truncated_gzip": "EOFError",
            "json": "JSONDecodeError",
            "identity": "Conflicting",
            "source_table": "no such table: source_runs",
        }[damage] in failed.stderr
    with sqlite3.connect(destination) as conn:
        assert list(conn.iterdump()) == before
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert snapshot.read_bytes() == damaged_snapshot
    assert {p.name: p.read_bytes() for p in (exports / "raw").iterdir()} == damaged_raw
    assert sentinel.read_text() == "previous export\n"
    assert all(
        (destination.parent / "raw" / name).read_bytes() == content
        for name, content in existing_raw.items()
    )
    assert list(destination_exports.iterdir()) == [sentinel]

    if damage.startswith("destination_"):
        collision.write_bytes(original)
    elif damage in ("gzip", "truncated_gzip", "hash_mismatch", "missing_hash", "invalid_utf8"):
        raw.write_bytes(original)
        if damage in ("missing_hash", "invalid_utf8"):
            with sqlite3.connect(snapshot) as source:
                source.execute(
                    "UPDATE fetches SET content_hash=? WHERE raw_path=?",
                    (
                        hashlib.sha256(gzip.decompress(original)).hexdigest(),
                        str(raw.relative_to(exports)),
                    ),
                )
    elif damage == "source_table":
        with sqlite3.connect(snapshot) as source:
            source.execute("ALTER TABLE missing_source_runs RENAME TO source_runs")
    elif damage != "process_exit":
        with sqlite3.connect(snapshot) as source:
            source.execute(
                "UPDATE observations SET details_json=? WHERE id=?", (original, observation)
            )
    repaired_snapshot = snapshot.read_bytes()
    retried = subprocess.run(
        args, cwd=ROOT, text=True, capture_output=True, check=False, timeout=30
    )
    assert retried.returncode == 0, retried.stderr
    assert snapshot.read_bytes() == repaired_snapshot
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT price_eur FROM observations WHERE listing_id=(SELECT id FROM listings WHERE external_id='1') ORDER BY observed_at,id"
        ).fetchall() == [(8500.0,), (8000.0,)]
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE kind='price_eur_changed'").fetchone()[0]
            == 1
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for content_hash, raw_path in conn.execute(
            "SELECT content_hash,raw_path FROM fetches WHERE raw_path IS NOT NULL"
        ):
            assert (
                hashlib.sha256(
                    gzip.decompress((destination.parent / raw_path).read_bytes())
                ).hexdigest()
                == content_hash
            )
    with sqlite3.connect(destination_exports / "market.sqlite3") as backup:
        assert backup.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 4


@pytest.mark.parametrize(
    "escape",
    [
        "absolute",
        "parent",
        "nested_parent",
        "file_symlink",
        "directory_symlink",
        "missing_absolute",
        "missing_parent",
        "broken_symlink",
    ],
)
def test_cli_snapshot_import_rejects_raw_outside_snapshot(market, tmp_path, escape):
    from car_market.db import Database

    state, database, exports, command, _ = market
    collect(command)
    state["price"] = 8000
    collect(command)
    shutil.copytree(database.parent / "raw", exports / "raw")
    snapshot = exports / "market.sqlite3"
    with sqlite3.connect(snapshot) as source:
        fetch_id, relative = source.execute(
            "SELECT id,raw_path FROM fetches WHERE raw_path IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    original = (exports / relative).read_bytes()
    outside = tmp_path / "outside/evidence.html.gz"
    outside.parent.mkdir()
    if not escape.startswith("missing_") and escape != "broken_symlink":
        outside.write_bytes(original)
    if escape in ("absolute", "missing_absolute"):
        unsafe = str(outside)
    elif escape in ("parent", "missing_parent"):
        unsafe = "../outside/evidence.html.gz"
    elif escape == "nested_parent":
        unsafe = "raw/../../outside/evidence.html.gz"
    elif escape in ("file_symlink", "broken_symlink"):
        (exports / "raw/alias.gz").symlink_to(outside)
        unsafe = "raw/alias.gz"
    else:
        (exports / "linked").symlink_to(outside.parent, target_is_directory=True)
        unsafe = "linked/evidence.html.gz"
    with sqlite3.connect(snapshot) as source:
        source.execute("UPDATE fetches SET raw_path=? WHERE id=?", (unsafe, fetch_id))
    snapshot_before = snapshot.read_bytes()
    destination = tmp_path / "imported/market.sqlite3"
    db = Database(destination)
    run = db.start_run("fixture")
    db.save_fetch(
        run,
        "fixture",
        "fixture://existing",
        "fixture://existing",
        200,
        "existing",
        "2026-10-04T08:00:00+00:00",
    )
    db.finish_run(run, "complete", db.summary())
    before = list(db.conn.iterdump())
    existing_raw = {p.name: p.read_bytes() for p in db.raw.iterdir()}
    db.conn.close()
    destination_exports = tmp_path / "imported-exports"
    destination_exports.mkdir()
    sentinel = destination_exports / "market-current.csv"
    sentinel.write_text("previous export\n")

    # An audit hook inside the real CLI process records even reads through symlinks.
    hook = tmp_path / "audit-hook"
    hook.mkdir()
    read_marker, ready_marker = tmp_path / "outside-read", tmp_path / "audit-ready"
    (hook / "sitecustomize.py").write_text(
        "import os, sys\n"
        f"with open({str(ready_marker)!r}, 'w') as stream: stream.write('ready')\n"
        "def audit(event, args):\n"
        "    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):\n"
        f"        if os.path.realpath(os.fsdecode(args[0])) == {str(outside)!r}:\n"
        f"            with open({str(read_marker)!r}, 'w') as stream: stream.write('opened')\n"
        "sys.addaudithook(audit)\n"
    )
    args = [
        sys.executable,
        "-m",
        "car_market.cli",
        "--db",
        str(destination),
        "--export-dir",
        str(destination_exports),
        "import-db",
        str(snapshot),
    ]
    env = {
        **os.environ,
        "E2E_TELEMETRY_DISABLED": "1",
        "PYTHONPATH": os.pathsep.join([str(hook), str(ROOT)]),
    }
    failed = subprocess.run(
        args, cwd=ROOT, env=env, text=True, capture_output=True, check=False, timeout=30
    )
    assert ready_marker.read_text() == "ready"
    assert not read_marker.exists(), "CLI read raw evidence outside the snapshot directory"
    assert failed.returncode != 0
    assert "Unsafe raw path" in failed.stderr
    with sqlite3.connect(destination) as conn:
        assert list(conn.iterdump()) == before
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert snapshot.read_bytes() == snapshot_before
    assert list(destination_exports.iterdir()) == [sentinel]
    assert sentinel.read_text() == "previous export\n"
    assert all(
        (destination.parent / "raw" / name).read_bytes() == body
        for name, body in existing_raw.items()
    )
    if outside.exists():
        assert outside.read_bytes() == original

    # The corrected path returns verified raw data; the lock is released and retry is idempotent.
    with sqlite3.connect(snapshot) as source:
        source.execute("UPDATE fetches SET raw_path=? WHERE id=?", (relative, fetch_id))
    repaired = snapshot.read_bytes()
    for _ in range(2):
        retried = subprocess.run(
            args, cwd=ROOT, env=env, text=True, capture_output=True, check=False, timeout=30
        )
        assert retried.returncode == 0, retried.stderr
    assert not read_marker.exists()
    assert snapshot.read_bytes() == repaired
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE kind='price_eur_changed'").fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM observations WHERE fetch_id IS NULL").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with sqlite3.connect(snapshot) as source:
            omitted = source.execute(
                "SELECT content_hash FROM fetches WHERE raw_path IS NULL ORDER BY id"
            ).fetchall()
        assert (
            conn.execute(
                "SELECT content_hash FROM fetches WHERE raw_path IS NULL ORDER BY id"
            ).fetchall()
            == omitted * 2
        )
        for digest, raw_path in conn.execute(
            "SELECT content_hash,raw_path FROM fetches WHERE raw_path IS NOT NULL"
        ):
            assert (
                hashlib.sha256(
                    gzip.decompress((destination.parent / raw_path).read_bytes())
                ).hexdigest()
                == digest
            )
    with sqlite3.connect(destination_exports / "market.sqlite3") as backup:
        assert backup.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3


@pytest.mark.parametrize("alias", ["file", "directory"])
def test_cli_snapshot_import_accepts_raw_alias_within_snapshot(market, tmp_path, alias):
    _, database, exports, command, _ = market
    collect(command)
    shutil.copytree(database.parent / "raw", exports / "raw")
    snapshot = exports / "market.sqlite3"
    with sqlite3.connect(snapshot) as source:
        fetch_id, relative = source.execute(
            "SELECT id,raw_path FROM fetches ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if alias == "file":
            (exports / "alias.gz").symlink_to(relative)
            contained = "alias.gz"
        else:
            (exports / "linked").symlink_to("raw", target_is_directory=True)
            contained = "linked/" + Path(relative).name
        source.execute("UPDATE fetches SET raw_path=? WHERE id=?", (contained, fetch_id))
    before = snapshot.read_bytes()
    destination = tmp_path / "imported/market.sqlite3"
    command(
        "--db",
        str(destination),
        "--export-dir",
        str(tmp_path / "imported-exports"),
        "import-db",
        str(snapshot),
    )
    assert snapshot.read_bytes() == before
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for digest, raw_path in conn.execute("SELECT content_hash,raw_path FROM fetches"):
            assert (
                hashlib.sha256(
                    gzip.decompress((destination.parent / raw_path).read_bytes())
                ).hexdigest()
                == digest
            )


@pytest.mark.parametrize("alias", ["same_path", "symlink", "hardlink"])
def test_cli_snapshot_import_rejects_destination_as_source(market, tmp_path, alias):
    _, database, _, command, _ = market
    collect(command)
    with sqlite3.connect(database) as conn:
        before = list(conn.iterdump())
    snapshot = database
    if alias != "same_path":
        snapshot = tmp_path / "snapshot.sqlite3"
        if alias == "symlink":
            snapshot.symlink_to(database)
        else:
            os.link(database, snapshot)
    result = command("import-db", str(snapshot), success=False)
    assert result.returncode != 0
    assert "different databases" in result.stderr
    with sqlite3.connect(database) as conn:
        assert list(conn.iterdump()) == before
