"""Regressions for retained Quoka links that interrupted both DE collectors."""

import gzip
import hashlib
import json
from pathlib import Path

import pytest
from car_market.collect import Collector
from car_market.db import Database
from car_market.mercedes_collect import MercedesCollector
from car_market.model_profiles import MERCEDES_PROFILE
from car_market.parse import Listing

FIXTURES = Path(__file__).parent / "fixtures" / "de_terminal_links"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())


def stored(meta):
    content = (FIXTURES / meta["listing_fixture"]).read_bytes()
    assert hashlib.sha256(content).hexdigest() == meta["listing_fixture_sha256"]
    return Listing(**json.loads(content))


def native(meta):
    content = (FIXTURES / meta["raw_fixture"]).read_bytes()
    assert hashlib.sha256(content).hexdigest() == meta["raw_fixture_sha256"]
    body = gzip.decompress(content)
    assert hashlib.sha256(body).hexdigest() == meta["content_hash"]
    return body.decode("utf-8")


def config(meta):
    scope = {"country": "DE"}
    if meta["profile"] == "mercedes":
        scope["model_profile"] = MERCEDES_PROFILE
    return {"scope": scope, "queries": [], "workers": 1}


def collector_type(meta):
    return MercedesCollector if meta["profile"] == "mercedes" else Collector


class NoNetwork:
    def __init__(self, *args, **kwargs):
        self._cache = {}

    def get(self, *args, **kwargs):
        raise AssertionError("The retained terminal-stage replay must not fetch live data")


@pytest.mark.parametrize("meta", MANIFEST, ids=lambda value: value["profile"])
def test_retained_string_links_queue_own_and_publisher_without_mutation(meta):
    item = stored(meta)
    original = (FIXTURES / meta["listing_fixture"]).read_text()
    worker = Collector.__new__(Collector)
    worker.country = "DE"
    worker.detail_jobs = {}
    assert all(isinstance(link, str) for link in item.links)

    worker.queue_details(item, meta["id"])

    assert worker.detail_jobs[item.url] == (item.source, meta["id"], item)
    for link in item.links:
        assert worker.detail_jobs[link] == ("portal", meta["id"], item)
    assert item.links == json.loads(original)["links"]
    assert item.raw == json.loads(original)["raw"]


@pytest.mark.parametrize("meta", MANIFEST, ids=lambda value: value["profile"])
def test_native_quoka_parser_links_reach_queue(meta, tmp_path, monkeypatch):
    monkeypatch.setattr("car_market.collect.Fetcher", NoNetwork)
    db = Database(tmp_path / "data" / "market.sqlite3")
    try:
        worker = collector_type(meta)(db, config(meta))
        page = worker._parse("quoka_de", native(meta), meta["final_url"])
        assert len(page.listings) == 1
        item = page.listings[0]
        assert item.external_id == meta["external_id"]
        assert item.links == stored(meta).links

        worker.queue_details(item, meta["id"])

        assert set(worker.detail_jobs) == {item.url, *item.links}
    finally:
        db.conn.close()


@pytest.mark.parametrize("meta", MANIFEST, ids=lambda value: value["profile"])
def test_run_drains_stored_quoka_links_and_finishes_without_refreshing_history(
    meta, tmp_path, monkeypatch
):
    monkeypatch.setattr("car_market.collect.Fetcher", NoNetwork)
    db = Database(tmp_path / "data" / "market.sqlite3")
    try:
        worker = collector_type(meta)(db, config(meta))
        item = stored(meta)
        seed = db.start_run("retained_fixture", meta["observed_at"])
        fetch = db.save_fetch(
            seed, item.source, meta["fetch_url"], meta["final_url"], 200,
            native(meta), meta["fetched_at"], "retained_fixture", None,
        )
        # Preserve original provenance references inside this isolated fixture database.
        db.conn.execute("UPDATE fetches SET id=? WHERE id=?", (meta["fetch_id"], fetch))
        fetch = meta["fetch_id"]
        listing = db.observe(seed, item, meta["observed_at"], fetch)
        db.finish_run(seed, "complete", {"retained_fixture": True})
        before = [tuple(row) for row in db.conn.execute("SELECT * FROM observations")]
        verifications = [tuple(row) for row in db.conn.execute("SELECT * FROM field_verifications")]
        queued = []

        def retain_deferred_details(run):
            queued.extend(worker.detail_jobs)
            db.finish_source(
                run, "portal_details", "discovered_links", meta["observed_at"],
                "partial", len(queued), 0, 0, 0, "Offline replay: details remain deferred",
                infer_missing=False,
            )

        monkeypatch.setattr(worker, "details", retain_deferred_details)
        result = worker.run()

        assert set(queued) == {item.url, *item.links}
        assert result["status"] == "partial"
        run = db.conn.execute("SELECT * FROM runs WHERE id=?", (result["run_id"],)).fetchone()
        assert run["finished_at"] and run["status"] == "partial"
        assert "error" not in json.loads(run["summary"])
        assert [tuple(row) for row in db.conn.execute("SELECT * FROM observations")] == before
        assert [tuple(row) for row in db.conn.execute("SELECT * FROM field_verifications")] == verifications
        assert db.conn.execute("SELECT availability FROM listings WHERE id=?", (listing,)).fetchone()[0] == "observed"
        assert result["publisher_bridges"]["merged"] == 0
        assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        db.conn.close()


@pytest.mark.parametrize("country", ["DE", "NL"])
def test_mixed_link_shapes_preserve_dealer_homepage_exclusion(country):
    item = stored(MANIFEST[0])
    publisher = item.links[0]
    item.links = [
        publisher,
        {"label": "publisher", "url": publisher},
        {"label": "dealer", "url": "https://example.test/"},
        {"label": "dealer", "url": "https://example.test/cars/own"},
        {"label": "publisher"},
    ]
    worker = Collector.__new__(Collector)
    worker.country = country
    worker.detail_jobs = {}

    worker.queue_details(item, 42)

    assert set(worker.detail_jobs) == {item.url, publisher, "https://example.test/cars/own"}
    assert worker.detail_jobs[publisher] == ("portal", 42, item)
