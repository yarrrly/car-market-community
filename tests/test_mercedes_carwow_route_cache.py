"""Original Carwow card/PDP clocks; route reuse is not accepted stock proof."""

import gzip
import hashlib
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from car_market import mercedes_collect
from car_market.db import Database
from car_market.fetch import Response
from car_market.mercedes_collect import MercedesCollector
from car_market.model_profiles import MERCEDES_PROFILE

FIXTURES = Path(__file__).resolve().parent / "fixtures/carwow_mercedes_routes"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
IDS = [r["external_id"] for r in MANIFEST["projections"] if r["kind"] == "card"]
CLOCK = datetime.fromisoformat("2026-10-13T00:27:58.105625+00:00")


def native(identity, kind):
    meta = next(
        r
        for r in MANIFEST["projections"]
        if r["external_id"] == identity and r["kind"] == kind
    )
    archive = (FIXTURES / meta["fixture"]).read_bytes()
    assert hashlib.sha256(archive).hexdigest() == meta["projected_gzip_sha256"]
    body = gzip.decompress(archive)
    assert hashlib.sha256(body).hexdigest() == meta["projected_body_sha256"]
    return body.decode(), meta


def set_clock(monkeypatch, instant):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(mercedes_collect, "datetime", Clock)


@pytest.fixture(autouse=True)
def original_evaluation_clock(monkeypatch):
    set_clock(monkeypatch, CLOCK)


class Replay:
    def __init__(self, tmp_path, limit=1, workers=1):
        self.db = Database(tmp_path / "data/market.sqlite3")
        self.worker = MercedesCollector(
            self.db,
            {
                "scope": {"country": "DE", "model_profile": MERCEDES_PROFILE},
                "queries": [],
                "detail_limit": limit,
                "detail_cache_hours": 6,
                "detail_error_backoff_hours": 6,
                "request_workers": workers,
            },
        )
        self.worker._secondary_detail_attempts = 0
        self.worker._secondary_detail_deferrals = []
        self.calls = []
        self.owner_thread = threading.get_ident()
        self.worker._secondary_get = self.get
        self.run = self.db.start_run("offline_native_route")
        self.rows = {}

    def item(self, identity=IDS[0]):
        meta = MANIFEST["search"]
        return next(
            i
            for i in self.worker._parse(
                "carwow_de", meta["projected_html"], meta["final_url"]
            ).listings
            if i.external_id == identity
        )

    def seed(self, identity=IDS[0], kind="card", error=None, stamp=None):
        body, meta = native(identity, kind)
        fetch = self.db.save_fetch(
            self.run,
            "carwow_de",
            meta["url"],
            meta["final_url"],
            200,
            body,
            stamp or meta["original_clock"],
            error=error,
        )
        self.rows[identity, kind] = dict(
            self.db.conn.execute(
                "SELECT * FROM fetches WHERE id=?", (fetch,)
            ).fetchone()
        )
        return fetch

    def get(self, url, **kwargs):
        assert threading.get_ident() == self.owner_thread
        self.calls.append(url)
        if url == MANIFEST["search"]["url"]:
            meta = MANIFEST["search"]
            return Response(
                url, url, 200, meta["projected_html"], meta["original_clock"]
            )
        for meta in MANIFEST["projections"]:
            if meta["url"] == url:
                body, _ = native(meta["external_id"], meta["kind"])
                # Fake transport replays original captures, never a fresh clock.
                return Response(url, url, 200, body, meta["original_clock"])
        raise AssertionError("Unadvertised request: " + url)

    def header(self, identity=IDS[0], original=None):
        original = original or self.item(identity)
        return self.worker._secondary_detail_get(
            self.run, "carwow_de", original.url, original=original
        )

    def collect(self):
        self.worker._collect_secondary(
            self.run,
            {
                "source": "carwow_de",
                "key": "native_c_class_all",
                "url": MANIFEST["search"]["url"],
                "max_pages": 1,
                "detail_limit": 3,
            },
        )
        return json.loads(
            self.db.conn.execute(
                "SELECT value FROM monitor_state WHERE key=?",
                ("model_discovery:carwow_de:native_c_class_all",),
            ).fetchone()[0]
        )


