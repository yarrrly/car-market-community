"""Run-local detail switches and durable BMW source fairness with native fixtures."""

import io
import json
import urllib.request
from contextlib import redirect_stdout
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from test_bmw_detail_policy import CASES, NativeReplay


@pytest.mark.parametrize("source", CASES)
@pytest.mark.parametrize("fast", [False, True])
def test_no_details_retains_census_pending_and_restores_next_run(
    tmp_path, monkeypatch, source, fast
):
    replay = NativeReplay(tmp_path, monkeypatch, source, fast)
    try:
        replay.config["queries"] = [dict(replay.query)]
        original_config = json.dumps(replay.config, sort_keys=True)
        with redirect_stdout(io.StringIO()):
            replay.collector.run(details=False, checks=False, fast=fast)
        assert replay.detail_calls() == 0 and replay.accepted() == 0
        assert replay.calls and replay.collector.specialized_details_enabled
        states = [
            json.loads(row[0])
            for row in replay.db.conn.execute(
                "SELECT value FROM monitor_state WHERE key LIKE 'germany_additional:%' OR key LIKE 'germany_discovery:%' OR key LIKE 'kleinanzeigen_discovery:%'"
            )
        ]
        assert len(states) == 1 and states[0]["cards"] and states[0]["pending_details"]
        assert any(
            row.get("reason") == "details_disabled"
            for row in states[0]["pending_details"].values()
        )
        assert json.dumps(replay.config, sort_keys=True) == original_config
        replay.clock += timedelta(minutes=5)
        with redirect_stdout(io.StringIO()):
            replay.collector.run(details=True, checks=False, fast=fast)
        assert replay.detail_calls() >= 1 and replay.accepted() > 0
    finally:
        replay.db.conn.close()


def test_no_details_flag_is_restored_after_interrupted_run(tmp_path, monkeypatch):
    replay = NativeReplay(tmp_path, monkeypatch, "instamotion_de")
    try:
        replay.config["queries"] = [dict(replay.query)]

        def interrupt(*unused):
            raise KeyboardInterrupt("controlled")

        with monkeypatch.context() as patch:
            patch.setattr(replay.collector, "collect_query", interrupt)
            with pytest.raises(KeyboardInterrupt):
                replay.collector.run(details=False, checks=False)
        assert replay.collector.specialized_details_enabled
        with redirect_stdout(io.StringIO()):
            replay.collector.run(details=True, checks=False)
        assert replay.detail_calls() >= 1
    finally:
        replay.db.conn.close()


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize(
    "cursor", [None, "malformed", "[]", '{"source":"removed-provider"}']
)
def test_whole_source_rotation_survives_three_restarts_with_budget_one(
    tmp_path, monkeypatch, fast, cursor
):
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = NativeReplay(first_dir, monkeypatch, "instamotion_de", fast)
    second = NativeReplay(second_dir, monkeypatch, "quoka_de", fast)
    second.db.conn.close()
    first.config.update(
        detail_limit=1, detail_batch_limit=1, fast_detail_limit=1, detail_cache_hours=0
    )
    first.config["queries"] = [
        dict(first.query),
        dict(first.query, key="same-source-alias"),
        dict(second.query, key="second-native"),
    ]
    if cursor is not None:
        first.db.conn.execute(
            "INSERT OR REPLACE INTO monitor_state VALUES (?,?)",
            ("bmw_specialized_query_cursor", cursor),
        )
        first.db.conn.commit()

    def wire(request, **kwargs):
        return (
            first
            if urlsplit(request.full_url).hostname == "www.instamotion.com"
            else second
        ).wire(request, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", wire)
    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda unused, request, **kwargs: wire(request, **kwargs),
    )
    try:
        counts = []
        for unused in range(3):
            first.restart()
            with redirect_stdout(io.StringIO()):
                summary = first.collector.run(details=True, checks=False, fast=fast)
            assert (
                first.collector.specialized_detail_policy(summary["run_id"]).used == 1
            )
            counts.append((first.detail_calls(), second.detail_calls()))
            first.clock += timedelta(minutes=5)
            second.clock = first.clock
        assert counts == [(1, 0), (1, 1), (2, 1)]
        assert first.db.conn.execute(
            "SELECT value FROM monitor_state WHERE key='bmw_specialized_query_cursor'"
        ).fetchone()
    finally:
        first.db.conn.close()
