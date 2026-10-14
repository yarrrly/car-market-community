"""New collectors stay isolated and a failed channel cannot stop the market pass."""

import json
from pathlib import Path

from car_market.collect import Collector
from car_market.db import Database
from car_market.germany import canonical_de_id, parse_germany_page, source_for_url
from car_market.germany_mobile import DEALER_SOURCE


def test_new_publication_uri_namespaces_are_own_host_and_route():
    urls = [
        ("instamotion_de", "https://www.instamotion.com/de/auto/12345/bmw-218i", "12345"),
        ("carwow_de", "https://angebote.carwow.de/deals/" + "a" * 32, "a" * 32),
    ]
    for source, url, identity in urls:
        assert source_for_url(url) == source
        assert canonical_de_id(source, url) == identity
        assert canonical_de_id(source, url.replace("https://", "https://foreign.example/")) is None
        assert (
            canonical_de_id(
                source, url.replace("/deals/", "/stock_cars/").replace("/de/auto/", "/de/autos/")
            )
            is None
        )


def test_actual_mobile_dealer_detail_is_dispatched_with_native_post_ID():
    folder = Path(__file__).parent / "fixtures/germany_mobile"
    manifest = json.loads((folder / "recovery-provenance.json").read_text())
    # Select a retained own detail rather than the discovery search.
    rows = manifest if isinstance(manifest, list) else manifest["captures"]
    own = next(x for x in rows if x["fixture"].startswith("straussberger-detail"))
    page = parse_germany_page(
        DEALER_SOURCE, (folder / own["fixture"]).read_text(), own["metadata"]["final_url"]
    )
    assert len(page.listings) == 1
    item = page.listings[0]
    assert canonical_de_id(DEALER_SOURCE, item.url) == item.external_id
    assert item.raw["market_country"] == "DE"


def test_failure_in_one_german_query_preserves_following_channel(tmp_path, monkeypatch):
    config = {
        "scope": {"country": "DE"},
        "queries": [
            {"source": "instamotion_de", "key": "failed"},
            {"source": "pkw_de", "key": "next"},
        ],
    }
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, config)
    seen = []

    def query(run, spec):
        seen.append(spec["key"])
        if spec["key"] == "failed":
            raise ValueError("bounded source error")
        db.finish_source(
            run,
            spec["source"],
            spec["key"],
            "2026-10-08T05:00:00Z",
            "zero_results",
            0,
            0,
            0,
            1,
            None,
            infer_missing=False,
        )

    monkeypatch.setattr(collector, "collect_query", query)
    result = collector.run(details=False, checks=False)
    assert seen == ["failed", "next"]
    assert result["status"] == "partial"
    health = {r["query_key"]: r["status"] for r in db.conn.execute("select * from source_health")}
    assert health == {"failed": "error", "next": "zero_results"}
    assert not db.conn.execute("pragma foreign_key_check").fetchall()