@pytest.mark.parametrize("workers", [1, 2, 4, 8])
@pytest.mark.parametrize("limit", [0, 1, 2])
def test_real_pipeline_replays_card_then_charges_only_pdp(tmp_path, limit, workers):
    replay = Replay(tmp_path, limit, workers)
    for identity in IDS:
        replay.seed(identity)
    old = [
        tuple(r) for r in replay.db.conn.execute("SELECT * FROM fetches ORDER BY id")
    ]
    result = replay.collect()
    assert result["received"] == result["expected"] == 3
    assert result["enumeration_status"] == "complete"
    assert result["detail_status"] == "partial"
    assert {r["external_id"] for r in result["listings"]} == set(IDS)
    assert replay.worker._secondary_detail_attempts == limit
    assert replay.calls == [MANIFEST["search"]["url"]] + [
        native(i, "pdp")[1]["url"] for i in IDS[:limit]
    ]
    assert result["details_attempted"] == sorted(IDS[:limit])
    assert result["details_cached"] == []
    assert result["details_received"] == limit
    assert result["details_deferred"] == sorted(IDS[limit:])
    assert result["country_held"] == IDS
    assert (
        replay.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    )
    assert [
        tuple(r)
        for r in replay.db.conn.execute("SELECT * FROM fetches ORDER BY id LIMIT 3")
    ] == old
    if limit == 2:
        scopes = {r["external_id"]: r["scope"] for r in result["listings"]}
        assert scopes[IDS[0]] == "needs_confirmation" and scopes[IDS[1]] == "excluded"
    replay.db.conn.close()


@pytest.mark.parametrize("identity", IDS)
@pytest.mark.parametrize("error", [None, "detail_unparsed"])
def test_native_route_replay_keeps_original_receipt_and_no_history_growth(
    tmp_path, identity, error
):
    replay = Replay(tmp_path, 0)
    fetch = replay.seed(identity, error=error)
    old = tuple(replay.db.conn.execute("SELECT * FROM fetches").fetchone())
    response = replay.header(identity)
    meta = native(identity, "card")[1]
    assert response.secondary_route_evidence is True
    assert response.secondary_fetch_id == fetch
    assert response.timestamp == meta["original_clock"]
    assert (
        hashlib.sha256(response.body.encode()).hexdigest()
        == meta["projected_body_sha256"]
    )
    assert not getattr(response, "secondary_cached_accepted", None)
    assert replay.calls == [] and replay.worker._secondary_detail_attempts == 0
    assert tuple(replay.db.conn.execute("SELECT * FROM fetches").fetchone()) == old
    replay.db.conn.close()


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_no_retained_card_charges_card_and_pdp_separately(tmp_path, limit):
    replay = Replay(tmp_path, limit)
    result = replay.collect()
    assert replay.worker._secondary_detail_attempts == limit
    assert replay.calls == [MANIFEST["search"]["url"]] + (
        [native(IDS[0], "card")[1]["url"]] if limit else []
    ) + ([native(IDS[0], "pdp")[1]["url"]] if limit == 2 else [])
    assert result["details_received"] == int(limit == 2)
    assert result["status"] == "partial" and result["received"] == 3
    replay.db.conn.close()


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("limit", [0, 1])
def test_route_does_not_change_normal_pdp_cache_admission(tmp_path, limit, accepted):
    replay = Replay(tmp_path, limit)
    replay.seed()
    fetch = replay.seed(kind="pdp")
    # Counterfactual persisted receipt only: actual three unknown-country PDPs
    # have no accepted publications. This isolates the existing VDP policy.
    replay.worker._accepted_detail_capture = lambda *a: (
        {"fetch_id": fetch, "observed_at": native(IDS[0], "pdp")[1]["original_clock"]}
        if accepted
        else None
    )
    response = replay.header()
    assert response.secondary_route_evidence
    url = native(IDS[0], "pdp")[1]["url"]
    if not accepted and limit == 0:
        with pytest.raises(ValueError, match="detail_budget_deferred"):
            replay.worker._secondary_detail_get(
                replay.run, "carwow_de", url, original=replay.item()
            )
    else:
        response = replay.worker._secondary_detail_get(
            replay.run, "carwow_de", url, original=replay.item()
        )
        assert response.timestamp == native(IDS[0], "pdp")[1]["original_clock"]
        assert bool(getattr(response, "secondary_cached_accepted", None)) == accepted
        if accepted:
            assert response.secondary_fetch_id == fetch
    assert replay.calls == ([] if accepted or not limit else [url])
    assert (
        replay.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    )
    replay.db.conn.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "source",
        "id",
        "card_uri",
        "native_card_uri",
        "profile",
        "country",
        "model",
        "fuel",
        "foreign_stock",
        "field_conflicts",
    ],
)
def test_queue_binding_or_conclusive_contradiction_never_replays_or_fetches(
    tmp_path, boundary
):
    replay = Replay(tmp_path, 2)
    replay.seed()
    item = replay.item()
    if boundary == "source":
        item.source = "pkw_de"
    elif boundary == "id":
        item.external_id = IDS[1]
    elif boundary == "card_uri":
        item.url = item.url.replace("angebote.carwow.de", "review.invalid")
    elif boundary == "native_card_uri":
        item.raw["native_card_url"] = native(IDS[1], "card")[1]["url"]
    elif boundary == "profile":
        replay.worker.config["scope"]["model_profile"] = "bmw_218i_active_tourer"
    elif boundary == "country":
        replay.worker.country = "NL"
    elif boundary == "model":
        item.title = "Mercedes-Benz C200"
    elif boundary == "fuel":
        item.fuel = "diesel"
    elif boundary == "foreign_stock":
        item.raw["market_country"] = "BE"
    else:
        item.raw["field_conflicts"] = {"country": ["DE", "BE"]}
    with pytest.raises(
        ValueError, match="carwow_detail_owner_mismatch|detail_conclusively_excluded"
    ):
        replay.header(original=item)
    assert replay.calls == [] and replay.worker._secondary_detail_attempts == 0
    replay.db.conn.close()


