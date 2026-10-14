"""Native full/fast regressions for the actual specialized BMW dispatch."""

import hashlib
import io
import json
import socket
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from car_market.collect import Collector
from car_market.db import Database
from car_market.parse import Listing

FIXTURES = Path(__file__).parent / "fixtures"
CASES = {
    "kleinanzeigen_de": ("germany_kleinanzeigen", "search.html", "detail.html"),
    "quoka_de": (
        "germany_discovery",
        "quoka-native-partitions.html",
        "quoka-own-active.html",
    ),
    "instamotion_de": (
        "germany_additional",
        "instamotion.html",
        "instamotion-detail.html",
    ),
}


class NativeReplay:
    def __init__(self, root, monkeypatch, source, fast=False):
        self.path = root / "market.sqlite3"
        self.source, self.fast = source, fast
        self.clock = datetime(2026, 10, 11, 18, tzinfo=UTC)
        self.calls, self.blocked_status = [], None
        self.config = {
            "scope": {"country": "DE"},
            "workers": 1,
            "request_delay": 0,
            "detail_cache_hours": 24,
            "detail_error_backoff_hours": 6,
            "detail_limit": 4,
            "detail_batch_limit": 4,
            "fast_detail_limit": 4,
        }
        folder, search, detail = CASES[source]
        native = FIXTURES / folder
        metadata = {
            r["fixture"]: r for r in json.loads((native / "manifest.json").read_text())
        }
        self.search_url = metadata[search].get(
            "final_url", metadata[search].get("final_uri")
        )
        self.detail_url = metadata[detail].get(
            "final_url", metadata[detail].get("final_uri")
        )
        self.search_body, self.detail_body = (
            (native / search).read_text(),
            (native / detail).read_text(),
        )
        for filename in (search, detail):
            assert (
                hashlib.sha256((native / filename).read_bytes()).hexdigest()
                == metadata[filename]["fixture_sha256"]
            )
        self.query = {
            "source": source,
            "key": "native-policy",
            "url": self.search_url,
            "max_pages": 1,
            "detail_limit": 1,
            "request_limit": 4,
        }
        for name in (
            "db",
            "collect",
            "fetch",
            "germany_kleinanzeigen",
            "germany_discovery",
            "germany_additional",
            "germany_additional_public",
            "bmw_detail_policy",
        ):
            monkeypatch.setattr("car_market." + name + ".now", self.stamp)
        monkeypatch.setattr(
            "car_market.germany_additional_public.time.sleep", lambda unused: None
        )
        monkeypatch.setattr(urllib.request, "urlopen", self.wire)
        monkeypatch.setattr(
            urllib.request.OpenerDirector,
            "open",
            lambda unused, request, **kw: self.wire(request, **kw),
        )
        monkeypatch.setattr(
            socket.socket,
            "connect",
            lambda *a, **kw: pytest.fail("Real socket forbidden"),
        )
        self.restart()

    def stamp(self):
        return self.clock.isoformat()

    def restart(self):
        if hasattr(self, "db"):
            self.db.conn.close()
        self.db = Database(self.path)
        self.collector = Collector(self.db, self.config)
        self.collector.fast = self.fast

    def wire(self, request, **unused):
        url = request.full_url
        detail = urlsplit(url).path != urlsplit(self.search_url).path
        self.calls.append((url, detail))
        if detail and self.blocked_status:
            raise urllib.error.HTTPError(
                url,
                self.blocked_status,
                "controlled",
                Message(),
                io.BytesIO(b"controlled"),
            )
        assert not detail or url == self.detail_url
        body = self.detail_body if detail else self.search_body

        class Wire:
            status = 200
            headers = Message()
            headers["Content-Type"] = "text/html; charset=utf-8"

            def __init__(self):
                self.url = url

            def read(self, size=-1):
                return body.encode()[:size]

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

        return Wire()

    def run(self):
        run = self.db.start_run(timestamp=self.stamp())
        with redirect_stdout(io.StringIO()):
            self.collector.collect_query(run, dict(self.query))
        self.db.finish_run(run, "partial", {})
        key = (
            "kleinanzeigen_discovery:" + self.query["key"]
            if self.source == "kleinanzeigen_de"
            else "germany_discovery:" + self.source + ":" + self.query["key"]
            if self.source == "quoka_de"
            else "germany_additional:" + self.query["key"]
        )
        self.ledger = json.loads(
            self.db.conn.execute(
                "SELECT value FROM monitor_state WHERE key=?", (key,)
            ).fetchone()[0]
        )
        return run

    def accepted(self):
        return self.db.conn.execute(
            "SELECT count(*) FROM observations o JOIN fetches f ON f.id=o.fetch_id WHERE f.url=?",
            (self.detail_url,),
        ).fetchone()[0]

    def detail_calls(self):
        return sum(detail for _, detail in self.calls)


