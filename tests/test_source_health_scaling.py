"""Source history must keep actual-time semantics and bounded lookup work."""

from datetime import UTC, datetime, timedelta, timezone

from car_market.db import Database


def test_v5_source_history_migration_preserves_rows_and_bounds_lookup(tmp_path):
    path = tmp_path / "market.sqlite3"
    db = Database(path)
    db.conn.execute("DROP INDEX source_query_actual_time")
    db.conn.execute("PRAGMA user_version=5")
    rows = []
    for number in range(8000):
        clock = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=number)
        # Different offsets must compare by the actual instant, including rows
        # where a running query has no finished_at yet.
        zone = UTC if number % 2 else timezone(timedelta(hours=2))
        stamp = clock.astimezone(zone).isoformat()
        rows.append((number % 4, number % 5, stamp, None if number % 7 == 0 else stamp))
    db.conn.executemany(
        "INSERT INTO source_runs(source,query_key,started_at,finished_at,status) VALUES (?,?,?,?, 'partial')",
        rows,
    )
    db.conn.commit()
    before = [
        tuple(row) for row in db.conn.execute("SELECT * FROM source_runs ORDER BY id")
    ]
    db.conn.close()
    db = Database(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert [
        tuple(row) for row in db.conn.execute("SELECT * FROM source_runs ORDER BY id")
    ] == before
    expected = {}
    for row in before:
        key = (row[2], row[3])
        actual = datetime.fromisoformat(row[5] or row[4])
        if key not in expected or (actual, row[0]) > expected[key][:2]:
            expected[key] = (actual, row[0], row)
    calls = 0

    def budget():
        nonlocal calls
        calls += 1
        return int(calls > 300)

    db.conn.set_progress_handler(budget, 1000)
    try:
        actual = [tuple(row) for row in db.conn.execute("SELECT * FROM source_health")]
    finally:
        db.conn.set_progress_handler(None, 0)
        db.conn.close()
    assert sorted(actual) == sorted(value[2] for value in expected.values())


def test_source_health_readonly_open_does_not_migrate_v5(tmp_path):
    path = tmp_path / "market.sqlite3"
    db = Database(path)
    db.conn.execute("DROP INDEX source_query_actual_time")
    db.conn.execute("PRAGMA user_version=5")
    db.conn.commit()
    db.conn.close()
    reader = Database.read_only(path)
    assert reader.conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert not reader.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='source_query_actual_time'"
    ).fetchone()
    reader.conn.close()


def test_blocked_history_lookup_does_not_scan_successful_inventory(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    rows = [
        ("pkw_de", "https://www.pkw.de/a/" + str(i), "2026-10-10T08:00:00+00:00", 200)
        for i in range(10_000)
    ]
    rows += [
        (
            "pkw_de",
            "https://www.pkw.de/a/rate-limited",
            "2026-10-10T11:00:00+02:00",
            429,
        ),
        ("pkw_de", "https://www.pkw.de/a/blocked", "2026-10-10T09:30:00+00:00", 403),
    ]
    db.conn.executemany(
        "INSERT INTO fetches(source,url,fetched_at,status) VALUES (?,?,?,?)", rows
    )
    calls = 0

    def budget():
        nonlocal calls
        calls += 1
        return int(calls > 20)

    db.conn.set_progress_handler(budget, 100)
    try:
        row = db.conn.execute(
            "SELECT * FROM fetches WHERE source=? AND status IN (403,429) ORDER BY julianday(fetched_at) DESC,id DESC LIMIT 1",
            ("pkw_de",),
        ).fetchone()
    finally:
        db.conn.set_progress_handler(None, 0)
    assert row["status"] == 403
    db.conn.close()
