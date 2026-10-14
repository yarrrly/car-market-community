"""Owned native Wiveda composition, clocks and actual shared request allowance."""

import copy
import gzip
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from car_market.db import Database
from car_market.fetch import Response
from car_market.mercedes_collect import MercedesCollector
from car_market.model_profiles import MERCEDES_PROFILE

FIXTURES = Path(__file__).parent / "fixtures/mercedes_wiveda_composition"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
IDS = list(dict.fromkeys(row["identity"] for row in MANIFEST))
SOURCE = "dealer:wiveda.de"


def native(identity, kind):
    meta = next(
        row for row in MANIFEST if row["identity"] == identity and row["kind"] == kind
    )
    archive = (FIXTURES / meta["file"]).read_bytes()
    assert hashlib.sha256(archive).hexdigest() == meta["gzip_SHA256"]
    body = gzip.decompress(archive)
    assert hashlib.sha256(body).hexdigest() == meta["body_SHA256"]
    return body.decode(), meta


def original_route_clock(monkeypatch, identity):
    """Keep source clocks unchanged while inspecting a route inside its actual TTL."""
    _, html = native(identity, "html")
    _, payload = native(identity, "payload")
    clock = max(
        datetime.fromisoformat(row["original_clock"]) for row in (html, payload)
    ) + timedelta(seconds=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock if tz else clock.replace(tzinfo=None)

    monkeypatch.setattr("car_market.mercedes_collect.datetime", Clock)
    monkeypatch.setattr("car_market.mercedes_wiveda_details.datetime", Clock)
    monkeypatch.setattr("car_market.bmw_detail_policy.now", lambda: clock.isoformat())


class Replay:
    def __init__(self, path, *, limit=2, workers=1, fast=False):
        self.path = path / "data/market.sqlite3"
        self.config = {
            "scope": {"country": "DE", "model_profile": MERCEDES_PROFILE},
            "queries": [],
            "workers": workers,
            "detail_limit": limit,
            "detail_cache_hours": 6,
            "detail_error_backoff_hours": 6,
        }
        self.calls = []
        self.root_stamp = self.payload_stamp = datetime.now(UTC).isoformat()
        self.overrides = {}
        self.reopen(fast=fast)

    def reopen(self, *, limit=None, fast=False):
        if hasattr(self, "worker"):
            self.worker.db.conn.close()
        if limit is not None:
            self.config["detail_limit"] = limit
        self.worker = MercedesCollector(Database(self.path), self.config)
        self.worker.fast = fast
        self.worker.fetcher.get = self.get

    def get(self, url, **kwargs):
        assert threading.get_ident() == threading.main_thread().ident
        assert kwargs.get("refresh") is True
        self.calls.append(url)
        if url in self.overrides:
            return self.overrides[url](url)
        meta = next(row for row in MANIFEST if row["url"] == url)
        body, _ = native(meta["identity"], meta["kind"])
        return Response(
            url,
            url,
            200,
            body,
            self.payload_stamp if meta["kind"] == "payload" else self.root_stamp,
        )

    def item(self, identity=IDS[0]):
        body, meta = native(identity, "payload")
        return self.worker._parse(SOURCE, body, meta["url"]).listings[0]

    def collect(self, identities=None, originals=None):
        self.worker.detail_jobs.clear()
        self.worker.detail_stats.clear()
        self.worker.failures.clear()
        for item in originals or [
            self.item(identity) for identity in identities or [IDS[0]]
        ]:
            self.worker.detail_jobs[item.url] = (SOURCE, None, item)
        run = self.worker.db.start_run("fast" if self.worker.fast else "live")
        self.worker.details(run)
        return run

    def summary(self):
        return self.worker.db.conn.execute(
            "SELECT * FROM source_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()


@pytest.mark.parametrize("identity", IDS)
def test_original_native_pairs_compose_without_inferred_generation(tmp_path, identity):
    replay = Replay(tmp_path)
    replay.collect([identity])
    assert len(replay.calls) == 2
    (row,) = replay.worker.db.conn.execute(
        "SELECT * FROM latest_observations"
    ).fetchall()
    assert row["observed_at"] == replay.payload_stamp
    payload = json.loads(row["details_json"])
    assert payload["external_id"] == identity
    assert payload["raw"]["market_country"] == "DE"
    assert payload["raw"].get("generation") is None
    assert row["scope"] == "needs_confirmation"
    assert replay.summary()["received_count"] == 1
    assert replay.summary()["pages"] == 2
    assert replay.worker.db.conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("identity", IDS)
def test_old_html_unparsed_is_only_route_evidence_and_history_is_immutable(
    tmp_path, monkeypatch, identity
):
    original_route_clock(monkeypatch, identity)
    replay = Replay(tmp_path, limit=1)
    body, html = native(identity, "html")
    _, payload = native(identity, "payload")
    replay.payload_stamp = payload["original_clock"]
    run = replay.worker.db.start_run("historical")
    fetch = replay.worker.db.save_fetch(
        run,
        SOURCE,
        html["url"],
        html["final_url"],
        200,
        body,
        html["original_clock"],
        error="detail_unparsed",
    )
    before = tuple(
        replay.worker.db.conn.execute(
            "SELECT * FROM fetches WHERE id=?", (fetch,)
        ).fetchone()
    )
    replay.collect([identity])
    assert replay.calls == [payload["url"]]
    assert (
        tuple(
            replay.worker.db.conn.execute(
                "SELECT * FROM fetches WHERE id=?", (fetch,)
            ).fetchone()
        )
        == before
    )
    assert (
        replay.worker.db.conn.execute("SELECT last_seen FROM listings").fetchone()[0]
        == payload["original_clock"]
    )
    assert all(
        row[0] == payload["original_clock"]
        for row in replay.worker.db.conn.execute(
            "SELECT verified_at FROM field_verifications"
        )
    )


@pytest.mark.parametrize("workers", [1, 2, 4])
@pytest.mark.parametrize("limit", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("fast", [False, True])
def test_actual_two_operation_allowance_and_pending_census(
    tmp_path, workers, limit, fast
):
    replay = Replay(tmp_path, limit=limit, workers=workers, fast=fast)
    value = json.dumps({"received": 53, "expected": 53, "IDs": IDS})
    replay.worker.db.conn.execute(
        "INSERT INTO monitor_state VALUES ('model_discovery:wiveda:native',?)", (value,)
    )
    replay.worker.db.conn.commit()
    replay.collect(IDS)
    assert len(replay.calls) == limit
    assert (
        replay.worker.db.conn.execute(
            "SELECT value FROM monitor_state WHERE key='model_discovery:wiveda:native'"
        ).fetchone()[0]
        == value
    )
    if limit < 6:
        assert replay.summary()["status"] == "partial"
    if limit == 1:
        (row,) = replay.worker.db.conn.execute("SELECT * FROM fetches").fetchall()
        assert row["status"] == 200 and row["error"] is None
        assert (
            replay.worker.db.conn.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
            == 0
        )


def test_accepted_cache_across_reopen_cap0_does_not_renew_any_original_clock(tmp_path):
    replay = Replay(tmp_path)
    replay.collect()
    tables = ["fetches", "observations", "events", "field_verifications", "listings"]
    before = {
        table: [
            tuple(row)
            for row in replay.worker.db.conn.execute("SELECT * FROM " + table)
        ]
        for table in tables
    }
    replay.reopen(limit=0)
    replay.collect()
    assert len(replay.calls) == 2
    assert replay.summary()["status"] == "complete"
    for table in tables:
        assert [
            tuple(row)
            for row in replay.worker.db.conn.execute("SELECT * FROM " + table)
        ] == before[table]
    assert replay.worker.detail_stats[SOURCE]["cached"] == 1


def test_unaccepted_same_sha_requires_a_new_real_payload_under_budget(tmp_path):
    replay = Replay(tmp_path)
    replay.collect()
    body, meta = native(IDS[0], "payload")
    run = replay.worker.db.start_run("unaccepted_same_sha")
    fetch = replay.worker.db.save_fetch(
        run, SOURCE, meta["url"], meta["url"], 200, body, datetime.now(UTC).isoformat()
    )
    old = [
        tuple(row)
        for row in replay.worker.db.conn.execute("SELECT * FROM observations")
    ]
    replay.reopen(limit=0)
    replay.collect()
    assert len(replay.calls) == 2
    assert replay.summary()["status"] == "partial"
    assert [
        tuple(row)
        for row in replay.worker.db.conn.execute("SELECT * FROM observations")
    ] == old
    assert (
        replay.worker.db.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE fetch_id=?", (fetch,)
        ).fetchone()[0]
        == 0
    )
    replay.reopen(limit=1)
    replay.payload_stamp = datetime.now(UTC).isoformat()
    replay.collect()
    assert replay.calls[-1] == meta["url"] and len(replay.calls) == 3
    assert (
        replay.worker.db.conn.execute(
            "SELECT fetched_at FROM fetches ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        == replay.payload_stamp
    )


@pytest.mark.parametrize("stage", ["html", "payload"])
@pytest.mark.parametrize("status", [403, 429])
def test_genuine_refusals_suppress_native_route_across_restarts(
    tmp_path, stage, status
):
    replay = Replay(tmp_path)
    _, meta = native(IDS[0], stage)
    replay.overrides[meta["url"]] = lambda url: Response(
        url,
        "https://wiveda.de",
        status,
        "refused",
        datetime.now(UTC).isoformat(),
        error="http_" + str(status),
    )
    replay.collect()
    calls = list(replay.calls)
    before = [
        tuple(row) for row in replay.worker.db.conn.execute("SELECT * FROM fetches")
    ]
    replay.reopen(limit=10)
    replay.collect()
    assert replay.calls == calls
    assert [
        tuple(row) for row in replay.worker.db.conn.execute("SELECT * FROM fetches")
    ] == before
    assert replay.worker.detail_stats[SOURCE]["backoff"] == 1


@pytest.mark.parametrize("status", [403, 429])
def test_actual_origin_backoff_crosses_logical_sources(tmp_path, status):
    replay = Replay(tmp_path)
    replay.worker.db.save_fetch(
        replay.worker.db.start_run("refused_elsewhere"),
        "quoka_de",
        "https://www.quoka.de/own",
        "https://WIVEDA.DE:443?gate=1#held",
        status,
        "refused",
        datetime.now(UTC).isoformat(),
        error="http_" + str(status),
    )
    replay.collect()
    assert replay.calls == []
    assert replay.worker.detail_stats[SOURCE]["backoff"] == 1


@pytest.mark.parametrize(
    "replacement",
    [
        "https://foreign.invalid/_payload.json?_b=old",
        "https://wiveda.de/fahrzeug/00000000-0000-0000-0000-000000000000/_payload.json?_b=old",
        "https://wiveda.de/fahrzeug/" + IDS[0] + "/_payload.json",
    ],
)
def test_foreign_wrong_id_and_missing_build_do_not_issue_payload_http(
    tmp_path, replacement
):
    replay = Replay(tmp_path)
    body, meta = native(IDS[0], "html")
    doc = body.replace(
        native(IDS[0], "payload")[1]["url"].removeprefix("https://wiveda.de"),
        replacement,
    )
    replay.overrides[meta["url"]] = lambda url: Response(
        url, url, 200, doc, replay.root_stamp
    )
    replay.collect()
    assert len(replay.calls) == 1
    assert (
        replay.worker.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("change", ["foreign_country", "other_id", "malformed"])
def test_native_payload_binding_rejects_wrong_country_identity_and_shape(
    tmp_path, change
):
    replay = Replay(tmp_path)
    body, meta = native(IDS[0], "payload")
    doc = json.loads(body)
    row = next(iter(doc.values()))
    if change == "foreign_country":
        row["country"] = "NL"
    elif change == "other_id":
        row["id"] = IDS[1]
    else:
        doc = 42
    replay.overrides[meta["url"]] = lambda url: Response(
        url, url, 200, json.dumps(doc), replay.payload_stamp
    )
    replay.collect()
    assert len(replay.calls) == 2
    assert replay.summary()["status"] == "partial"
    assert (
        replay.worker.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        == 0
    )


def test_current_bound_price_contradiction_cannot_be_satisfied_by_old_cache_cap0(
    tmp_path,
):
    replay = Replay(tmp_path)
    replay.collect()
    fresh = replay.item()
    fresh.price += 100
    body, _meta = native(IDS[0], "payload")
    doc = json.loads(body)
    next(iter(doc.values()))["price"] = fresh.price
    run = replay.worker.db.start_run("new_owned_search")
    stamp = datetime.now(UTC).isoformat()
    fetch = replay.worker.db.save_fetch(
        run,
        SOURCE,
        "https://wiveda.de/gebrauchtwagen/mercedes-benz/c-180",
        "https://wiveda.de/gebrauchtwagen/mercedes-benz/c-180",
        200,
        json.dumps(doc),
        stamp,
    )
    replay.worker._observe(run, copy.deepcopy(fresh), stamp, fetch)
    replay.reopen(limit=0)
    replay.collect(originals=[fresh])
    assert len(replay.calls) == 2
    assert replay.summary()["status"] == "partial"
    assert (
        replay.worker.db.conn.execute(
            "SELECT price_eur FROM latest_observations"
        ).fetchone()[0]
        == fresh.price
    )


@pytest.mark.parametrize("kind", ["global", "source"])
def test_revision_requests_fresh_composition_and_cap1_retains_old_proof(tmp_path, kind):
    replay = Replay(tmp_path)
    replay.collect()
    old = [
        tuple(row)
        for row in replay.worker.db.conn.execute("SELECT * FROM observations")
    ]
    cutoff = datetime.now(UTC).isoformat()
    replay.reopen(limit=1)
    if kind == "global":
        replay.worker.parser_refresh_since = cutoff
    else:
        replay.worker.source_parser_refresh_since[SOURCE] = cutoff
    replay.collect()
    assert len(replay.calls) == 3
    assert replay.summary()["status"] == "partial"
    assert [
        tuple(row)
        for row in replay.worker.db.conn.execute("SELECT * FROM observations")
    ] == old


def test_no_details_run_keeps_native_pending_and_performs_no_http(tmp_path):
    replay = Replay(tmp_path)
    run = replay.worker.db.start_run("owned_search")
    body, meta = native(IDS[0], "payload")
    fetch = replay.worker.db.save_fetch(
        run, SOURCE, meta["url"], meta["url"], 200, body, meta["original_clock"]
    )
    replay.worker._observe(run, replay.item(), meta["original_clock"], fetch)
    before = [
        tuple(row)
        for row in replay.worker.db.conn.execute("SELECT * FROM observations")
    ]
    replay.worker.run(details=False, checks=False, fast=True)
    assert replay.calls == []
    assert [
        tuple(row)
        for row in replay.worker.db.conn.execute("SELECT * FROM observations")
    ] == before


@pytest.mark.parametrize(
    "field,value", [("fuel", "diesel"), ("year", 2015), ("transmission", "manual")]
)
def test_fresh_conclusive_search_exclusion_does_not_reuse_compatible_old_vdp(
    tmp_path, field, value
):
    replay = Replay(tmp_path)
    replay.collect()
    current = replay.item()
    setattr(current, field, value)
    replay.reopen(limit=0)
    replay.collect(originals=[current])
    assert len(replay.calls) == 2
    assert replay.summary()["status"] == "partial"
    assert replay.worker.detail_stats[SOURCE].get("cached", 0) == 0


@pytest.mark.parametrize("workers", [1, 2, 4])
@pytest.mark.parametrize("limit", [1, 2, 3])
def test_generic_groups_and_native_barrier_share_allowance_and_fair_order(
    tmp_path, workers, limit
):
    replay = Replay(tmp_path, workers=workers, limit=limit)
    folder = Path(__file__).parent / "fixtures/mercedes_autoscout"
    case = next(
        row
        for row in json.loads((folder / "manifest.json").read_text())
        if row["country"] == "DE"
    )
    body = (folder / case["file"]).read_text()
    record = json.loads(body)["props"]["pageProps"]["listingDetails"]
    uri = record["webPage"]
    item = replay.worker._parse("autoscout24_de", body, uri).listings[0]
    native_item = replay.item()
    replay.worker.detail_jobs = {
        uri: (item.source, None, item),
        native_item.url: (SOURCE, None, native_item),
    }

    def generic(url):
        replay.calls.append(url)
        return Response(url, url, 200, body, replay.root_stamp)

    replay.worker._detail_get = generic
    replay.worker.details(replay.worker.db.start_run("mixed_actual_routes"))
    assert len(replay.calls) == limit
    assert replay.calls[0] == uri
    assert replay.worker.db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    if limit == 2:
        root = replay.worker.db.conn.execute(
            "SELECT * FROM fetches WHERE source=?", (SOURCE,)
        ).fetchone()
        assert root["error"] is None
        assert (
            replay.worker.db.conn.execute(
                "SELECT COUNT(*) FROM observations o JOIN listings l ON l.id=o.listing_id WHERE l.source=?",
                (SOURCE,),
            ).fetchone()[0]
            == 0
        )


def test_late_generic_refusal_cannot_drop_next_native_slot(tmp_path, monkeypatch):
    from car_market.bmw_detail_policy import BMWDetailPolicy

    replay = Replay(tmp_path, limit=2, workers=4)
    item = replay.item()
    uri = "https://www.autoscout24.de/angebote/00000000-0000-0000-0000-000000000000"
    generic = copy.deepcopy(item)
    generic.source, generic.url = "autoscout24_de", uri
    replay.worker.detail_jobs = {
        uri: (generic.source, None, generic),
        item.url: (SOURCE, None, item),
    }
    original = BMWDetailPolicy._host_backoff
    calls = 0

    def refuse_late(policy, source, url, clock):
        nonlocal calls
        if url == uri:
            calls += 1
            if calls == 2:
                replay.worker.db.save_fetch(
                    policy.run,
                    source,
                    uri,
                    uri,
                    403,
                    "refused",
                    datetime.now(UTC).isoformat(),
                    error="http_403",
                )
        return original(policy, source, url, clock)

    monkeypatch.setattr(BMWDetailPolicy, "_host_backoff", refuse_late)
    replay.worker.details(replay.worker.db.start_run("late_refusal"))
    assert len(replay.calls) == 2
    assert replay.worker.detail_stats[SOURCE]["parsed"] == 1
    assert replay.worker.detail_stats[generic.source]["backoff"] == 1


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize(
    "marker", ["<title>Login</title>", "<script>cf-chl-managed-challenge</script>"]
)
def test_access_walls_never_open_advertised_native_payload(tmp_path, cached, marker):
    replay = Replay(tmp_path)
    body, meta = native(IDS[0], "html")
    held = marker + body
    if cached:
        replay.worker.db.save_fetch(
            replay.worker.db.start_run("old_wall"),
            SOURCE,
            meta["url"],
            meta["url"],
            200,
            held,
            datetime.now(UTC).isoformat(),
            error="detail_unparsed",
        )
    replay.overrides[meta["url"]] = lambda url: Response(
        url, url, 200, held, replay.root_stamp
    )
    replay.collect()
    assert all("_payload.json" not in url for url in replay.calls)
    assert replay.summary()["status"] == "partial"
    assert (
        replay.worker.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("stage", ["html", "payload"])
@pytest.mark.parametrize("clock", ["future", "naive"])
def test_invalid_incoming_capture_clock_never_renews_fields(tmp_path, stage, clock):
    replay = Replay(tmp_path)
    stamp = (
        (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        if clock == "future"
        else datetime.now(UTC).replace(tzinfo=None).isoformat()
    )
    if stage == "html":
        replay.root_stamp = stamp
    else:
        replay.payload_stamp = stamp
    replay.collect()
    assert (
        replay.worker.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        == 0
    )
    assert (
        replay.worker.db.conn.execute(
            "SELECT COUNT(*) FROM field_verifications"
        ).fetchone()[0]
        == 0
    )
    assert replay.summary()["status"] == "partial"


def test_cache_zero_requires_both_real_operations_again(tmp_path):
    replay = Replay(tmp_path)
    replay.collect()
    replay.config["detail_cache_hours"] = 0
    replay.reopen(limit=1)
    replay.collect()
    assert len(replay.calls) == 3
    assert replay.summary()["status"] == "partial"


def test_exact_cache_expiry_does_not_credit_old_payload(tmp_path, monkeypatch):
    from car_market import bmw_detail_policy, mercedes_wiveda_details

    replay = Replay(tmp_path)
    old = datetime.now(UTC) - timedelta(hours=6)
    replay.root_stamp = replay.payload_stamp = old.isoformat()
    replay.collect()
    old_rows = [
        tuple(row)
        for row in replay.worker.db.conn.execute("SELECT * FROM observations")
    ]
    exact = old + timedelta(hours=6)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return exact

    monkeypatch.setattr(mercedes_wiveda_details, "datetime", Clock)
    monkeypatch.setattr(bmw_detail_policy, "now", lambda: exact.isoformat())
    replay.reopen(limit=0)
    replay.collect()
    assert len(replay.calls) == 2
    assert replay.summary()["status"] == "partial"
    assert [
        tuple(row)
        for row in replay.worker.db.conn.execute("SELECT * FROM observations")
    ] == old_rows


@pytest.mark.parametrize("identity", IDS)
def test_native_full_getter_reuses_generic_accepted_payload_and_header_route(
    tmp_path, identity
):
    replay = Replay(tmp_path)
    replay.collect([identity])
    old_rows = [
        tuple(row)
        for row in replay.worker.db.conn.execute("SELECT * FROM observations")
    ]
    replay.reopen(limit=0)
    worker = replay.worker
    worker._secondary_detail_attempts = 0
    worker._secondary_detail_deferrals = []
    run = worker.db.start_run("native_full_getter")
    item = replay.item(identity)
    header = worker._secondary_detail_get(run, SOURCE, item.url, original=item)
    assert header.secondary_route_evidence is True
    body, meta = native(identity, "payload")
    payload = worker._secondary_detail_get(run, SOURCE, meta["url"], original=item)
    assert payload.body == body
    assert (
        payload.secondary_cached_accepted["fetch_id"]
        == payload.secondary_cached_accepted["accepted_fetch_id"]
    )
    assert (
        payload.timestamp == payload.secondary_cached_accepted["accepted_observed_at"]
    )
    assert worker._secondary_detail_attempts == 0
    assert len(replay.calls) == 2
    assert [
        tuple(row) for row in worker.db.conn.execute("SELECT * FROM observations")
    ] == old_rows


@pytest.mark.parametrize("identity", IDS)
def test_native_full_getter_recovers_only_owned_unparsed_html_without_renewal(
    tmp_path, monkeypatch, identity
):
    original_route_clock(monkeypatch, identity)
    replay = Replay(tmp_path, limit=1)
    replay.payload_stamp = native(identity, "payload")[1]["original_clock"]
    worker = replay.worker
    worker._secondary_detail_attempts = 0
    worker._secondary_detail_deferrals = []
    body, meta = native(identity, "html")
    run = worker.db.start_run("old_generic_failure")
    fetch = worker.db.save_fetch(
        run,
        SOURCE,
        meta["url"],
        meta["url"],
        200,
        body,
        meta["original_clock"],
        error="detail_unparsed",
    )
    old = tuple(
        worker.db.conn.execute("SELECT * FROM fetches WHERE id=?", (fetch,)).fetchone()
    )
    item = replay.item(identity)
    header = worker._secondary_detail_get(run, SOURCE, item.url, original=item)
    assert (
        header.secondary_route_evidence and header.timestamp == meta["original_clock"]
    )
    payload_url = native(identity, "payload")[1]["url"]
    worker._secondary_detail_get(run, SOURCE, payload_url, original=item)
    assert replay.calls == [payload_url]
    assert worker._secondary_detail_attempts == 1
    assert (
        tuple(
            worker.db.conn.execute(
                "SELECT * FROM fetches WHERE id=?", (fetch,)
            ).fetchone()
        )
        == old
    )


@pytest.mark.parametrize("status", [403, 429])
def test_native_full_getter_preserves_cross_source_actual_origin_refusal(
    tmp_path, status
):
    replay = Replay(tmp_path)
    worker = replay.worker
    worker._secondary_detail_attempts = 0
    worker._secondary_detail_deferrals = []
    run = worker.db.start_run("actual_refusal")
    fetch = worker.db.save_fetch(
        run,
        "quoka_de",
        "https://www.quoka.de/stock",
        "https://wiveda.de",
        status,
        "refused",
        datetime.now(UTC).isoformat(),
        error="http_" + str(status),
    )
    with pytest.raises(ValueError, match="detail_host_backoff"):
        worker._secondary_detail_get(
            run, SOURCE, replay.item().url, original=replay.item()
        )
    assert worker._secondary_detail_deferrals[0]["fetch_id"] == fetch
    assert replay.calls == []


@pytest.mark.parametrize("field,value", [("source", "pkw_de"), ("external_id", IDS[1])])
def test_cached_accepted_payload_requires_current_sql_publication_binding(
    tmp_path, field, value
):
    replay = Replay(tmp_path)
    replay.collect()
    row = replay.worker.db.conn.execute("SELECT * FROM observations").fetchone()
    payload = json.loads(row["details_json"])
    payload[field] = value
    replay.worker.db.conn.execute(
        "UPDATE observations SET details_json=? WHERE id=?",
        (json.dumps(payload), row["id"]),
    )
    replay.worker.db.conn.commit()
    replay.reopen(limit=0)
    replay.collect()
    assert len(replay.calls) == 2
    assert replay.summary()["status"] == "partial"
    assert replay.worker.detail_stats[SOURCE]["held"] == 1
    assert "0 budget deferred" in replay.summary()["error"]


@pytest.mark.parametrize("change", ["foreign_country", "other_id", "malformed"])
def test_native_full_payload_guard_preserves_honest_pending_and_failed_raw(
    tmp_path, change
):
    replay = Replay(tmp_path, limit=1)
    worker = replay.worker
    worker._secondary_detail_attempts = 0
    worker._secondary_detail_deferrals = []
    body, meta = native(IDS[0], "payload")
    doc = json.loads(body)
    if change == "malformed":
        doc = 42
    else:
        next(iter(doc.values()))["country" if change == "foreign_country" else "id"] = (
            "NL" if change == "foreign_country" else IDS[1]
        )
    replay.overrides[meta["url"]] = lambda url: Response(
        url, url, 200, json.dumps(doc), replay.payload_stamp
    )
    with pytest.raises(ValueError, match="identity_mismatch"):
        worker._secondary_detail_get(
            worker.db.start_run("native_payload_guard"),
            SOURCE,
            meta["url"],
            original=replay.item(),
        )
    assert len(replay.calls) == 1
    assert (
        worker.db.conn.execute("SELECT error FROM fetches").fetchone()[0]
        == "wiveda_detail_identity_mismatch"
    )
    assert (
        worker.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    )


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("mismatch", ["queue_source", "source", "external_id", "url"])
def test_queued_publication_binding_is_required_before_http_or_cache(
    tmp_path, cached, mismatch
):
    replay = Replay(tmp_path)
    if cached:
        replay.collect()
        replay.reopen(limit=0)
    original = replay.item()
    queued_url = original.url
    source = SOURCE
    if mismatch == "queue_source":
        source = "autoscout24_de"
    elif mismatch == "source":
        original.source = "autoscout24_de"
    elif mismatch == "external_id":
        original.external_id = IDS[1]
    else:
        original.url = replay.item(IDS[1]).url
    tables = ["fetches", "observations", "events", "field_verifications", "listings"]
    before = {
        table: [
            tuple(row)
            for row in replay.worker.db.conn.execute("SELECT * FROM " + table)
        ]
        for table in tables
    }
    calls = list(replay.calls)
    replay.worker.detail_jobs = {queued_url: (source, None, original)}
    replay.worker.details(replay.worker.db.start_run("queue_binding"))
    assert replay.calls == calls
    assert replay.summary()["status"] == "partial"
    assert replay.summary()["received_count"] == 0
    for table in tables:
        assert [
            tuple(row)
            for row in replay.worker.db.conn.execute("SELECT * FROM " + table)
        ] == before[table]


@pytest.mark.parametrize("workers", [1, 2, 4])
@pytest.mark.parametrize("prior_paid", [0, 1])
def test_zero_cost_native_cache_is_resolved_after_generic_allowance_exhaustion(
    tmp_path, workers, prior_paid
):
    replay = Replay(tmp_path, workers=workers)
    replay.collect()
    replay.reopen(limit=prior_paid)
    replay.worker._secondary_detail_attempts = prior_paid
    item = replay.item()
    generic = copy.deepcopy(item)
    generic.source = "autoscout24_de"
    generic.url = (
        "https://www.autoscout24.de/angebote/00000000-0000-0000-0000-000000000000"
    )
    later = copy.deepcopy(generic)
    later.external_id = IDS[1]
    later.url = "https://www.autoscout24.de/angebote/" + IDS[1]
    before = [
        tuple(row)
        for row in replay.worker.db.conn.execute("SELECT * FROM observations")
    ]
    replay.worker.detail_jobs = {
        generic.url: (generic.source, None, generic),
        item.url: (SOURCE, None, item),
        later.url: (later.source, None, later),
    }
    replay.worker.details(replay.worker.db.start_run("cache_after_budget"))
    assert len(replay.calls) == 2
    assert replay.worker.detail_stats[SOURCE]["cached"] == 1
    assert replay.worker.detail_stats[generic.source]["deferred"] == 2
    assert replay.summary()["status"] == "partial"
    assert replay.summary()["expected_count"] == 3
    assert replay.summary()["received_count"] == 1
    assert replay.summary()["pages"] == 0
    assert [
        tuple(row)
        for row in replay.worker.db.conn.execute("SELECT * FROM observations")
    ] == before
