"""Native Mercedes discovery, own-publication IDs and scheduled query freshness."""

import copy
import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest
from bs4 import BeautifulSoup

from car_market.coverage import report
from car_market.db import Database
from car_market.fetch import Response
from car_market.mercedes_collect import MercedesCollector
from car_market.model_profiles import MERCEDES_PROFILE
from car_market.parse import Listing

FIXTURES = Path(__file__).parent / "fixtures/mercedes_retention_20261011"
CASES = json.loads((FIXTURES / "manifest.json").read_text())


def config():
    return {
        "scope": {"country": "NL", "model_profile": MERCEDES_PROFILE},
        "queries": [],
        "workers": 1,
        "detail_limit": 0,
    }


def native(case):
    blob = (FIXTURES / case["file"]).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == case["fixture_archive_sha256"]
    text = gzip.decompress(blob)
    assert hashlib.sha256(text).hexdigest() == case["fixture_body_sha256"]
    return text.decode()


def request_id(url):
    p = urlsplit(url)
    return p._replace(
        query=urlencode(sorted(parse_qsl(p.query, keep_blank_values=True)))
    ).geturl()


def ledgers(db, key="c180_mercedes_xml"):
    return {
        row["key"]: json.loads(row["value"])
        for row in db.conn.execute(
            "SELECT key,value FROM monitor_state WHERE key LIKE ?",
            ("model_discovery:marktnet:" + key + ":%",),
        )
    }


def replay_partitions(db):
    worker = MercedesCollector(db, config())
    cases = {
        request_id(row["url"]): row for row in CASES if row["source"] == "marktnet"
    }
    calls = []

    def get(url, **kwargs):
        case = cases[request_id(url)]
        calls.append(url)
        return Response(url, case["final_url"], 200, native(case), case["captured_at"])

    worker.fetcher.get = get
    query = {
        "source": "marktnet",
        "key": "c180_mercedes_xml",
        "url": "https://www.marktnet.nl/zoeken.xml?cat_id=57&q=c180&descr=on",
        "partition": "price",
        "partition_page_size": 20,
        "partition_request_limit": 150,
        "infer_missing": False,
    }
    run = db.start_run("fixture")
    worker.collect_query(run, query)
    assert len(calls) == 9
    health = db.conn.execute(
        "SELECT * FROM source_runs WHERE run_id=?", (run,)
    ).fetchone()
    assert (
        health["status"] == "complete"
        and health["received_count"] == health["expected_count"] == 66
    )
    return worker, cases


def test_all_native_partition_ids_survive_restart_without_new_membership(tmp_path):
    path = tmp_path / "market.sqlite3"
    db = Database(path)
    worker, cases = replay_partitions(db)
    before = ledgers(db)
    assert len(before) == 9
    assert (
        len(
            {
                row["external_id"]
                for value in before.values()
                for row in value["listings"]
            }
        )
        == 66
    )
    for value in before.values():
        assert value["page"] == 1
        case = cases[request_id(value["request_url"])]
        assert value["fetched_at"] == case["captured_at"]
        assert len(json.dumps(value).encode()) < 20000
        for row in value["listings"]:
            fetch = db.conn.execute(
                "SELECT fetched_at,content_hash FROM fetches WHERE id=?",
                (row["fetch_id"],),
            ).fetchone()
            assert row["fetched_at"] == fetch["fetched_at"] == case["captured_at"]
            assert fetch["content_hash"] == case["fixture_body_sha256"]
            assert not {"raw", "description", "vehicle"} & row.keys()
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 0
    assert db.conn.execute("SELECT count(*) FROM query_membership").fetchone()[0] == 0
    assert worker.detail_jobs
    db.conn.close()
    db = Database(path)
    assert ledgers(db) == before
    replay_partitions(db)
    after = ledgers(db)
    assert set(after) == set(before)
    assert (
        len(
            {
                row["external_id"]
                for value in after.values()
                for row in value["listings"]
            }
        )
        == 66
    )
    assert {key: value["fetched_at"] for key, value in after.items()} == {
        key: value["fetched_at"] for key, value in before.items()
    }
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 0
    assert db.conn.execute("SELECT count(*) FROM query_membership").fetchone()[0] == 0
    db.conn.close()


