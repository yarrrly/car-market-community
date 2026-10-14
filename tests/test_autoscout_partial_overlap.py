"""A bound native partial-overlap page is distinct from full-repeat and invalid binding."""

import copy
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pytest

from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database
from car_market.fetch import Response

ROOT = Path(__file__).parent / "fixtures/autoscout_held_ids"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())
PAIRS = [(67, 68), (104, 105), (106, 107), (203, 204)]


def meta(fid):
    return next(row for row in MANIFEST if row["fetch_id"] == fid)


def captured(fid):
    row = meta(fid)
    raw = (ROOT / row["fixture"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == row["fixture_sha256"]
    return json.loads(raw)


def state(doc):
    return doc.get("pageProps") or doc["props"]["pageProps"]


def page_url(page):
    original = meta(67)["url"]
    split = urlsplit(original)
    query = parse_qs(split.query)
    query["page"] = [str(page)]
    return urlunsplit(split._replace(query=urlencode(query, doseq=True)))


def on_page(doc, page):
    result = copy.deepcopy(doc)
    own = state(result)
    own["pageQuery"]["page"] = str(page)
    route = urlsplit(own["pagePath"])
    query = parse_qs(route.query)
    query["page"] = [str(page)]
    own["pagePath"] = urlunsplit(route._replace(query=urlencode(query, doseq=True)))
    return result


def setup(tmp_path):
    query = {
        "source": "autoscout24_de",
        "key": "native",
        "url": meta(1)["url"],
        "pagination": "mercedes_autoscout_next",
        "coverage": "enumerate",
        "infer_missing": False,
    }
    config = {
        "schema_version": 1,
        "scope": {
            "model_profile": "configured_vehicle",
            "country": "DE",
            "selector": {"make": "Volkswagen", "model_terms": [["Golf"]]},
        },
        "queries": [query],
        "max_pages": 2,
        "workers": 1,
        "detail_limit": 0,
        "detail_batch_limit": 0,
    }
    db = Database(tmp_path / "market.sqlite3")
    return db, ConfiguredCollector(db, config), query


def pending(db):
    return {
        row["key"].split(":", 2)[-1]: json.loads(row["value"])
        for row in db.conn.execute(
            "SELECT key,value FROM monitor_state WHERE key LIKE 'pending:%'"
        )
    }


def fetch_body(db, fid):
    row = db.conn.execute("SELECT * FROM fetches WHERE id=?", (fid,)).fetchone()
    raw = gzip.decompress((db.path.parent / row["raw_path"]).read_bytes())
    assert hashlib.sha256(raw).hexdigest() == row["content_hash"]
    return row, raw


def scenario(tmp_path, pair, mode, fault=None):
    db, worker, query = setup(tmp_path)
    first = captured(1)
    old, fresh = [on_page(captured(fid), 2) for fid in pair]
    missing = set(meta(pair[0])["missing_ids"])
    repeated = [row for row in state(old)["listings"] if row["id"] not in missing]
    # Controlled page placement reproduces native overlaps. Each own card's
    # ID/URI/country/model/price remains unchanged; this is not a page1 census claim.
    state(first)["listings"] = (
        state(old)["listings"] if mode == "full_repeat" else repeated
    )
    if mode == "refresh_partial":
        state(old)["listings"] = repeated
    if fault:
        for doc in (old, fresh):
            own = state(doc)
            if fault == "page":
                own["pageQuery"]["page"] = "3"
            elif fault == "counter":
                own["numberOfResults"] = None
            elif fault == "filter":
                own["pageQuery"]["fuel"] = "B"
            elif fault == "country_context":
                own["pageQuery"]["cy"] = "NL"
    bodies = [json.dumps(doc) for doc in (first, old, fresh)]
    clocks = [meta(fid)["fetched_at"] for fid in (1, *pair)]
    calls = []

    def get(url, refresh=False):
        index = 0 if url == query["url"] else 2 if refresh else 1
        calls.append((url, refresh))
        status = 403 if fault == "denial" and index else 200
        error = "http_403" if status == 403 else None
        return Response(url, url, status, bodies[index], clocks[index], error=error)

    worker.fetcher = SimpleNamespace(get=get)
    run = db.start_run(timestamp=clocks[0])
    worker.collect_query(run, query)
    health = db.conn.execute("SELECT * FROM source_runs").fetchone()
    return (
        db,
        health,
        missing,
        {r["id"] for r in state(first)["listings"]},
        calls,
        clocks,
        bodies,
    )


@pytest.mark.parametrize("pair", PAIRS)
@pytest.mark.parametrize("mode", ["original_partial", "refresh_partial"])
def test_bound_native_majority_overlap_ingests_novel_minority(tmp_path, pair, mode):
    db, health, missing, initial, calls, clocks, bodies = scenario(tmp_path, pair, mode)
    stored = {r[0] for r in db.conn.execute("SELECT external_id FROM listings")}
    assert stored == initial | missing
    assert not pending(db)
    assert health["received_count"] == health["target_count"] == len(stored)
    assert health["status"] == "partial" and "page_limit_exceeded" in health["error"]
    assert "empty_or_repeated_page" not in health["error"]
    assert len(calls) == (2 if mode == "original_partial" else 3)
    assert sum(refresh for _url, refresh in calls) == (mode == "refresh_partial")
    expected = 1 if mode == "original_partial" else 2
    for identity in missing:
        row = db.conn.execute(
            "SELECT o.* FROM latest_observations o JOIN listings l ON l.id=o.listing_id WHERE l.external_id=?",
            (identity,),
        ).fetchone()
        assert row["observed_at"] == clocks[expected]
        capture, raw = fetch_body(db, row["fetch_id"])
        assert raw.decode() == bodies[expected]
        assert (
            capture["fetched_at"] == clocks[expected]
            and capture["status"] == 200
            and capture["error"] is None
        )
        payload = json.loads(row["details_json"])
        assert (
            payload["raw"]["location"]["countryCode"]
            == payload["raw"]["market_country"]
            == "DE"
        )
        assert payload["scope"] == "target" and identity in payload["url"]
    db.conn.close()


@pytest.mark.parametrize("pair", PAIRS)
def test_native_full_repeat_still_refreshes_once_and_stays_held(tmp_path, pair):
    db, health, _missing, initial, calls, clocks, bodies = scenario(
        tmp_path, pair, "full_repeat"
    )
    assert {
        r[0] for r in db.conn.execute("SELECT external_id FROM listings")
    } == initial
    assert health["received_count"] == health["target_count"] == len(initial)
    assert (
        health["status"] == "partial" and "empty_or_repeated_page:2" in health["error"]
    )
    assert len(calls) == 3 and calls[-1] == (page_url(2), True)
    assert {r[0] for r in db.conn.execute("SELECT observed_at FROM observations")} == {
        clocks[0]
    }
    assert {r[0] for r in db.conn.execute("SELECT last_seen FROM listings")} == {
        clocks[0]
    }
    for index, row in enumerate(db.conn.execute("SELECT id FROM fetches ORDER BY id")):
        capture, raw = fetch_body(db, row["id"])
        assert raw.decode() == bodies[index] and capture["fetched_at"] == clocks[index]
        assert capture["status"] == 200 and capture["error"] is None
    db.conn.close()


@pytest.mark.parametrize(
    "fault", ["page", "counter", "filter", "country_context", "denial"]
)
def test_novel_minority_never_overrides_native_context_or_denial_guards(
    tmp_path, fault
):
    db, health, missing, initial, calls, clocks, bodies = scenario(
        tmp_path, PAIRS[0], "original_partial", fault
    )
    assert {
        r[0] for r in db.conn.execute("SELECT external_id FROM listings")
    } == initial
    assert not pending(db)
    assert health["status"] == "partial" and health["received_count"] == len(initial)
    assert not db.conn.execute(
        "SELECT 1 FROM listings WHERE external_id IN ("
        + ",".join("?" for _ in missing)
        + ")",
        sorted(missing),
    ).fetchone()
    assert len(calls) <= 3
    if fault == "denial":
        assert len(calls) == 2 and "access_response_held" in health["error"]
    for index, row in enumerate(db.conn.execute("SELECT id FROM fetches ORDER BY id")):
        capture, raw = fetch_body(db, row["id"])
        assert raw.decode() == bodies[index] and capture["fetched_at"] == clocks[index]
        if fault != "denial" or index == 0:
            assert capture["status"] == 200 and capture["error"] is None
        else:
            assert capture["status"] == 403 and capture["error"] == "http_403"
    db.conn.close()