def test_no_details_blocks_route_even_with_paid_allowance(tmp_path):
    replay = Replay(tmp_path, 2)
    replay.seed()
    replay.worker.specialized_details_enabled = False
    with pytest.raises(ValueError, match="details_disabled"):
        replay.header()
    assert replay.calls == []
    replay.db.conn.close()


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("logical_source", ["carwow_de", "quoka_de"])
def test_actual_origin_backoff_precedes_cached_card(tmp_path, status, logical_source):
    replay = Replay(tmp_path, 2)
    replay.seed()
    fetch = replay.db.save_fetch(
        replay.run,
        logical_source,
        "https://www.quoka.de/synthetic-refusal",
        "https://angebote.carwow.de",
        status,
        "refused",
        (CLOCK - timedelta(minutes=1)).isoformat(),
        error="http_" + str(status),
    )
    with pytest.raises(ValueError, match="detail_host_backoff"):
        replay.header()
    assert replay.worker._secondary_detail_deferrals[-1]["fetch_id"] == fetch
    assert replay.calls == []
    replay.db.conn.close()


def test_unrelated_origin_refusal_does_not_block_and_shared_tail_is_not_spent_twice(
    tmp_path,
):
    replay = Replay(tmp_path, 2)
    for identity in IDS:
        replay.seed(identity)
    replay.db.save_fetch(
        replay.run,
        "carwow_de",
        "https://www.quoka.de/blocked",
        "https://www.quoka.de/blocked",
        429,
        "refused",
        CLOCK.isoformat(),
        error="http_429",
    )
    replay.worker.detail_stats["pkw_de"] = {"attempted": 1}
    result = replay.collect()
    assert replay.worker._secondary_detail_attempts == 1
    assert result["details_received"] == 1
    assert replay.calls == [MANIFEST["search"]["url"], native(IDS[0], "pdp")[1]["url"]]
    replay.db.conn.close()


@pytest.mark.parametrize("which", ["global", "source"])
def test_parser_revision_requires_real_route_refresh(tmp_path, which):
    replay = Replay(tmp_path, 0)
    replay.seed()
    cutoff = (
        datetime.fromisoformat(native(IDS[0], "card")[1]["original_clock"])
        + timedelta(seconds=1)
    ).isoformat()
    if which == "global":
        replay.worker.parser_refresh_since = cutoff
    else:
        replay.worker.source_parser_refresh_since["carwow_de"] = cutoff
    with pytest.raises(ValueError, match="detail_budget_deferred"):
        replay.header()
    assert replay.calls == []
    replay.db.conn.close()


@pytest.mark.parametrize(
    "position,returns_route",
    [
        ("before", True),
        ("exact", False),
        ("after", False),
        ("future", False),
        ("naive", False),
    ],
)
def test_original_route_clock_obeys_true_ttl_without_renewal(
    tmp_path, monkeypatch, position, returns_route
):
    replay = Replay(tmp_path, 0)
    capture = datetime.fromisoformat(native(IDS[0], "card")[1]["original_clock"])
    if position == "naive":
        replay.seed(stamp=capture.replace(tzinfo=None).isoformat())
    else:
        replay.seed()
    instant = {
        "before": capture + timedelta(hours=6, microseconds=-1),
        "exact": capture + timedelta(hours=6),
        "after": capture + timedelta(hours=6, microseconds=1),
        "future": capture - timedelta(microseconds=1),
        "naive": CLOCK,
    }[position]
    set_clock(monkeypatch, instant)
    if returns_route:
        assert replay.header().timestamp == capture.isoformat()
    else:
        with pytest.raises(ValueError, match="detail_budget_deferred"):
            replay.header()
    assert replay.calls == []
    replay.db.conn.close()