def test_discovery_key_normalizes_parameter_order_but_separates_query_filters(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    worker = MercedesCollector(db, config())
    case = next(row for row in CASES if row["source"] == "marktnet")
    run = db.start_run("fixture")
    for url in [case["url"], request_id(case["url"])]:
        worker.ingest_response(
            run,
            "marktnet",
            "same",
            Response(url, url, 200, native(case), case["captured_at"]),
        )
    assert len(ledgers(db, "same")) == 1
    parsed = urlsplit(case["url"])
    different = parsed._replace(query=parsed.query + "&minprice=1").geturl()
    worker.ingest_response(
        run,
        "marktnet",
        "same",
        Response(different, different, 200, native(case), case["captured_at"]),
    )
    assert len(ledgers(db, "same")) == 2
    worker.ingest_response(
        run,
        "marktnet",
        "other",
        Response(case["url"], case["url"], 200, native(case), case["captured_at"]),
    )
    assert len(ledgers(db, "other")) == 1
    assert db.conn.execute("SELECT count(*) FROM query_membership").fetchone()[0] == 0
    db.conn.close()


@pytest.mark.parametrize(
    "case",
    [row for row in CASES if row["source"] == "autokopen"],
    ids=lambda row: str(row["fetch_id"]),
)
def test_same_own_autokopen_uri_keeps_numeric_id_and_original_price_country_gates(
    tmp_path, monkeypatch, case
):
    db = Database(tmp_path / "market.sqlite3")
    worker = MercedesCollector(db, {**config(), "detail_limit": 1})
    text = native(case)
    original = worker._parse("autokopen", text, case["url"]).listings[0]
    scope, evidence = (
        original.scope,
        copy.deepcopy(original.raw["model_scope_evidence"]),
    )
    original.external_id = urlsplit(case["url"]).path.rsplit("-", 1)[-1]
    worker.queue_details(original, None)
    captured = []
    observe = worker._observe

    def capture(run, item, stamp, fetch_id=None, key=None):
        captured.append((copy.deepcopy(item), stamp))
        return observe(run, item, stamp, fetch_id, key)

    monkeypatch.setattr(worker, "_observe", capture)
    monkeypatch.setattr(
        worker,
        "_detail_get",
        lambda url: Response(url, url, 200, text, case["captured_at"]),
    )
    worker.details(db.start_run("fixture"))
    assert len(captured) == 1
    item, stamp = captured[0]
    assert item.external_id == original.external_id
    assert stamp == case["captured_at"]
    assert item.price is None and item.price_type == "unknown"
    assert item.scope == scope and item.raw["model_scope_evidence"] == evidence
    assert item.raw.get("market_country") is None
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 0
    assert db.conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
    assert (
        db.conn.execute(
            "SELECT error FROM fetches ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        == "detail_not_accepted"
    )
    db.conn.close()


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "foreign",
        "same_host_other_id",
        "request_redirect",
        "final_query",
        "capture_request",
    ],
)
def test_autokopen_normalization_requires_exact_request_final_canonical_binding(
    tmp_path, monkeypatch, change
):
    case = next(row for row in CASES if row["source"] == "autokopen")
    db = Database(tmp_path / "market.sqlite3")
    worker = MercedesCollector(db, {**config(), "detail_limit": 1})
    original = worker._parse("autokopen", native(case), case["url"]).listings[0]
    original.external_id = urlsplit(case["url"]).path.rsplit("-", 1)[-1]
    worker.queue_details(original, None)
    soup = BeautifulSoup(native(case), "html.parser")
    canonical = soup.select_one('link[rel="canonical"]')
    final = case["url"]
    captured_request = None
    if change == "missing":
        canonical.decompose()
    elif change == "duplicate":
        canonical.insert_after(copy.copy(canonical))
    elif change == "foreign":
        canonical["href"] = (
            "https://foreign.invalid/auto/detail/own-" + original.external_id
        )
    elif change == "same_host_other_id":
        canonical["href"] = case["url"].rsplit("-", 1)[0] + "-99999999"
    elif change == "request_redirect":
        original.url = case["url"].replace("/auto/detail/", "/auto/detail/alias-")
        worker.detail_jobs.clear()
        worker.queue_details(original, None)
    elif change == "final_query":
        final += "?unbound=1"
    elif change == "capture_request":
        captured_request = case["url"].replace("/auto/detail/", "/auto/detail/alias-")
    monkeypatch.setattr(
        worker,
        "_detail_get",
        lambda url: Response(
            captured_request or url, final, 200, str(soup), case["captured_at"]
        ),
    )
    monkeypatch.setattr(
        worker,
        "_observe",
        lambda *args, **kwargs: pytest.fail("unbound detail reached observation"),
    )
    worker.details(db.start_run("fixture"))
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 0
    db.conn.close()


