"""Current owned exclusions cancel only that publication's stale detail work."""

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from car_market import collect, provenance
from car_market import db as db_module
from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Response
from car_market.model_profiles import BMW_PROFILE, MERCEDES_PROFILE, bind_profile
from car_market.parse import Page, json_documents, walk

ROOT = Path(__file__).parent / "fixtures/autokopen_native_scope"
META = json.loads((ROOT / "manifest.json").read_text())[0]
BODY = (ROOT / META["fixture"]).read_text()
STAMP = datetime.fromisoformat(META["fetched_at"])


@pytest.fixture(autouse=True)
def offline_clock(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(
            "Retained-native queue controls must not contact a provider"
        )

    monkeypatch.setattr("socket.socket.connect", refuse)
    monkeypatch.setattr("socket.create_connection", refuse)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            clock = STAMP + timedelta(minutes=5)
            return clock.astimezone(tz) if tz else clock.replace(tzinfo=None)

    for module in (collect, db_module, provenance):
        monkeypatch.setattr(module, "datetime", Clock)


def derived(updates):
    """Fault fields are synthetic; the original own ID/URI envelope is preserved."""
    soup = BeautifulSoup(BODY, "html.parser")
    envelope = next(
        node
        for document in json_documents(BODY)
        for node in walk(document)
        if isinstance(node.get("initialResults"), list)
    )
    envelope["initialResults"][0].update(updates)
    soup.find("script").string = (
        "self.__next_f.push("
        + json.dumps([1, "0:" + json.dumps(envelope) + "\n"])
        + ")"
    )
    return str(soup)


def setup(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    config = {
        "scope": {"country": "NL", "model_profile": BMW_PROFILE},
        "queries": [],
        "workers": 1,
        "detail_limit": 1,
        "detail_cache_hours": 6,
        "detail_error_backoff_hours": 6,
    }
    bind_profile(db, config)
    return db, Collector(db, config)


def ingest(worker, run, body=BODY, *, minutes=0):
    clock = (STAMP + timedelta(minutes=minutes)).isoformat()
    return worker.ingest_response(
        run,
        "autokopen",
        "same_owned_publication",
        Response(META["url"], META["final_url"], 200, body, clock),
    )


@pytest.mark.parametrize(
    ("updates", "expected_scope", "expected_calls"),
    [
        ({"country_code": "BE"}, "excluded", 0),
        ({"variant": "218d Executive"}, "excluded", 0),
        ({"fuel": "Diesel"}, "excluded", 0),
        ({}, "needs_confirmation", 1),
    ],
)
def test_same_pass_owned_exclusion_stops_prior_pending_get(
    tmp_path, updates, expected_scope, expected_calls
):
    db, worker = setup(tmp_path)
    historical = db.start_run(
        "retained_native_seed", (STAMP - timedelta(minutes=1)).isoformat()
    )
    ingest(worker, historical, minutes=-1)
    original = {
        table: [
            tuple(row) for row in db.conn.execute(f"SELECT * FROM {table} ORDER BY id")
        ]
        for table in ("observations", "fetches")
    }
    run = db.start_run("same_pass_native_then_synthetic_fault", STAMP.isoformat())
    ingest(worker, run)
    assert len(worker.detail_jobs) == 1
    page, count = ingest(worker, run, derived(updates), minutes=1)
    assert page.listings[0].external_id == META["external_id"]
    assert page.listings[0].scope == expected_scope
    assert db.conn.execute("SELECT scope FROM listings").fetchone()[0] == expected_scope
    assert count == expected_calls
    calls = []

    def controlled_get(url):
        calls.append(url)
        return Response(
            url,
            url,
            404,
            "synthetic missing detail",
            (STAMP + timedelta(minutes=5)).isoformat(),
        )

    worker.fetcher.get = controlled_get
    worker.details(run)
    assert len(calls) == expected_calls
    for table, rows in original.items():
        assert [
            tuple(row)
            for row in db.conn.execute(
                f"SELECT * FROM {table} WHERE id<=? ORDER BY id", (rows[-1][0],)
            )
        ] == rows
    db.conn.close()


def test_exclusion_removes_all_same_id_jobs_and_preserves_other_and_unknown_ids(
    tmp_path,
):
    db, worker = setup(tmp_path)
    run = db.start_run(timestamp=STAMP.isoformat())
    ingest(worker, run)
    own_job = next(iter(worker.detail_jobs.values()))
    source, own_id, item = own_job
    # Explicit synthetic planner entries prove cleanup keys, not native aliases.
    same_uri = item.url + "?synthetic_same_ID_alias=1"
    other_uri = item.url + "?synthetic_other_ID=1"
    unknown_uri = item.url + "?synthetic_unknown_ID=1"
    worker.detail_jobs[same_uri] = (source, own_id, copy.deepcopy(item))
    worker.detail_jobs[other_uri] = (source, own_id + 1, copy.deepcopy(item))
    worker.detail_jobs[unknown_uri] = (source, None, copy.deepcopy(item))
    other = worker.detail_jobs[other_uri]
    unknown = worker.detail_jobs[unknown_uri]
    ingest(worker, run, derived({"country_code": "BE"}), minutes=1)
    assert set(worker.detail_jobs) == {other_uri, unknown_uri}
    assert (
        worker.detail_jobs[other_uri] is other
        and worker.detail_jobs[unknown_uri] is unknown
    )
    db.conn.close()


@pytest.mark.parametrize("boundary", ["configured", "other_provider", "DE", "Mercedes"])
def test_early_cleanup_does_not_change_other_collection_contracts(
    tmp_path, monkeypatch, boundary
):
    db, worker = setup(tmp_path)
    run = db.start_run(timestamp=STAMP.isoformat())
    page, _ = ingest(worker, run)
    item = copy.deepcopy(page.listings[0])
    item.scope = "excluded"
    source = "autokopen"
    if boundary == "configured":
        worker.config["scope"]["model_profile"] = "configured_vehicle"
    elif boundary == "other_provider":
        source = item.source = "autoscout24"
    elif boundary == "DE":
        worker.country = worker.config["scope"]["country"] = "DE"
        item.raw["market_country"] = "DE"
    else:
        worker.config["scope"]["model_profile"] = MERCEDES_PROFILE
    queued = dict(worker.detail_jobs)
    own_id = next(iter(queued.values()))[1]
    monkeypatch.setattr(
        worker,
        "_parse",
        lambda *args: Page([item], search_filters={"country": worker.country}),
    )
    monkeypatch.setattr(worker, "_observe", lambda *args: own_id)
    worker.ingest_response(
        run,
        source,
        "synthetic_contract_boundary",
        Response(META["url"], META["final_url"], 200, BODY, META["fetched_at"]),
    )
    assert worker.detail_jobs == queued
    db.conn.close()
