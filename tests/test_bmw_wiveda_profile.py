"""The BMW composition boundary cannot capture another current model scope."""

import copy
import json
from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import test_bmw_wiveda_composition as bmw
import test_mercedes_wiveda_composition as mercedes

from car_market.bmw_dedicated import bmw_wiveda_context, fetch_wiveda_detail
from car_market.bmw_detail_policy import DetailDeferred
from car_market.collect import Collector
from car_market.fetch import Response
from car_market.mercedes_collect import MercedesCollector
from car_market.mercedes_wiveda_details import route_from_capture
from car_market.model_profiles import BMW_PROFILE, MERCEDES_PROFILE, use_profile


def current_context(worker, item, case):
    if case == "config_mercedes":
        worker.config["scope"]["model_profile"] = MERCEDES_PROFILE
    elif case == "config_generic":
        worker.config["scope"]["model_profile"] = "generic_corolla"
    elif case == "config_country":
        worker.config["scope"]["country"] = "NL"
    elif case == "bound_mercedes":
        worker.db.conn.execute(
            "INSERT OR REPLACE INTO monitor_state VALUES ('market_scope',?)",
            (json.dumps({"country": "DE", "model_profile": MERCEDES_PROFILE}),),
        )
        worker.db.conn.commit()
    elif case == "bound_country":
        worker.db.conn.execute(
            "INSERT OR REPLACE INTO monitor_state VALUES ('market_scope',?)",
            (json.dumps({"country": "NL", "model_profile": BMW_PROFILE}),),
        )
        worker.db.conn.commit()
    elif case == "class_mercedes":
        replacement = object.__new__(MercedesCollector)
        replacement.__dict__.update(worker.__dict__)
        worker = replacement
    elif case == "item_mercedes":
        item.raw["model_profile"] = MERCEDES_PROFILE
    elif case == "item_generic":
        item.raw["model_profile"] = "generic_corolla"
    elif case == "native_brand":
        item.raw["native_vehicle"]["brand"] = "Mercedes-Benz"
    elif case == "current_brand":
        item.raw["model_brand"] = "Toyota"
    if case == "active_mercedes":
        return worker, use_profile(MERCEDES_PROFILE, "DE")
    return worker, nullcontext()


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        "config_mercedes",
        "config_generic",
        "config_country",
        "bound_mercedes",
        "class_mercedes",
        "active_mercedes",
        "bound_country",
        "item_mercedes",
        "item_generic",
        "native_brand",
        "current_brand",
    ],
)
def test_shared_helper_current_scope_guard_precedes_HTTP_and_accepted_cache(
    tmp_path, monkeypatch, cached, case
):
    db, worker, item, listing_id, calls, _ = bmw.setup(tmp_path, monkeypatch)
    if cached:
        worker.details(db.start_run())
        worker.config["detail_limit"] = 0
    item = copy.deepcopy(item)
    worker, context = current_context(worker, item, case)
    before = bmw.history(db)
    prior_calls = calls.copy()
    with context:
        assert not bmw_wiveda_context(worker, item)
        with pytest.raises(DetailDeferred, match="detail_profile_mismatch"):
            fetch_wiveda_detail(worker, db.start_run(), item, listing_id=listing_id)
    assert bmw.history(db) == before and calls == prior_calls


@pytest.mark.parametrize(
    "case",
    [
        "config_mercedes",
        "config_generic",
        "config_country",
        "bound_mercedes",
        "class_mercedes",
        "active_mercedes",
        "bound_country",
    ],
)
def test_base_dispatch_keeps_original_route_outside_current_BMW_context(
    tmp_path, monkeypatch, case
):
    db, worker, item, _, _, _ = bmw.setup(tmp_path, monkeypatch)
    worker.config["detail_cache_hours"] = 0
    worker, context = current_context(worker, item, case)
    monkeypatch.setattr(
        "car_market.bmw_dedicated.fetch_wiveda_detail",
        lambda *args, **kwargs: pytest.fail("BMW helper must not steal this profile"),
    )
    calls = []

    def generic_get(url, **kwargs):
        calls.append(url)
        return Response(
            url, url, 200, bmw.retained(bmw.MANIFEST[0]), bmw.MANIFEST[0]["fetched_at"]
        )

    worker.fetcher = SimpleNamespace(get=generic_get)
    before = bmw.history(db)
    with context:
        Collector.details(worker, run := db.start_run())
    assert calls == [item.url]
    assert bmw.history(db)["observations"] == before["observations"]
    assert bmw.history(db)["events"] == before["events"]
    assert bmw.portal(db, run)["status"] == "partial"


@pytest.mark.parametrize(
    "case", ["item_mercedes", "item_generic", "native_brand", "current_brand"]
)
def test_BMW_dispatch_retains_mismatched_own_native_item_without_GET(
    tmp_path, monkeypatch, case
):
    db, worker, item, _, calls, _ = bmw.setup(tmp_path, monkeypatch)
    worker, context = current_context(worker, item, case)
    before = bmw.history(db)
    with context:
        worker.details(run := db.start_run())
    assert calls == [] and bmw.history(db) == before
    assert (
        worker.detail_stats[item.source]["held"] == 1
        and bmw.portal(db, run)["status"] == "partial"
    )


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("unknown_brand", [False, True])
def test_current_BMW_profile_retains_free_cached_native_proof(
    tmp_path, monkeypatch, explicit, unknown_brand
):
    db, worker, item, _, calls, _ = bmw.setup(tmp_path, monkeypatch)
    worker.details(db.start_run())
    before = bmw.history(db)
    worker.config["detail_limit"] = 0
    if explicit:
        worker.config["scope"]["model_profile"] = BMW_PROFILE
        item.raw["model_profile"] = BMW_PROFILE
    if unknown_brand:
        item.raw["model_brand"] = None
        item.raw["native_vehicle"]["brand"] = None
    with use_profile(BMW_PROFILE, "DE"):
        worker.details(run := db.start_run())
    assert worker.detail_stats[item.source]["cached"] == 1
    assert len(calls) == 2 and bmw.history(db) == before
    assert bmw.portal(db, run)["status"] == "complete"


@pytest.mark.parametrize("identity", mercedes.IDS)
@pytest.mark.parametrize("delta", [-0.001, 0, 0.001])
def test_Mercedes_original_route_TTL_stays_strict_after_clock_oracle_fix(
    tmp_path, identity, delta
):
    replay = mercedes.Replay(tmp_path)
    body, meta = mercedes.native(identity, "html")
    run = replay.worker.db.start_run()
    fetch_id = replay.worker.fetch_record(
        run,
        mercedes.SOURCE,
        Response(
            meta["url"],
            meta["url"],
            200,
            body,
            meta["original_clock"],
            error="detail_unparsed",
        ),
    )
    row = replay.worker.db.conn.execute(
        "SELECT * FROM fetches WHERE id=?", (fetch_id,)
    ).fetchone()
    clock = datetime.fromisoformat(meta["original_clock"]) + timedelta(
        hours=6, seconds=delta
    )
    route = route_from_capture(replay.worker, meta["url"], row, clock)
    assert bool(route) == (delta < 0)
    assert replay.calls == []
