import gzip
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Response
from car_market.marktnet import parse_xml
from car_market.partitions import _marktnet_categories, collect_price_partitions

FIXTURES = Path(__file__).parent / "fixtures" / "marktnet_recovery"
URL = "https://www.marktnet.nl/zoeken.xml?q=218i&descr=on"


def identity(url):
    parsed = urlsplit(url)
    return parsed.path, tuple(sorted(parse_qs(parsed.query).items()))


def read_fixture(name):
    return gzip.decompress((FIXTURES / name).read_bytes()).decode("utf-8-sig")


class Replay:
    def __init__(self, failure=None, stale_root=False):
        self.entries = json.loads((FIXTURES / "manifest.json").read_text())["responses"]
        self.failure, self.stale_root = failure, stale_root
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        params = parse_qs(urlsplit(url).query)
        if self.failure and params.get("cat_id") == ["74"]:
            return Response(
                url,
                url,
                self.failure,
                None,
                "2026-10-07T11:35:00+00:00",
                "http_" + str(self.failure),
            )
        entry = next(item for item in self.entries if identity(item["url"]) == identity(url))
        body = read_fixture(entry["fixture"])
        error = None if entry["status"] == 200 else "http_" + str(entry["status"])
        return Response(url, url, entry["status"], body, entry["time"], error)


def collect(tmp_path, replay, limit=80):
    db = Database(tmp_path / "audit.sqlite3")
    collector = Collector(db, {})
    collector.fetcher = replay
    if replay.stale_root:
        ingest = collector.ingest_response
        stale_total = parse_xml(read_fixture("stale-root.xml.gz")).total

        def stale_ingest(run, source, key, response):
            page, targets = ingest(run, source, key, response)
            if parse_qs(urlsplit(response.url).query) == {
                "q": ["218i"],
                "descr": ["on"],
                "c": ["20"],
            }:
                # The real old counter with current authentic cards.
                page.total = stale_total
            return page, targets

        collector.ingest_response = stale_ingest
    result = collect_price_partitions(
        collector,
        db.start_run(),
        {"source": "marktnet", "key": "recovery", "url": URL, "partition_request_limit": limit},
    )
    stored = db.conn.execute("SELECT status,received_count FROM source_runs").fetchone()
    ids = {row[0] for row in db.conn.execute("SELECT external_id FROM listings")}
    db.conn.close()
    return result, stored, ids


def test_real_generic_recovery_matches_all_counters_and_unique_ids(tmp_path):
    replay = Replay()
    result, stored, ids = collect(tmp_path, replay)
    assert result["status"] == "complete"
    assert result["received"] == result["expected"] == 413
    assert result["errors"] == []
    assert result["pages"] == len(replay.calls)
    assert stored[0] == "complete" and stored[1] == 413
    assert "96711378" in ids
    # Gran Tourer counts toward coverage but is excluded from selection.
    assert any("96638138" in item.get("ids", []) for item in replay.entries)
    assert any(parse_qs(urlsplit(url).query).get("cat_id") == ["74"] for url in replay.calls)


def test_stale_real_root_is_partial_even_with_413_recovered_ids(tmp_path):
    result, stored, _ = collect(tmp_path, Replay(stale_root=True))
    assert result["status"] == stored[0] == "partial"
    assert result["expected"] == 412 and result["received"] == 413
    assert any("partition_counter_mismatch" in error for error in result["errors"])


@pytest.mark.parametrize("status", [403, 404, 500])
def test_category_transport_error_never_counts_as_zero(tmp_path, status):
    result, stored, _ = collect(tmp_path, Replay(failure=status))
    assert result["status"] == stored[0] == "partial"
    assert result["received"] < result["expected"]
    assert any(
        "category_recovery_failed:http_" + str(status) in error for error in result["errors"]
    )


def test_category_recovery_shares_the_root_request_budget(tmp_path):
    replay = Replay()
    result, _, _ = collect(tmp_path, replay, limit=5)
    assert result["status"] == "partial"
    assert len(replay.calls) == result["pages"] == 5
    assert any("partition_request_limit" in error for error in result["errors"])


def test_category_overlap_cannot_fill_missing_records():
    replay = Replay()
    entry = next(
        item
        for item in replay.entries
        if parse_qs(urlsplit(item["url"]).query).get("cat_id") == ["40"]
        and item.get("total") is not None
        and int(item["total"]) <= 20
    )
    page = parse_xml(read_fixture(entry["fixture"]))
    # Duplicate the entire authentic page without inventing IDs or prices.
    with pytest.raises(ValueError, match="category_overlap"):
        _marktnet_categories(lambda url: page, URL, 100, None, None)


def test_unknown_price_in_recovered_category_is_kept():
    replay = Replay()
    pages = {}
    for category in ("40", "74"):
        entry = next(
            item
            for item in replay.entries
            if parse_qs(urlsplit(item["url"]).query).get("cat_id") == [category]
            and item.get("total") is not None
            and int(item["total"]) <= 20
        )
        pages[category] = parse_xml(read_fixture(entry["fixture"]))
    # Model a missing price for an authentic ID, preserving the counter and inventory.
    unknown = pages["40"].listings[0]
    unknown.price = None
    recovered = _marktnet_categories(
        lambda url: pages[parse_qs(urlsplit(url).query)["cat_id"][0]], URL, 100, None, None
    )
    assert unknown in recovered.listings
    assert recovered.total == len({ad.external_id for ad in recovered.listings})
