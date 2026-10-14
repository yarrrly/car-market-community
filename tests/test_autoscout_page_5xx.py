"""Native AS500 pagination recovery uses one existing refresh and exact proofs."""

import copy
import gzip
import hashlib
import json
from pathlib import Path

import pytest
from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Response
from car_market.germany_autoscout import _state
from car_market.mercedes_collect import MercedesCollector
from car_market.model_profiles import MERCEDES_PROFILE

FIXTURES = Path(__file__).parent / "fixtures/autoscout_page_5xx"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
FIRST = next(m for m in MANIFEST if m["id"] == 1)
FAILURE = next(m for m in MANIFEST if m["id"] == 2)
LATER = next(m for m in MANIFEST if m["id"] == 3)


def body(meta):
    encoded = (FIXTURES / meta["fixture"]).read_bytes()
    assert hashlib.sha256(encoded).hexdigest() == meta["fixture_sha256"]
    decoded = gzip.decompress(encoded)
    assert hashlib.sha256(decoded).hexdigest() == meta["content_hash"]
    return decoded.decode()


def response(meta):
    return Response(
        meta["url"],
        meta["final_url"],
        meta["status"],
        body(meta),
        meta["fetched_at"],
        meta["error"],
        meta["transport"],
    )


def recovery(**changes):
    # Controlled recovery derived from original successful page3, never live page2.
    document = json.loads(body(LATER))
    state = document["pageProps"]
    state["pageQuery"]["page"] = "2"
    state["pagePath"] = state["pagePath"].replace("page=3", "page=2")
    state.update(changes)
    result = Response(
        FAILURE["url"],
        FAILURE["url"],
        200,
        json.dumps(document),
        LATER["fetched_at"],
        transport="retained_native_controlled_page_number",
    )
    return result


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    collector = MercedesCollector(
        db,
        {
            "scope": {"country": "NL", "model_profile": MERCEDES_PROFILE},
            "queries": [],
            "workers": 1,
        },
    )
    query = {
        "source": "autoscout24",
        "key": "native_corolla_query",
        "url": FIRST["url"],
        "pagination": "mercedes_autoscout_next",
        "coverage": "enumerate",
        "infer_missing": False,
        "max_pages": 2,
    }
    calls = []
    yield db, collector, query, calls
    db.conn.close()


def collect(setup, initial=None, recovered=None, mutate_query=None):
    db, collector, query, calls = setup
    initial = initial or response(FAILURE)
    recovered = recovered or recovery()
    if mutate_query:
        query.update(mutate_query)

    def get(url, refresh=False, **kwargs):
        calls.append((url, refresh, kwargs))
        if url == FIRST["url"]:
            return response(FIRST)
        assert url == FAILURE["url"]
        if refresh and isinstance(recovered, BaseException):
            raise recovered
        return copy.copy(recovered if refresh else initial)

    collector.fetcher.get = get
    run = db.start_run("retained_native_offline")
    # Actual canonical core entrypoint, actual canonical Mercedes AS parser hook.
    Collector.collect_query(collector, run, query)
    return run, dict(db.conn.execute("SELECT * FROM source_runs WHERE run_id=?", (run,)).fetchone())


