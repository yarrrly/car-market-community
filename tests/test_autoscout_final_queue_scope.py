"""Current owned AS exclusions invalidate earlier work within one ordinary pass."""

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from car_market import collect, model_profiles, provenance
from car_market import db as db_module
from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Response
from car_market.parse import walk

ROOT = Path(__file__).parent / "fixtures/nl_publication_proof"
META = next(
    m for m in json.loads((ROOT / "manifest.json").read_text()) if m["id"] == 267194
)
ORIGINAL = (ROOT / META["fixture"]).read_text()
IDENTITY = "e8fbe6bc-57ac-4b12-9864-96c0d92c3620"
CAPTURE = datetime.fromisoformat(META["fetched_at"])
URL = META["url"].replace("page=2", "page=1")


@pytest.fixture(autouse=True)
def fixed_evaluation_and_no_network(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = CAPTURE + timedelta(minutes=5)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    for module in (collect, db_module, model_profiles, provenance):
        monkeypatch.setattr(module, "datetime", Clock)

    def refuse(*args, **kwargs):
        raise AssertionError("Native fixtures and controlled Getter only")

    monkeypatch.setattr("socket.socket.connect", refuse)
    monkeypatch.setattr("socket.create_connection", refuse)


def projected(fault=None):
    # The original own record is unchanged in the positive control. Page1 context,
    # clocks and negative country/model mutations are labelled synthetic controls.
    soup = BeautifulSoup(ORIGINAL, "html.parser")
    script = soup.select_one("script#__NEXT_DATA__")
    document = json.loads(script.get_text())
    state = next(n for n in walk(document) if isinstance(n.get("listings"), list))
    state["pageQuery"]["page"] = "1"
    own = next(n for n in walk(state) if n.get("id") == IDENTITY)
    if fault == "country_BE":
        own["location"]["countryCode"] = "BE"
    elif fault == "model_220":
        own["vehicle"]["model"] = "220"
    elif fault == "country_missing":
        own["location"].pop("countryCode")
    script.string = json.dumps(document)
    return str(soup)


def config(queries=None):
    return {
        "scope": {"country": "NL"},
        "queries": queries or [],
        "workers": 1,
        "detail_limit": 1,
        "detail_batch_limit": 1,
        "detail_cache_hours": 24,
    }


def ingest(worker, run, fault=None, minutes=0):
    stamp = (CAPTURE + timedelta(minutes=minutes)).isoformat()
    return worker.ingest_response(
        run, "autoscout24", "own-root", Response(URL, URL, 200, projected(fault), stamp)
    )


def current(db):
    return db.conn.execute(
        "SELECT * FROM listings WHERE source='autoscout24' AND external_id=?",
        (IDENTITY,),
    ).fetchone()


@pytest.mark.parametrize("fault", ["country_BE", "model_220"])
def test_one_run_skips_current_conclusive_exclusion(tmp_path, fault):
    exercise_run(tmp_path, fault, "excluded", 0)


@pytest.mark.parametrize(
    "fault,scope", [(None, "target"), ("country_missing", "needs_confirmation")]
)
def test_one_run_keeps_positive_and_unknown_country_details(tmp_path, fault, scope):
    exercise_run(tmp_path, fault, scope, 1)


def exercise_run(tmp_path, fault, expected_scope, expected_calls):
    db = Database(tmp_path / "market.sqlite3")
    queries = [
        {
            "source": "autoscout24",
            "key": key,
            "url": URL,
            "pagination": "page",
            "max_pages": 1,
        }
        for key in ("overlap-first", "overlap-later")
    ]
    worker = Collector(db, config(queries))
    native = next(
        item
        for item in worker._parse("autoscout24", ORIGINAL, META["url"]).listings
        if item.external_id == IDENTITY
    )
    assert native.transmission == "manual" and native.scope == "target"
    assert not worker.detail_jobs
    roots, details, before_details = [], [], []

    def getter(url, **kwargs):
        if url == URL:
            index = len(roots)
            roots.append(url)
            return Response(
                url,
                url,
                200,
                projected(fault if index else None),
                (CAPTURE + timedelta(minutes=index)).isoformat(),
            )
        details.append(url)
        return Response(
            url,
            url,
            404,
            "",
            (CAPTURE + timedelta(minutes=4)).isoformat(),
            error="synthetic_404",
        )

    worker.fetcher.get = getter
    actual_details = worker.details

    def observe_dispatch(run):
        before_details.append(dict(current(db)))
        return actual_details(run)

    worker.details = observe_dispatch
    result = worker.run(details=True, checks=False)
    assert len(roots) == 2
    assert before_details[0]["scope"] == expected_scope
    assert len(details) == expected_calls
    assert db.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert {r[0] for r in db.conn.execute("SELECT DISTINCT run_id FROM fetches")} == {
        result["run_id"]
    }
    # A two-record projection/page cap never proves the complete628-record census.
    assert result["status"] == "partial"
    db.conn.close()


@pytest.mark.parametrize("fault", ["country_BE", "model_220"])
def test_cleanup_uses_same_listing_id_and_preserves_other_and_none(tmp_path, fault):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, config())
    run = db.start_run(timestamp=CAPTURE.isoformat())
    ingest(worker, run)
    listing_id = current(db)["id"]
    url, job = next(iter(worker.detail_jobs.items()))
    alias = "https://review.invalid/same-publication-alias"
    worker.detail_jobs[alias] = ("portal", listing_id, copy.deepcopy(job[2]))
    other_meta = json.loads(
        (
            Path(__file__).parent / "fixtures/autokopen_native_scope/manifest.json"
        ).read_text()
    )[0]
    other_body = (
        Path(__file__).parent
        / "fixtures/autokopen_native_scope"
        / other_meta["fixture"]
    ).read_text()
    worker.ingest_response(
        run,
        "autokopen",
        "other-native-publication",
        Response(
            other_meta["url"],
            other_meta["final_url"],
            200,
            other_body,
            other_meta["fetched_at"],
        ),
    )
    other = next(
        (u, j) for u, j in worker.detail_jobs.items() if j[2].source == "autokopen"
    )
    none_url = "https://review.invalid/unresolved-frontier"
    none_job = ("portal", None, copy.deepcopy(job[2]))
    worker.detail_jobs[none_url] = none_job
    ingest(worker, run, fault, 1)
    assert current(db)["scope"] == "excluded"
    assert url not in worker.detail_jobs and alias not in worker.detail_jobs
    assert worker.detail_jobs[other[0]] == other[1]
    assert worker.detail_jobs[none_url] == none_job
    assert not any(j[1] == listing_id for j in worker.detail_jobs.values())
    db.conn.close()


