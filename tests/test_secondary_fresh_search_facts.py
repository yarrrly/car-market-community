"""Fresh owned search facts survive incomplete or deferred VDP enrichment."""

import copy
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from test_secondary_detail_policy import collector, native

from car_market.fetch import Response
from car_market.mercedes_de_sources import PKW_SEARCH, parse_mercedes_de_sources
from car_market.model_profiles import MERCEDES_PROFILE, use_profile
from car_market.parse import Page


@pytest.mark.parametrize("detail_limit", [0, 2])
def test_newly_known_native_search_price_keeps_its_own_capture(tmp_path, detail_limit):
    owned_body, owned_meta = native("pkw-detail-142208737556145407.json")
    search_body, search_meta = native("pkw-c180-api-2.json")
    old_doc = json.loads(owned_body)
    old_doc["price"] = {}
    old_body = json.dumps(old_doc)
    identity = "142208737556145407"
    with use_profile(MERCEDES_PROFILE, "DE"):
        prior = parse_mercedes_de_sources(
            "pkw_de", old_body, owned_meta["final_url"]
        ).listings[0]
        fresh = next(
            x
            for x in parse_mercedes_de_sources(
                "pkw_de", search_body, search_meta["final_url"]
            ).listings
            if x.external_id == identity
        )
    assert prior.price is None and fresh.price == 26988
    worker = collector(tmp_path, detail_limit=detail_limit)
    old_run = worker.db.start_run("accepted_past_capture")
    old_clock = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    old_fetch = worker.db.save_fetch(
        old_run,
        "pkw_de",
        owned_meta["final_url"],
        owned_meta["final_url"],
        200,
        old_body,
        old_clock,
    )
    worker._observe(old_run, copy.deepcopy(prior), old_clock, old_fetch)
    original = [tuple(x) for x in worker.db.conn.execute("SELECT * FROM observations")]
    run = worker.db.start_run("test")
    search_clock = datetime.now(UTC).isoformat()

    def get(url, **kwargs):
        body = search_body if "/cars/search/basic?" in url else old_body
        return Response(url, url, 200, body, search_clock)

    worker._secondary_get = get
    real_parse = parse_mercedes_de_sources

    def one_card(source, body, url):
        if "/cars/search/basic?" in url:
            return Page([copy.deepcopy(fresh)], 1, 1, 1)
        return real_parse(source, body, url)

    with patch(
        "car_market.mercedes_de_sources.parse_mercedes_de_sources", side_effect=one_card
    ):
        worker._collect_secondary(
            run,
            {
                "source": "pkw_de",
                "key": "fresh",
                "url": PKW_SEARCH,
                "max_pages": 1,
                "detail_limit": 1,
            },
        )
    row = worker.db.conn.execute("SELECT * FROM latest_observations").fetchone()
    assert row["price_eur"] == 26988
    price = json.loads(row["details_json"])["raw"]["field_evidence"]["price"]
    proof = worker.db.conn.execute(
        "SELECT url,fetched_at FROM fetches WHERE id=?", (price["fetch_id"],)
    ).fetchone()
    assert "/cars/search/basic?" in proof["url"]
    assert price["observed_at"] == proof["fetched_at"] == search_clock
    assert [
        tuple(x)
        for x in worker.db.conn.execute("SELECT * FROM observations WHERE id=1")
    ] == original
    assert (
        worker.db.conn.execute(
            "SELECT fetched_at FROM fetches WHERE id=?", (old_fetch,)
        ).fetchone()[0]
        == old_clock
    )
    worker.db.conn.close()