@pytest.mark.parametrize("source", CASES)
@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize(
    "scenario",
    ["success", "403", "429", "zero", "global_revision", "source_revision", "pending"],
)
def test_native_policy_across_restart(tmp_path, monkeypatch, source, fast, scenario):
    replay = NativeReplay(tmp_path, monkeypatch, source, fast)
    try:
        if scenario in {"403", "429"}:
            replay.blocked_status = int(scenario)
        if scenario == "zero":
            replay.config.update(
                detail_limit=0, detail_batch_limit=0, fast_detail_limit=0
            )
        if scenario == "pending":
            with monkeypatch.context() as patcher:
                patcher.setattr(replay.collector, "_observe", lambda *args: None)
                patcher.setattr(
                    "car_market.germany_additional.observe_german_item",
                    lambda *args: None,
                )
                replay.run()
            assert replay.accepted() == 0 and replay.detail_calls() == 1
            replay.config.update(
                detail_limit=0, detail_batch_limit=0, fast_detail_limit=0
            )
        else:
            replay.run()
        if scenario == "success":
            assert replay.accepted() > 0
        old_fetches = [
            tuple(r)
            for r in replay.db.conn.execute("SELECT * FROM fetches ORDER BY id")
        ]
        census = set(replay.ledger["cards"])
        replay.clock += timedelta(minutes=5)
        if scenario == "global_revision":
            replay.config["detail_parser_revision"] = "native-revision-2"
        if scenario == "source_revision":
            replay.config["detail_parser_revisions"] = {source: "native-revision-2"}
        replay.restart()
        replay.run()
        assert set(replay.ledger["cards"]) == census
        assert [
            tuple(r)
            for r in replay.db.conn.execute("SELECT * FROM fetches ORDER BY id")
        ][: len(old_fetches)] == old_fetches
        if scenario in {"global_revision", "source_revision"}:
            assert replay.detail_calls() == 2
            assert replay.accepted() > 0
        elif scenario == "zero":
            assert replay.detail_calls() == 0 and replay.ledger["pending_details"]
        else:
            assert replay.detail_calls() == 1
            if scenario == "success":
                assert replay.accepted() > 0
                captures = replay.db.conn.execute(
                    "SELECT fetched_at FROM fetches WHERE url=?", (replay.detail_url,)
                ).fetchall()
                assert (
                    len(captures) == 1
                    and captures[0][0]
                    == (replay.clock - timedelta(minutes=5)).isoformat()
                )
            else:
                assert replay.ledger["pending_details"]
                assert any(
                    row["status"] == "deferred"
                    for row in replay.ledger["pending_details"].values()
                )
                assert (
                    replay.ledger.get("status", replay.ledger.get("coverage_status"))
                    != "complete"
                )
                if scenario == "pending":
                    assert replay.accepted() == 0
    finally:
        replay.db.conn.close()


def test_shared_allowance_limits_later_generic_details(tmp_path, monkeypatch):
    replay = NativeReplay(tmp_path, monkeypatch, "instamotion_de")
    try:
        replay.config.update(
            detail_limit=1,
            detail_batch_limit=1,
            fast_detail_limit=1,
            detail_cache_hours=0,
        )
        run = replay.run()
        assert replay.detail_calls() == 1
        row = replay.db.conn.execute(
            "SELECT l.id,o.details_json FROM listings l JOIN latest_observations o ON o.listing_id=l.id WHERE l.url=?",
            (replay.detail_url,),
        ).fetchone()
        item = Listing(**json.loads(row["details_json"]))
        replay.collector.detail_jobs = {
            replay.detail_url: (item.source, row["id"], item)
        }
        with redirect_stdout(io.StringIO()):
            replay.collector.details(run)
        assert replay.detail_calls() == 1
        assert replay.collector.specialized_detail_policy(run).used == 1
    finally:
        replay.db.conn.close()