@pytest.mark.parametrize("meta", MANIFEST, ids=lambda m: str(m["id"]))
def test_original_native_capture_hash_and_clock(meta):
    assert body(meta)
    assert meta["fetched_at"].startswith("2026-10-12T00:31:")


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_transient_as_get_uses_one_refresh_and_keeps_original_capture_proofs(setup, status):
    failed = response(FAILURE)
    failed.status, failed.error = status, "http_" + str(status)
    good = recovery()
    run, health = collect(setup, failed, good)
    db, collector, _, calls = setup
    assert [(u, r) for u, r, _ in calls] == [
        (FIRST["url"], False),
        (FAILURE["url"], False),
        (FAILURE["url"], True),
    ]
    rows = db.conn.execute(
        "SELECT * FROM fetches WHERE run_id=? AND url=? ORDER BY id", (run, FAILURE["url"])
    ).fetchall()
    assert len(rows) == 2 and rows[0]["id"] != rows[1]["id"]
    for row, expected in zip(rows, (failed, good)):
        assert row["fetched_at"] == expected.timestamp
        assert row["status"] == expected.status and row["error"] == expected.error
        assert row["content_hash"] == hashlib.sha256(expected.body.encode()).hexdigest()
        assert (
            gzip.decompress((db.path.parent / row["raw_path"]).read_bytes()).decode()
            == expected.body
        )
    first = collector._parse("autoscout24", body(FIRST), FIRST["url"])
    next_page = collector._parse("autoscout24", good.body, good.final_url)
    assert health["received_count"] == len(
        {x.external_id for x in first.listings + next_page.listings}
    )
    assert health["pages"] == 2 and "http_" + str(status) not in (health["error"] or "")
    assert all(x.raw["model_brand"] == "Toyota" for x in next_page.listings)
    # The Corolla census cannot borrow the active canonical Mercedes model binding.
    assert db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0


@pytest.mark.parametrize(
    "fault",
    ["wrong_page", "country", "model_filter", "counter", "page_count", "missing_counter", "repeat"],
)
def test_recovered_native_shape_is_revalidated_without_third_call(setup, fault):
    good = recovery()
    document = json.loads(good.body)
    state = document["pageProps"]
    if fault == "wrong_page":
        state["pageQuery"]["page"] = "3"
    elif fault == "country":
        state["pageQuery"]["cy"] = "D"
    elif fault == "model_filter":
        state["pageQuery"]["cat"] = "contradictory-native-selector"
    elif fault == "counter":
        state["numberOfResults"] += 1
    elif fault == "page_count":
        state["numberOfPages"] += 1
    elif fault == "missing_counter":
        state["numberOfResults"] = None
    else:
        first_state, *_ = _state(body(FIRST))
        state["listings"] = first_state["listings"]
    good.body = json.dumps(document)
    run, health = collect(setup, recovered=good)
    db, _, _, calls = setup
    assert sum(u == FAILURE["url"] for u, _, _ in calls) == 2
    assert health["status"] == "partial"
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM fetches WHERE run_id=? AND url=?", (run, FAILURE["url"])
        ).fetchone()[0]
        == 2
    )
    if fault not in {"counter", "page_count"}:
        assert health["pages"] == 1
    else:
        assert ("counter_changed" if fault == "counter" else "page_count_changed") in health[
            "error"
        ]


@pytest.mark.parametrize(
    "status,error,gate",
    [
        (403, "http_403", None),
        (429, "http_429", None),
        (501, "http_501", None),
        (500, "login_required", None),
        (500, "captcha_required", None),
        (503, "http_503", "<title>Login</title>"),
        (503, "http_503", "<title>Just a moment</title>captcha"),
        (200, "unrelated_transport_error", None),
    ],
)
def test_blocks_login_captcha_and_other_errors_are_never_refreshed(setup, status, error, gate):
    failed = response(FAILURE)
    failed.status, failed.error = status, error
    if gate:
        failed.body = gate
    run, health = collect(setup, failed)
    db, _, _, calls = setup
    assert not any(refresh for _, refresh, _ in calls)
    row = db.conn.execute(
        "SELECT * FROM fetches WHERE run_id=? AND url=?", (run, FAILURE["url"])
    ).fetchone()
    assert row["status"] == status and row["error"] == error
    assert health["pages"] == 1


@pytest.mark.parametrize("query", [{"request_body": {}}, {"method": "POST"}])
def test_post_queries_never_gain_a_transient_refresh(setup, query):
    _, health = collect(setup, mutate_query=query)
    assert not any(refresh for _, refresh, _ in setup[3])
    assert health["pages"] == 1