@pytest.mark.parametrize("wall", ["challenge", "login", "wrong_pdp", "missing_link"])
def test_cached_route_requires_own_literal_native_link_and_no_access_wall(
    tmp_path, wall
):
    replay = Replay(tmp_path, 0)
    body, meta = native(IDS[0], "card")
    if wall == "challenge":
        body = "<title>Just a moment...</title>" + body
    elif wall == "login":
        body = "<title>Login required</title>" + body
    elif wall == "wrong_pdp":
        body = body.replace(IDS[0], IDS[1])
    else:
        body = "<p>No link</p>"
    replay.db.save_fetch(
        replay.run,
        "carwow_de",
        meta["url"],
        meta["final_url"],
        200,
        body,
        meta["original_clock"],
    )
    with pytest.raises(ValueError, match="detail_cached_unparsed"):
        replay.header()
    assert replay.calls == []
    replay.db.conn.close()


def test_reopen_retains_route_receipt_but_not_unaccepted_pdp_completion(tmp_path):
    first = Replay(tmp_path, 1)
    for identity in IDS:
        first.seed(identity)
    first.collect()
    old_rows = [
        tuple(r) for r in first.db.conn.execute("SELECT * FROM fetches ORDER BY id")
    ]
    first.db.conn.close()
    reopened = Replay(tmp_path, 0)
    result = reopened.collect()
    assert result["details_received"] == 0 and result["detail_status"] == "partial"
    assert result["details_budget_deferred"] == sorted(IDS)
    assert reopened.calls == [MANIFEST["search"]["url"]]
    assert [
        tuple(r)
        for r in reopened.db.conn.execute(
            "SELECT * FROM fetches ORDER BY id LIMIT ?", (len(old_rows),)
        )
    ] == old_rows
    assert (
        reopened.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    )
    reopened.db.conn.close()


@pytest.mark.parametrize(
    "mutation", ["scheme", "credentials", "port", "fragment", "duplicate_id"]
)
def test_unsupported_native_card_uri_is_never_authorized(tmp_path, mutation):
    replay = Replay(tmp_path, 2)
    item = replay.item()
    item.url = {
        "scheme": item.url.replace("https://", "javascript://"),
        "credentials": item.url.replace("https://", "https://user@"),
        "port": item.url.replace("angebote.carwow.de", "angebote.carwow.de:bad"),
        "fragment": item.url + "#other",
        "duplicate_id": item.url + "&id=" + IDS[1],
    }[mutation]
    item.raw["native_card_url"] = item.url
    with pytest.raises(ValueError, match="carwow_detail_owner_mismatch"):
        replay.header(original=item)
    assert replay.calls == []
    replay.db.conn.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("raw_path", None),
        ("content_hash", "0" * 64),
        ("final_url", "https://review.invalid/redirect"),
    ],
)
def test_route_requires_byte_receipt_and_exact_capture_uri(tmp_path, field, value):
    replay = Replay(tmp_path, 0)
    fetch = replay.seed()
    replay.db.conn.execute(
        "UPDATE fetches SET " + field + "=? WHERE id=?", (value, fetch)
    )
    replay.db.conn.commit()
    with pytest.raises(
        ValueError,
        match="detail_budget_deferred|detail_cache_unverified|detail_cached_unparsed",
    ):
        replay.header()
    assert replay.calls == []
    replay.db.conn.close()


@pytest.mark.parametrize("status", [403, 429])
def test_same_pass_real_refusal_does_not_follow_card_or_retry_other_ids(
    tmp_path, status
):
    replay = Replay(tmp_path, 2)
    get = replay.get

    def refusal(url, **kwargs):
        if url == MANIFEST["search"]["url"]:
            return get(url, **kwargs)
        replay.calls.append(url)
        return Response(
            url, url, status, "refused", CLOCK.isoformat(), "http_" + str(status)
        )

    replay.worker._secondary_get = refusal
    result = replay.collect()
    assert replay.calls == [MANIFEST["search"]["url"], native(IDS[0], "card")[1]["url"]]
    assert result["received"] == 3 and result["details_received"] == 0
    assert replay.worker._secondary_detail_attempts == 1
    assert result["detail_status"] == "partial"
    with pytest.raises(ValueError, match="detail_host_backoff"):
        replay.header(IDS[1])
    assert len(replay.calls) == 2
    replay.db.conn.close()