@pytest.mark.parametrize("age,expected", [(50, False), (85, False), (91, True)])
def test_hourly_fast_query_freshness_includes_its_actual_period_and_grace(
    tmp_path, monkeypatch, age, expected
):
    from car_market import coverage

    clock = datetime(2026, 10, 11, 21, 30, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    monkeypatch.setattr(coverage, "datetime", FixedDatetime)
    db = Database(tmp_path / "market.sqlite3")
    stamp = (clock - timedelta(minutes=age)).isoformat()
    db.finish_source(
        db.start_run("fixture"),
        "autoscout24",
        "own",
        stamp,
        "complete",
        32,
        32,
        30,
        2,
        None,
        finished_at=stamp,
    )
    ad = Listing(
        "autoscout24",
        "facts",
        "https://fixture.invalid/facts",
        "Mercedes C180 Coupe",
        price=19800,
        mileage=80000,
        year=2018,
        fuel="petrol",
        transmission="automatic",
        raw={"market_country": "NL", "generation": "C205"},
    )
    db.observe(db.start_run("fixture-facts"), ad, "2026-10-07T10:00:00+00:00")
    tables = (
        "source_runs",
        "listings",
        "observations",
        "field_verifications",
        "query_membership",
    )
    before = {
        table: [tuple(row) for row in db.conn.execute("SELECT * FROM " + table)]
        for table in tables
    }
    result = report(
        db.conn,
        {
            "queries": [
                {
                    "source": "autoscout24",
                    "key": "own",
                    "url": "https://www.autoscout24.nl/lst/mercedes-benz",
                    "cadence": "fast",
                }
            ],
            "fast_sources": ["autoscout24"],
            "fast_interval_minutes": 60,
            "interval_hours": 6,
        },
    )
    assert result["queries"][0]["stale"] == expected
    assert {
        table: [tuple(row) for row in db.conn.execute("SELECT * FROM " + table)]
        for table in tables
    } == before
    db.conn.close()


@pytest.mark.parametrize(
    "cadence,source_filter,age,expected",
    [
        ("broad", ["s"], 300, False),
        ("fast", ["other"], 300, False),
        ("broad", ["s"], 391, True),
        ("fast", ["s"], -1, True),
    ],
)
def test_freshness_uses_actual_full_selection_and_rejects_future_clock(
    tmp_path, cadence, source_filter, age, expected
):
    db = Database(tmp_path / "market.sqlite3")
    stamp = (datetime.now(UTC) - timedelta(minutes=age)).isoformat()
    db.finish_source(
        db.start_run("fixture"),
        "s",
        "own",
        stamp,
        "complete",
        1,
        1,
        0,
        1,
        None,
        finished_at=stamp,
    )
    result = report(
        db.conn,
        {
            "queries": [
                {
                    "source": "s",
                    "key": "own",
                    "url": "https://source.invalid",
                    "cadence": cadence,
                }
            ],
            "fast_sources": source_filter,
            "fast_interval_minutes": 60,
            "interval_hours": 6,
        },
    )
    assert result["queries"][0]["stale"] == expected
    db.conn.close()


def test_discovery_keeps_legacy_state(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    worker = MercedesCollector(db, config())
    case = next(row for row in CASES if row["source"] == "marktnet")
    response = Response(
        case["url"], case["url"], 200, native(case), case["captured_at"]
    )
    legacy_key = "model_discovery:marktnet:own:1"
    legacy_value = json.dumps(
        {"listings": [{"external_id": "legacy-only"}], "run_id": 1}
    )
    db.conn.execute(
        "INSERT INTO monitor_state VALUES (?,?)", (legacy_key, legacy_value)
    )
    worker.ingest_response(db.start_run("fixture"), "marktnet", "own", response)
    assert len(ledgers(db, "own")) == 2
    assert (
        db.conn.execute(
            "SELECT value FROM monitor_state WHERE key=?", (legacy_key,)
        ).fetchone()[0]
        == legacy_value
    )
    assert db.conn.execute("SELECT count(*) FROM query_membership").fetchone()[0] == 0
    db.conn.close()


def test_native_autohero_post_filters_keep_separate_bounded_discovery(tmp_path):
    from car_market.mercedes_autohero_nl import autohero_nl_page_request

    db = Database(tmp_path / "market.sqlite3")
    worker = MercedesCollector(db, config())
    root = Path(__file__).parent / "fixtures/mercedes_autohero_nl"
    provenance = json.loads((root / "provenance.json").read_text())
    for name in ("cclass", "c180", "c180"):
        meta = provenance[name + "-search.json"]["original"]
        body = json.loads((root / ("request-" + name + ".json")).read_text())
        if name == "c180":
            body = dict(reversed(list(body.items())))
        request = autohero_nl_page_request(meta["url"], 1, body)
        text = (root / (name + "-search.json")).read_text()
        assert (
            hashlib.sha256(text.encode()).hexdigest()
            == provenance[name + "-search.json"]["sha256"]
        )
        worker.ingest_response(
            db.start_run("fixture"),
            "autohero",
            "same-query",
            Response(request["url"], request["url"], 200, text, meta["timestamp"]),
            request_body=request["body"],
        )
    rows = db.conn.execute(
        "SELECT value FROM monitor_state WHERE key LIKE 'model_discovery:autohero:same-query:%'"
    ).fetchall()
    assert len(rows) == 2
    states = [json.loads(row[0]) for row in rows]
    assert {state["total"] for state in states} == {1, 3}
    assert {state["request_url"] for state in states} == {request["url"]}
    assert len({state["request_context_hash"] for state in states}) == 2
    assert all(
        len(json.dumps(state)) < 20000 and "request_body" not in state
        for state in states
    )
    assert {state["fetched_at"] for state in states} == {
        provenance[name + "-search.json"]["original"]["timestamp"]
        for name in ("cclass", "c180")
    }
    db.conn.close()


@pytest.mark.parametrize("source_filter", [None, []])
@pytest.mark.parametrize("age,expected", [(31, False), (36, True)])
def test_unfiltered_fast_default_uses_actual_five_minute_interval(
    tmp_path, source_filter, age, expected
):
    db = Database(tmp_path / "market.sqlite3")
    stamp = (datetime.now(UTC) - timedelta(minutes=age)).isoformat()
    db.finish_source(
        db.start_run("fixture"),
        "s",
        "own",
        stamp,
        "complete",
        0,
        0,
        0,
        1,
        finished_at=stamp,
    )
    result = report(
        db.conn,
        {
            "queries": [
                {"source": "s", "key": "own", "url": "https://fixture.invalid"}
            ],
            "fast_sources": source_filter,
        },
    )
    assert result["queries"][0]["stale"] == expected
    db.conn.close()


@pytest.mark.parametrize("capture_request_matches", [True, False])
def test_cached_autokopen_normalization_keeps_capture_binding_and_clocks(
    tmp_path, monkeypatch, capture_request_matches
):
    from car_market import mercedes_collect

    case = next(row for row in CASES if row["source"] == "autokopen")
    clock = datetime.fromisoformat(case["captured_at"]) + timedelta(hours=1)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    monkeypatch.setattr(mercedes_collect, "datetime", FixedDatetime)
    db = Database(tmp_path / "market.sqlite3")
    worker = MercedesCollector(db, {**config(), "detail_cache_hours": 24})
    text = native(case)
    original = worker._parse("autokopen", text, case["url"]).listings[0]
    original.external_id = urlsplit(case["url"]).path.rsplit("-", 1)[-1]
    worker.queue_details(original, None)
    requested = (
        case["url"]
        if capture_request_matches
        else case["url"].replace("/auto/detail/", "/auto/detail/alias-")
    )
    worker.fetch_record(
        db.start_run("fixture"),
        "autokopen",
        Response(requested, case["url"], 200, text, case["captured_at"]),
    )
    before = [tuple(row) for row in db.conn.execute("SELECT * FROM fetches")]
    parsed = []
    parse = worker._parse

    def capture(*args):
        page = parse(*args)
        parsed.extend(page.listings)
        return page

    monkeypatch.setattr(worker, "_parse", capture)
    monkeypatch.setattr(
        worker, "_detail_get", lambda *args: pytest.fail("cap zero must not fetch")
    )
    worker.details(db.start_run("fixture"))
    assert len(parsed) == 1
    assert parsed[0].external_id == (
        original.external_id if capture_request_matches else case["url"]
    )
    assert parsed[0].price is None and parsed[0].raw.get("market_country") is None
    assert [tuple(row) for row in db.conn.execute("SELECT * FROM fetches")] == before
    assert db.conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
    assert worker.detail_stats["autokopen"]["attempted"] == 0
    assert worker.detail_stats["autokopen"]["deferred"] == 1
    db.conn.close()


def test_discovery_context_keeps_repeated_filter_value_order(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    worker = MercedesCollector(db, config())
    case = next(row for row in CASES if row["source"] == "marktnet")
    for suffix in ("&minprice=1&minprice=2", "&minprice=2&minprice=1"):
        url = case["url"] + suffix
        worker.ingest_response(
            db.start_run("fixture"),
            "marktnet",
            "duplicates",
            Response(url, url, 200, native(case), case["captured_at"]),
        )
    states = ledgers(db, "duplicates")
    assert len(states) == 2
    assert len({value["request_url"] for value in states.values()}) == 2
    assert db.conn.execute("SELECT count(*) FROM query_membership").fetchone()[0] == 0
    db.conn.close()