@pytest.mark.parametrize("source", CASES)
def test_missing_raw_cache_requires_bounded_fresh_capture(
    tmp_path, monkeypatch, source
):
    replay = NativeReplay(tmp_path, monkeypatch, source)
    try:
        replay.run()
        replay.db.conn.execute(
            "UPDATE fetches SET raw_path=NULL WHERE url=?", (replay.detail_url,)
        )
        replay.db.conn.commit()
        replay.clock += timedelta(minutes=5)
        replay.restart()
        replay.run()
        assert replay.detail_calls() == 2 and replay.accepted() > 0
    finally:
        replay.db.conn.close()


def test_changed_search_price_survives_budget_deferral(tmp_path, monkeypatch):
    replay = NativeReplay(tmp_path, monkeypatch, "instamotion_de")
    try:
        replay.run()
        assert "27499" in replay.search_body
        replay.search_body = replay.search_body.replace("27499", "25999")
        replay.config.update(detail_limit=0, detail_batch_limit=0, fast_detail_limit=0)
        replay.clock += timedelta(minutes=5)
        replay.restart()
        replay.run()
        row = replay.db.conn.execute(
            "SELECT o.price_eur,o.observed_at FROM listings l JOIN latest_observations o ON o.listing_id=l.id WHERE l.url=?",
            (replay.detail_url,),
        ).fetchone()
        assert (row[0], row[1]) == (25999, replay.stamp())
        assert replay.detail_calls() == 1 and replay.ledger["pending_details"]
        assert any(
            r["reason"] == "detail_budget_deferred"
            for r in replay.ledger["pending_details"].values()
            if r["status"] == "deferred"
        )
    finally:
        replay.db.conn.close()


@pytest.mark.parametrize(
    "blocked_final",
    [
        "https://different.example/own",
        "https://www.instamotion.com:443/de/auto/blocked",
    ],
)
def test_backoff_matches_actual_final_host_and_offset_clock(
    tmp_path, monkeypatch, blocked_final
):
    replay = NativeReplay(tmp_path, monkeypatch, "instamotion_de")
    try:
        run = replay.db.start_run(timestamp=replay.stamp())
        stamp = (
            (replay.clock - timedelta(minutes=5))
            .astimezone(timezone(timedelta(hours=2)))
            .isoformat()
        )
        replay.db.save_fetch(
            run,
            replay.source,
            "https://www.instamotion.com/de/auto/different",
            blocked_final,
            429,
            "controlled",
            stamp,
            "http",
            "http_429",
        )
        replay.run()
        assert replay.detail_calls() == (0 if "instamotion.com" in blocked_final else 1)
        if "instamotion.com" in blocked_final:
            assert replay.ledger["pending_details"]
    finally:
        replay.db.conn.close()


def test_successful_history_does_not_enter_blocked_lookup(tmp_path, monkeypatch):
    replay = NativeReplay(tmp_path, monkeypatch, "instamotion_de")
    try:
        run = replay.db.start_run(timestamp=replay.stamp())
        replay.db.conn.executemany(
            "INSERT INTO fetches(run_id,source,url,final_url,fetched_at,status,transport) VALUES (?,?,?,?,?,200,'http')",
            [
                (
                    run,
                    replay.source,
                    "https://www.instamotion.com/de/auto/history/" + str(i),
                    "https://www.instamotion.com/de/auto/history/" + str(i),
                    replay.stamp(),
                )
                for i in range(10000)
            ],
        )
        replay.db.conn.commit()
        statements = []
        replay.db.conn.set_trace_callback(statements.append)
        replay.run()
        blocked = next(
            s for s in statements if "FROM fetches" in s and "status IN (403,429)" in s
        )
        plans = replay.db.conn.execute("EXPLAIN QUERY PLAN " + blocked).fetchall()
        assert any("fetch_blocked_actual_time" in row[3] for row in plans)
        assert not any(row[3].startswith("SCAN fetches") for row in plans)
        assert replay.detail_calls() == 1
    finally:
        replay.db.conn.close()


@pytest.mark.parametrize("source", CASES)
def test_new_revision_needs_accepted_observation_after_its_cutoff(
    tmp_path, monkeypatch, source
):
    replay = NativeReplay(tmp_path, monkeypatch, source)
    try:
        replay.run()
        replay.clock += timedelta(minutes=5)
        replay.config["detail_parser_revision"] = "current-parser"
        replay.restart()
        with monkeypatch.context() as patcher:
            patcher.setattr(replay.collector, "_observe", lambda *args: None)
            patcher.setattr(
                "car_market.germany_additional.observe_german_item", lambda *args: None
            )
            replay.run()
        assert replay.detail_calls() == 2
        replay.config.update(detail_limit=0, detail_batch_limit=0, fast_detail_limit=0)
        replay.clock += timedelta(minutes=5)
        replay.restart()
        replay.run()
        assert replay.detail_calls() == 2 and replay.ledger["pending_details"]
        assert not any(
            r.get("cached") for r in replay.ledger["pending_details"].values()
        )
    finally:
        replay.db.conn.close()


