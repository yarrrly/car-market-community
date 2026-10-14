"""Native bytes survive failed publication and retries without corrupt final names."""

import gzip
import hashlib
import os
from pathlib import Path

import pytest

from car_market.db import Database, now

BODY = (
    Path(__file__).parent / "fixtures/reliability_nl/marktplaats-corolla-unicode.json"
).read_text()
URL = "https://www.marktplaats.nl/lrp/api/search"
DIGEST = hashlib.sha256(BODY.encode()).hexdigest()


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "data/market.sqlite3")
    yield db
    db.conn.close()


def save(db, run):
    return db.save_fetch(run, "marktplaats", URL, URL, 200, BODY, now())


@pytest.mark.parametrize("failure", ["write", "close", "fsync", "publish"])
def test_failure_never_publishes_partial_digest_and_retry_succeeds(
    database, monkeypatch, failure
):
    db = database
    run = db.start_run("live")
    target = db.raw / (DIGEST + ".html.gz")
    with monkeypatch.context() as fault:
        if failure == "write":
            original = gzip.GzipFile.write

            def fail(stream, data):
                original(stream, data[: len(data) // 2])
                raise OSError("injected_write_failure")

            fault.setattr(gzip.GzipFile, "write", fail)
        elif failure == "close":
            original = gzip.GzipFile.close

            def fail(stream):
                was_open = stream.fileobj is not None
                original(stream)
                if was_open and stream.mode == gzip.WRITE:
                    raise OSError("injected_close_failure")

            fault.setattr(gzip.GzipFile, "close", fail)
        else:

            def fail(*_args, **_kwargs):
                raise OSError("injected_" + failure + "_failure")

            fault.setattr(os, "fsync" if failure == "fsync" else "link", fail)
        with pytest.raises(OSError, match="injected_"):
            save(db, run)
    assert not target.exists()
    assert not list(db.raw.glob(".raw-*.tmp"))
    assert db.conn.execute("SELECT count(*) FROM fetches").fetchone()[0] == 0
    fetch = save(db, run)
    assert gzip.decompress(target.read_bytes()).decode() == BODY
    row = db.conn.execute(
        "SELECT content_hash,raw_path FROM fetches WHERE id=?", (fetch,)
    ).fetchone()
    assert row["content_hash"] == DIGEST
    assert row["raw_path"] == "raw/" + DIGEST + ".html.gz"


def test_duplicate_capture_keeps_original_file_and_decoded_hash(database):
    db = database
    run = db.start_run("live")
    first = save(db, run)
    target = db.raw / (DIGEST + ".html.gz")
    before = target.stat()
    original = target.read_bytes()
    second = save(db, run)
    assert second != first
    assert target.read_bytes() == original
    assert (target.stat().st_ino, target.stat().st_mtime_ns) == (
        before.st_ino,
        before.st_mtime_ns,
    )
    assert not list(db.raw.glob(".raw-*.tmp"))


@pytest.mark.parametrize("corrupt", [False, True])
def test_concurrent_existing_capture_is_checked_without_replacement(
    database, monkeypatch, corrupt
):
    db = database
    run = db.start_run("live")
    target = db.raw / (DIGEST + ".html.gz")
    published = []
    original_link = os.link

    def another_writer(temporary, destination):
        if corrupt:
            target.write_bytes(gzip.compress(b"different retained bytes"))
        else:
            original_link(temporary, destination)
        published.append(target.read_bytes())
        raise FileExistsError("concurrent_capture")

    monkeypatch.setattr(os, "link", another_writer)
    if corrupt:
        with pytest.raises(ValueError, match="Existing raw content hash mismatch"):
            save(db, run)
        assert db.conn.execute("SELECT count(*) FROM fetches").fetchone()[0] == 0
    else:
        save(db, run)
        assert gzip.decompress(target.read_bytes()).decode() == BODY
    assert target.read_bytes() == published[0]
    assert not list(db.raw.glob(".raw-*.tmp"))


def test_preexisting_corruption_remains_fail_closed(database):
    db = database
    run = db.start_run("live")
    target = db.raw / (DIGEST + ".html.gz")
    original = gzip.compress(b"original corruption must not be overwritten")
    target.write_bytes(original)
    with pytest.raises(ValueError, match="Existing raw content hash mismatch"):
        save(db, run)
    assert target.read_bytes() == original
    assert db.conn.execute("SELECT count(*) FROM fetches").fetchone()[0] == 0
    assert not list(db.raw.glob(".raw-*.tmp"))