@pytest.mark.parametrize("status,error", [(503, "http_503"), (500, None), (200, "http_500")])
def test_recovery_error_body_is_saved_before_native_filter_parsing(setup, status, error):
    failed_recovery = recovery()
    failed_recovery.status, failed_recovery.error = status, error
    document = json.loads(failed_recovery.body)
    document["pageProps"]["pageQuery"]["cy"] = "D"
    failed_recovery.body = json.dumps(document)
    run, health = collect(setup, recovered=failed_recovery)
    db, _, _, calls = setup
    rows = db.conn.execute(
        "SELECT * FROM fetches WHERE run_id=? AND url=? ORDER BY id", (run, FAILURE["url"])
    ).fetchall()
    assert len(rows) == 2 and rows[1]["status"] == status and rows[1]["error"] == error
    assert "search_filters_changed" not in health["error"]
    assert health["pages"] == 1 and sum(u == FAILURE["url"] for u, _, _ in calls) == 2


def test_original_getter_stop_interrupt_remains_terminal(setup):
    with pytest.raises(KeyboardInterrupt, match="controlled stopping"):
        collect(setup, recovered=KeyboardInterrupt("controlled stopping"))
    assert sum(u == FAILURE["url"] for u, _, _ in setup[3]) == 2


def test_transient_redirect_cannot_refresh_a_new_endpoint(setup):
    failed = response(FAILURE)
    failed.final_url = "https://www.autoscout24.nl/account/login"
    _, health = collect(setup, failed)
    assert not any(refresh for _, refresh, _ in setup[3])
    assert health["pages"] == 1


@pytest.mark.parametrize("status,error", [(500, None), (503, "http_503"), (200, "http_500")])
def test_first_response_error_cannot_count_native_inventory(setup, status, error):
    db, collector, query, calls = setup
    failed = response(FIRST)
    failed.status, failed.error = status, error

    def get(url, refresh=False, **kwargs):
        calls.append((url, refresh, kwargs))
        return failed

    collector.fetcher.get = get
    run = db.start_run("retained_native_offline")
    Collector.collect_query(collector, run, query)
    assert len(calls) == 1 and not calls[0][1]
    row = db.conn.execute("SELECT * FROM fetches WHERE run_id=?", (run,)).fetchone()
    assert row["status"] == status and row["error"] == error
    health = db.conn.execute("SELECT * FROM source_runs WHERE run_id=?", (run,)).fetchone()
    assert health["pages"] == 0 and health["received_count"] == 0


def test_existing_malformed_200_repair_keeps_both_captures_and_one_refresh(setup):
    initial = recovery()
    initial.body = body(LATER)  # Native page3 on the requested page2 URI.
    run, health = collect(setup, initial=initial)
    db, _, _, calls = setup
    assert sum(u == FAILURE["url"] for u, _, _ in calls) == 2
    rows = db.conn.execute(
        "SELECT * FROM fetches WHERE run_id=? AND url=? ORDER BY id", (run, FAILURE["url"])
    ).fetchall()
    assert len(rows) == 2 and all(r["status"] == 200 for r in rows)
    assert rows[0]["content_hash"] == LATER["content_hash"]
    assert health["pages"] == 2


def test_recovered_parser_exception_preserves_second_raw(setup, monkeypatch):
    db, collector, _, calls = setup
    good = recovery()
    original = collector._parse

    def parse(source, text, url):
        if text == good.body:
            raise ValueError("controlled native parser boundary")
        return original(source, text, url)

    monkeypatch.setattr(collector, "_parse", parse)
    run, health = collect(setup, recovered=good)
    rows = db.conn.execute(
        "SELECT * FROM fetches WHERE run_id=? AND url=? ORDER BY id", (run, FAILURE["url"])
    ).fetchall()
    assert (
        len(rows) == 2 and rows[1]["content_hash"] == hashlib.sha256(good.body.encode()).hexdigest()
    )
    assert "controlled native parser boundary" in health["error"]
    assert health["pages"] == 1 and sum(u == FAILURE["url"] for u, _, _ in calls) == 2
