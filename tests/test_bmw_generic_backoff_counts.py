"""Bounded canonical generic scheduling controls with three owned native targets."""

import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from test_bmw_detail_policy import NativeReplay


@pytest.mark.parametrize("global_limit", [1, 4])
def test_native_generic_backoff_counts_each_publication_once(tmp_path, monkeypatch, global_limit):
    replay = NativeReplay(tmp_path, monkeypatch, "instamotion_de")
    replay.config.update(detail_cache_hours=0, detail_limit=global_limit)
    native = Path(__file__).parent / "fixtures/germany_additional"
    manifest = {m["fixture"]: m for m in json.loads((native / "manifest.json").read_text())}
    try:
        run = replay.run()
        selected = []
        evidence = []
        for name in ("instamotion.html", "instamotion-page2.html", "instamotion-page3.html"):
            m = manifest[name]
            body = (native / name).read_text()
            digest = hashlib.sha256((native / name).read_bytes()).hexdigest()
            assert digest == m["fixture_sha256"]
            targets = [
                x
                for x in replay.collector._parse(replay.source, body, m["final_uri"]).listings
                if x.scope == "target"
            ]
            assert len(targets) == 1 and targets[0].raw["market_country"] == "DE"
            item = targets[0]
            row = replay.db.conn.execute(
                "SELECT id FROM listings WHERE source=? AND external_id=?",
                (item.source, item.external_id),
            ).fetchone()
            if row is None:
                fetch_id = replay.db.save_fetch(
                    run,
                    item.source,
                    m["final_uri"],
                    m["final_uri"],
                    200,
                    body,
                    replay.stamp(),
                    "native-offline-replay",
                    None,
                )
                listing_id = replay.collector._observe(
                    run, item, replay.stamp(), fetch_id, "native-generic-backoff"
                )
                assert listing_id is not None
            else:
                listing_id = row[0]
            selected.append((item.url, (item.source, listing_id, item)))
            evidence.append(
                {
                    "fixture": name,
                    "fixture_sha256": digest,
                    "source_wire_sha256": m["source_wire_sha256"],
                    "source_fetched_at": m["fetched_at"],
                    "public_url": m["final_uri"],
                    "external_id": item.external_id,
                    "scope": item.scope,
                    "country": item.raw["market_country"],
                }
            )
        assert len({job[1][2].external_id for job in selected}) == 3
        replay.collector.detail_jobs = dict(selected)
        old = {
            table: [tuple(r) for r in replay.db.conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("fetches", "observations", "events")
        }
        before = replay.detail_calls()
        replay.blocked_status = 429
        with redirect_stdout(io.StringIO()):
            replay.collector.details(run)
        summary = dict(
            replay.db.conn.execute(
                "SELECT * FROM source_runs WHERE run_id=? AND source='portal_details'", (run,)
            ).fetchone()
        )
        actual_calls = replay.detail_calls() - before
        immutable = all(
            [tuple(r) for r in replay.db.conn.execute(f"SELECT * FROM {table} ORDER BY id")][
                : len(rows)
            ]
            == rows
            for table, rows in old.items()
        )
        assert immutable
        assert actual_calls == (1 if global_limit == 4 else 0)
        assert replay.collector.specialized_detail_policy(run).used == (
            2 if global_limit == 4 else 1
        )
        assert summary["received_count"] == actual_calls
        assert summary["expected_count"] == 3, (
            "Generic census counts each queued native publication once, including backoff"
        )
    finally:
        replay.db.conn.close()
