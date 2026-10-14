"""The legacy BMW composer must not bypass portable selector backoff."""

import copy

import pytest
from test_bmw_wiveda_composition import MANIFEST, NOW, history, retained, setup

from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database
from car_market.search_profile import GENERIC_PROFILE


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize(
    "make,model",
    [
        ("BMW", "218i"),
        ("Toyota", "Corolla"),
        ("Volkswagen", "Golf"),
        ("Tesla", "Model Y"),
    ],
)
def test_configured_selector_retains_recent_error_and_native_frontier(
    tmp_path, monkeypatch, fast, make, model
):
    db, original_worker, item, listing_id, _, _ = setup(tmp_path, monkeypatch)
    config = copy.deepcopy(original_worker.config)
    config["scope"] = {
        "country": "DE",
        "model_profile": GENERIC_PROFILE,
        "selector": {"make": make, "model_terms": [[model]], "exclude_terms": []},
    }
    db.conn.close()
    db = Database(tmp_path / "configured.sqlite3")
    worker = ConfiguredCollector(db, config)
    worker.fast = fast
    listing_id = worker._observe(db.start_run(), item, NOW.isoformat())
    if make != "BMW":
        assert listing_id is None
        assert item.scope == "excluded"
        assert db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        db.conn.close()
        return
    assert listing_id is not None
    worker.detail_jobs[item.url] = (item.source, listing_id, copy.deepcopy(item))
    db.save_fetch(
        db.start_run(),
        item.source,
        item.url,
        item.url,
        200,
        retained(MANIFEST[0]),
        NOW.isoformat(),
        error="detail_unparsed",
    )
    before = history(db)
    monkeypatch.setattr(
        worker.fetcher,
        "get",
        lambda *a, **k: pytest.fail("recent portable error retried"),
    )
    monkeypatch.setattr(
        "car_market.bmw_dedicated.fetch_wiveda_detail",
        lambda *a, **k: pytest.fail(
            "legacy native composer bypassed portable admission"
        ),
    )
    worker.details(db.start_run())
    assert history(db) == before
    assert set(worker.detail_jobs) == {item.url}
    assert worker.detail_stats[item.source]["backoff"] == 1
    assert (
        worker.specialized_detail_policy(
            worker.db.conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        ).used
        == 0
    )
    db.conn.close()
