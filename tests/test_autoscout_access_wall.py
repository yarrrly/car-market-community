"""Known HTTP200 access walls retain original bytes without inventory credit."""

import gzip
import hashlib

import pytest
from car_market.collect import Collector
from test_autoscout_page_5xx import FIRST, collect, recovery, response
from test_autoscout_page_5xx import setup as setup  # noqa: PLC0414 - shared pytest fixture


def wall(reply, gate):
    title, marker = ("Login", "") if gate == "login" else ("Just a moment", "captcha")
    reply.body = (
        "<html><head><title>" + title + "</title></head><body>" + marker
        + '<script id="__NEXT_DATA__" type="application/json">'
        + reply.body + "</script></body></html>"
    )
    return reply


@pytest.mark.parametrize("gate", ["login", "challenge"])
@pytest.mark.parametrize("phase", ["first", "later", "refreshed"])
def test_known_access_wall_has_no_inventory_or_fresh_fact_credit(setup, gate, phase):
    db, collector, query, calls = setup
    held = wall(response(FIRST) if phase == "first" else recovery(), gate)
    if phase == "first":
        def get(url, refresh=False, **kwargs):
            calls.append((url, refresh, kwargs))
            return held

        collector.fetcher.get = get
        run = db.start_run("native_access_wall")
        Collector.collect_query(collector, run, query)
        health = dict(db.conn.execute("SELECT * FROM source_runs WHERE run_id=?", (run,)).fetchone())
        assert health["pages"] == health["received_count"] == 0
        assert len(calls) == 1
    else:
        run, health = collect(
            setup,
            initial=held if phase == "later" else None,
            recovered=held if phase == "refreshed" else None,
        )
        original = collector._parse("autoscout24", response(FIRST).body, FIRST["url"])
        assert health["pages"] == 1
        assert health["received_count"] == len({x.external_id for x in original.listings})
        assert len(calls) == (2 if phase == "later" else 3)
    assert health["status"] in {"partial", "error"}
    assert "access_response_held" in health["error"]
    assert db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    rows = db.conn.execute("SELECT * FROM fetches WHERE run_id=? ORDER BY id", (run,)).fetchall()
    assert len(rows) == len(calls)
    held_row = rows[-1]
    assert held_row["status"] == 200 and held_row["error"] is None
    assert held_row["fetched_at"] == held.timestamp
    assert held_row["content_hash"] == hashlib.sha256(held.body.encode()).hexdigest()
    assert gzip.decompress((db.path.parent / held_row["raw_path"]).read_bytes()).decode() == held.body