def test_current_positive_restores_work_after_negative(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, config())
    run = db.start_run(timestamp=CAPTURE.isoformat())
    ingest(worker, run)
    ingest(worker, run, "country_BE", 1)
    assert current(db)["scope"] == "excluded" and not worker.detail_jobs
    ingest(worker, run, minutes=2)
    assert current(db)["scope"] == "target" and len(worker.detail_jobs) == 1
    assert next(iter(worker.detail_jobs.values()))[2].scope == "target"
    db.conn.close()


@pytest.mark.parametrize("fault", ["country_BE", "model_220"])
def test_older_negative_cannot_cancel_newer_positive_work(tmp_path, fault):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, config())
    run = db.start_run(timestamp=CAPTURE.isoformat())
    ingest(worker, run, minutes=2)
    before = dict(current(db))
    job = next(iter(worker.detail_jobs.items()))
    confirmations = [
        tuple(r)
        for r in db.conn.execute("SELECT * FROM field_verifications ORDER BY field")
    ]
    ingest(worker, run, fault, 1)
    after = dict(current(db))
    assert after["scope"] == "target" and after["last_seen"] == before["last_seen"]
    assert list(worker.detail_jobs.items()) == [job]
    assert [
        tuple(r)
        for r in db.conn.execute("SELECT * FROM field_verifications ORDER BY field")
    ] == confirmations
    db.conn.close()
