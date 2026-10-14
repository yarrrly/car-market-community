"""BMW traversal stops on public refusal and never fabricates complete stock."""

import json

import pytest
import test_germany_bmw as cases
from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Response
from car_market.germany_bmw_collect import enumerate_bmw

STAMP = "2026-10-07T21:00:00Z"


@pytest.mark.parametrize("failure", ["budget", "403", "foreign"])
def test_bmw_partial_counter_refusal_and_country_guard(tmp_path, failure):
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, {"scope": {"country": "DE"}})
    body = cases.document()
    if failure == "foreign":
        for hit in body["hits"]:
            hit["vehicle"]["country"] = "NL"
    first = Response(
        cases.REQUEST["url"],
        cases.REQUEST["url"],
        200,
        json.dumps(body),
        STAMP,
        transport="browser_native_UI",
    )
    calls = []

    def fetch(request):
        calls.append(request)
        return Response(
            request["url"],
            request["url"],
            403,
            "public refusal",
            STAMP,
            "http_403",
            "browser_native_UI",
        )

    query = {
        "source": "bmw_de",
        "key": "controlled",
        "max_pages": 1 if failure == "budget" else 200,
        "pagination_certified": True,
    }
    result = enumerate_bmw(collector, db.start_run(), query, first, cases.REQUEST, fetch)
    assert result["status"] == "partial"
    assert result["received"] == (0 if failure == "foreign" else 12)
    assert len(calls) == (1 if failure == "403" else 0)
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == (
        0 if failure == "foreign" else 7
    )
    assert (
        db.conn.execute(
            "SELECT count(*) FROM events WHERE kind='not_seen_in_complete_query'"
        ).fetchone()[0]
        == 0
    )
    if failure == "403":
        assert db.conn.execute(
            "SELECT status,error FROM fetches ORDER BY id DESC LIMIT 1"
        ).fetchone()[:] == (403, "http_403")


@pytest.mark.parametrize("complete", [True, False])
def test_native_ui_waits_for_complete_body_with_timeout(monkeypatch, complete):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from car_market import germany_bmw as native

    state = {"clicked": False, "finished": False, "read": False}

    class Button:
        def evaluate(self, script):
            pass

        def click(self, **kwargs):
            state["clicked"] = True

    class Empty:
        def get_by_role(self, *args, **kwargs):
            return self

        def all(self):
            return []

    class Page:
        def locator(self, selector):
            return Button() if "show-more" in selector else Empty()

        @contextmanager
        def expect_response(self, predicate, timeout):
            yield SimpleNamespace(value="own response")

        @contextmanager
        def expect_request_finished(self, predicate, timeout):
            assert timeout == 30000
            yield
            if not complete:
                raise TimeoutError("native body unfinished")
            state["finished"] = True

    def result(page, response):
        assert state["finished"] and state["clicked"]
        state["read"] = True
        return {"status": 200}

    monkeypatch.setattr(native, "_native_ui_result", result)
    request = {**cases.REQUEST, "model_ranges": ["2_U06"]}
    if complete:
        assert native.browser_bmw_ui_next(Page(), request, 2)["status"] == 200
        assert state["read"]
    else:
        with pytest.raises(TimeoutError, match="unfinished"):
            native.browser_bmw_ui_next(Page(), request, 2)
        assert state["clicked"] and not state["read"]