def test_nested_country_proof_block_is_retained_and_guarded_after_restart(
    tmp_path, monkeypatch
):
    from car_market.bmw_detail_policy import DetailDeferred
    from car_market.germany_additional_public import AdditionalPublicGetter

    replay = NativeReplay(tmp_path, monkeypatch, "instamotion_de")
    native = FIXTURES / "germany_additional"
    metadata = {
        r["fixture"]: r for r in json.loads((native / "manifest.json").read_text())
    }
    own_url = metadata["carwow-own-detail.html"]["final_uri"]
    claim = metadata["markt-own-herne.html"]["final_uri"]
    own_id = own_url.rsplit("/", 1)[1]
    calls = []

    def wire(request, **unused):
        url = request.full_url
        calls.append(url)
        if url == claim:
            raise urllib.error.HTTPError(
                url, 403, "controlled", Message(), io.BytesIO(b"blocked")
            )
        assert url == own_url

        class Wire:
            status = 200
            headers = Message()

            def __init__(self):
                self.url = url

            def read(self, unused):
                return (native / "carwow-own-detail.html").read_bytes()

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

        return Wire()

    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda unused, request, **kw: wire(request, **kw),
    )
    try:
        replay.collector.fetcher = AdditionalPublicGetter(
            max_requests=8, request_delay=0, country_publications={own_id: claim}
        )
        run = replay.db.start_run(timestamp=replay.stamp())
        first = replay.collector.specialized_detail_policy(run).fetch(
            "carwow_de", own_url, identity=own_id
        )
        assert first.requests == 2 and first.response.status == 200
        row = replay.db.conn.execute(
            "SELECT source,status FROM fetches WHERE url=?", (claim,)
        ).fetchone()
        assert (row[0], row[1]) == ("markt_de", 403)
        replay.clock += timedelta(minutes=5)
        replay.restart()
        replay.collector.fetcher = AdditionalPublicGetter(
            max_requests=8, request_delay=0, country_publications={own_id: claim}
        )
        run = replay.db.start_run(timestamp=replay.stamp())
        with pytest.raises(DetailDeferred, match="detail_host_backoff:403"):
            replay.collector.specialized_detail_policy(run).fetch(
                "carwow_de", own_url, identity=own_id
            )
        assert calls == [own_url, claim, own_url]
    finally:
        replay.db.conn.close()


def test_protocol_redirect_is_not_an_error_backoff(tmp_path, monkeypatch):
    replay = NativeReplay(tmp_path, monkeypatch, "instamotion_de")
    try:
        run = replay.db.start_run(timestamp=replay.stamp())
        replay.db.save_fetch(
            run,
            replay.source,
            replay.detail_url,
            replay.detail_url,
            303,
            "redirect",
            replay.stamp(),
            "http",
            None,
        )
        replay.run()
        assert replay.detail_calls() == 1 and replay.accepted() > 0
    finally:
        replay.db.conn.close()


def test_redirect_captures_keep_wire_order_for_equal_timestamps(tmp_path, monkeypatch):
    from car_market.germany_additional_public import AdditionalPublicGetter

    replay = NativeReplay(tmp_path, monkeypatch, "instamotion_de")
    initial = replay.detail_url + "-old-slug"

    def wire(request, **unused):
        url = request.full_url

        class Wire:
            def __init__(self):
                self.url = url
                self.status = 302 if url == initial else 200
                self.headers = Message()
                if self.status == 302:
                    self.headers["Location"] = replay.detail_url

            def read(self, unused):
                return (
                    b"redirect" if self.status == 302 else replay.detail_body.encode()
                )

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

        return Wire()

    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda unused, request, **kw: wire(request, **kw),
    )
    try:
        replay.collector.fetcher = AdditionalPublicGetter(
            max_requests=8, request_delay=0
        )
        run = replay.db.start_run(timestamp=replay.stamp())
        result = replay.collector.specialized_detail_policy(run).fetch(
            replay.source, initial
        )
        assert result.requests == 2 and result.response.status == 200
        assert [
            row[0]
            for row in replay.db.conn.execute("SELECT status FROM fetches ORDER BY id")
        ] == [302, 200]
    finally:
        replay.db.conn.close()
