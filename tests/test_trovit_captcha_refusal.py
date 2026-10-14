"""Native challenge projection, actual transport/admission and original clocks."""

import dataclasses
import hashlib
import json
import socket
import sys
from email.message import Message
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace

import pytest
import test_trovit_detail_backoff as native

from car_market import fetch as fm
from car_market import trovit_details as td
from car_market.fetch import Response
from car_market.parse import Listing
from car_market.trovit_detail_policy import _native_id, in_error_backoff

FIXTURES = Path(__file__).parent / "fixtures/trovit_captcha_refusal"
NATIVE = json.loads((FIXTURES / "manifest.json").read_text())
BODY = (FIXTURES / "challenge.html").read_text()
REFUSAL = "trovit_captcha_refusal"
FINAL = NATIVE[0]["projected_final_route"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Provider network is forbidden in native fixture tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


class Wire:
    def __init__(self, body=BODY, final=FINAL):
        self.url, self.status, self.body = final, 200, body.encode()
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, maximum):
        return self.body[:maximum]


@pytest.mark.parametrize("capture", NATIVE, ids=lambda r: str(r["original_fetch_id"]))
def test_native_projection_transport_refusal_not_success_cache_or_browser(
    monkeypatch, capture
):
    assert hashlib.sha256(BODY.encode()).hexdigest() == capture["projected_SHA256"]
    calls, browsers = [], []
    request = "https://rd.clk.thribee.com/id.NATIVE_REPLAY/origin.2/"
    fetcher = fm.Fetcher(delay=0, browser=True)
    monkeypatch.setattr(fm, "now", lambda: capture["original_clock"])
    monkeypatch.setattr(
        fm.urllib.request, "urlopen", lambda *a, **k: calls.append(1) or Wire()
    )
    monkeypatch.setattr(fetcher, "browser_get", lambda url: browsers.append(url))
    first = fetcher.get(request)
    assert first.error == REFUSAL and first.status == 200
    assert first.body == BODY and first.timestamp == capture["original_clock"]
    assert not fetcher._cache and not browsers
    second = fetcher.get(request)
    assert second.error == REFUSAL and len(calls) == 2
    assert not browsers and not fetcher._cache


def test_old_success_cache_is_held_without_refetch_or_capture_renewal(monkeypatch):
    url = "https://rd.clk.thribee.com/id.EXISTING/origin.2/"
    original = Response(url, FINAL, 200, BODY, NATIVE[0]["original_clock"])
    fetcher = fm.Fetcher(delay=0)
    # Simulate an older pre-classifier entry. Direct API must never re-credit it.
    fetcher._cache[url] = dataclasses.replace(original)
    fetcher._cache[FINAL] = dataclasses.replace(original)
    monkeypatch.setattr(
        fm.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("cached refusal must not refetch"),
    )
    result = fetcher.get(url)
    assert result.error == REFUSAL and result.timestamp == original.timestamp
    assert result.body == BODY and result.status == 200 and not fetcher._cache
    assert original.error is None
    if hasattr(fetcher, "_remember_response"):
        fetcher._remember_response(original)
        assert not fetcher._cache and original.error is None


def test_actual_browser_result_refused_before_cookie_or_success_cache(monkeypatch):
    calls = []

    class Page:
        url = FINAL

        def set_default_timeout(self, value):
            pass

        def goto(self, *a, **k):
            calls.append("controlled_navigation")
            return SimpleNamespace(status=200)

        def wait_for_timeout(self, delay):
            pass

        def get_by_role(self, *a, **k):
            return SimpleNamespace(count=lambda: 0)

        def content(self):
            return BODY

    class Context:
        def new_page(self):
            return Page()

        def cookies(self):
            pytest.fail("refusal cookies must not enter successful session")

    class Browser:
        def new_context(self, **kwargs):
            return Context()

        def close(self):
            calls.append("closed")

    class Playwright:
        def __enter__(self):
            return SimpleNamespace(
                chromium=SimpleNamespace(launch=lambda **k: Browser())
            )

        def __exit__(self, *args):
            return False

    monkeypatch.setitem(
        sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=Playwright)
    )
    monkeypatch.setattr(fm, "now", lambda: NATIVE[0]["original_clock"])
    fetcher = fm.Fetcher(delay=0, browser=True)
    result = fetcher._browser_get("https://autos.trovit.nl/listing/owned.ABC/")
    assert result.error == REFUSAL and result.status == 200 and result.body == BODY
    assert (
        result.timestamp == NATIVE[0]["original_clock"]
        and result.transport == "browser"
    )
    assert calls == ["controlled_navigation", "closed"] and not fetcher._cache


@pytest.mark.parametrize(
    "final",
    [
        "https://autos.trovit.nl/listing/owned.ABC/",
        "https://dealer.invalid/index.php/cod.captcha_check/",
        "https://autos.trovit.nl.invalid/index.php/cod.captcha_check/",
        "http://autos.trovit.nl/index.php/cod.captcha_check/",
        "https://user@autos.trovit.nl/index.php/cod.captcha_check/",
        "https://autos.trovit.nl:8443/index.php/cod.captcha_check/",
        "https://autos.trovit.nl:bad/index.php/cod.captcha_check/",
        "https://autos.trovit.nl/index.php/cod.captcha_check_extra/",
        "https://autos.trovit.nl/index.php/cod.captcha_check/#fragment",
    ],
)
def test_foreign_unsupported_or_listing_route_is_not_explicit_refusal(final):
    assert not getattr(td, "trovit_captcha_refusal", lambda *a: False)(BODY, final)


@pytest.mark.parametrize(
    "body",
    [
        "<p>captcha mentioned in own description</p>",
        '<script src="https://www.google.com/recaptcha/api.js"></script>',
        BODY.replace('method="POST"', 'method="GET"'),
        BODY.replace('cod.captcha_check"', 'cod.contact"'),
        BODY.replace('class="g-recaptcha"', 'class="other"'),
        BODY.replace("</body>", BODY + "</body>"),
        BODY.replace("<body>", '<body id="atpage" class="cars" data-id="ABC">'),
        BODY.replace(
            "https://autos.trovit.nl/index.php", "https://foreign.invalid/index.php"
        ),
    ],
)
def test_script_contact_or_ambiguous_form_does_not_classify(body):
    assert not getattr(td, "trovit_captcha_refusal", lambda *a: False)(body, FINAL)


def test_legitimate_recaptcha_contact_success_keeps_existing_cache(monkeypatch):
    body = BODY.replace('cod.captcha_check"', 'cod.contact"')
    url = "https://autos.trovit.nl/listing/owned.ABC/"
    calls = []
    monkeypatch.setattr(
        fm.urllib.request, "urlopen", lambda *a, **k: calls.append(1) or Wire(body, url)
    )
    fetcher = fm.Fetcher(delay=0)
    first, second = fetcher.get(url), fetcher.get(url)
    assert first.error is None and second.error is None and len(calls) == 1
    assert first.timestamp == second.timestamp


def queue_three(tmp_path, monkeypatch, *, workers=1, limit=4):
    pairs = native.MANIFEST["pairs"][:3]
    db, c, first_id, _, calls, clock = native.setup(
        tmp_path,
        monkeypatch,
        pairs[0],
        seed_error=False,
        workers=workers,
        detail_cache_hours=0,
        detail_batch_limit=limit,
        detail_limit=limit,
    )
    ids = [first_id]
    root = native.MANIFEST["older_root"]
    root_fetch = db.conn.execute(
        "SELECT id,run_id FROM fetches ORDER BY id LIMIT 1"
    ).fetchone()
    items = [native.native_card(pair, current=False) for pair in pairs]
    for item in items[1:]:
        ids.append(
            db.observe(root_fetch["run_id"], item, root["fetched_at"], root_fetch["id"])
        )
    by_id = {p["native_id"]: p for p in pairs}

    def get(url):
        from car_market.trovit_detail_policy import _native_id

        identity = _native_id(url)
        calls.append(identity)
        previous = by_id[identity]["previous"]
        return Response(
            url,
            previous["final_url"],
            200,
            native.body(previous),
            previous["fetched_at"],
            transport="native_captcha_replay",
        )

    c.fetcher = SimpleNamespace(get=get)
    for item, listing in zip(items, ids):
        c.queue_details(item, listing)
    return db, c, ids, items, calls, clock


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_actual_collector_stops_logical_trovit_with_bounded_inflight_and_keeps_other_source(
    tmp_path, monkeypatch, workers
):
    db, c, ids, items, calls, _ = queue_three(tmp_path, monkeypatch, workers=workers)
    other = Listing(
        "autoweek",
        "synthetic-other",
        "https://www.autoweek.nl/occasions/control/",
        "BMW 218i Active Tourer",
        price=9000,
        year=2016,
        fuel="petrol",
        transmission="manual",
    ).qualify()
    run = db.start_run()
    fid = db.save_fetch(
        run,
        other.source,
        other.url,
        other.url,
        200,
        "synthetic_other_source",
        native.CLOCK.isoformat(),
    )
    other_id = db.observe(run, other, native.CLOCK.isoformat(), fid)
    c.queue_details(other, other_id)
    original_get = c.fetcher.get
    unrelated = []

    def get(url):
        if url == other.url:
            unrelated.append(url)
            return Response(
                url, url, 200, None, native.CLOCK.isoformat(), "controlled_unrelated"
            )
        return original_get(url)

    c.fetcher.get = get
    before = {
        t: [tuple(r) for r in db.conn.execute("SELECT * FROM " + t + " ORDER BY id")]
        for t in ("observations", "events", "listings")
    }
    c.details(db.start_run())
    assert 1 <= len(calls) <= min(workers, 3) and len(unrelated) == 1
    assert len(calls) == 1 if workers == 1 else True
    assert c.detail_stats["trovit"]["parsed"] == 0
    attempts = list(
        db.conn.execute("SELECT * FROM fetches WHERE transport='native_captcha_replay'")
    )
    assert len(attempts) == len(calls) and all(
        r["error"] == REFUSAL and r["status"] == 200 for r in attempts
    )
    for row in attempts:
        identity = _native_id(row["url"])
        previous = next(
            p["previous"]
            for p in native.MANIFEST["pairs"]
            if p["native_id"] == identity
        )
        assert row["url"] == next(
            item.url for item in items if item.external_id == identity
        )
        assert (
            row["content_hash"] == previous["content_hash"]
            and row["fetched_at"] == previous["fetched_at"]
        )
    assert {
        t: [tuple(r) for r in db.conn.execute("SELECT * FROM " + t + " ORDER BY id")]
        for t in before
    } == before
    assert set(ids) <= {r[0] for r in db.conn.execute("SELECT id FROM listings")}
    assert len(calls) + len(unrelated) <= 4
    db.conn.close()


def test_ordered_slow_first_future_cannot_admit_trovit_after_later_getter_refusal(
    tmp_path, monkeypatch
):
    from car_market import collect as cm
    from car_market.parse import Listing

    db, c, _ids, items, calls, _ = queue_three(tmp_path, monkeypatch, workers=2)
    slow_done = Event()
    refused = Event()
    ordinary = Listing(
        "autoweek",
        "synthetic-slow",
        "https://www.autoweek.nl/occasions/slow/",
        "BMW 218i Active Tourer",
    ).qualify()
    other = Listing(
        "anwb",
        "synthetic-other",
        "https://www.anwb.nl/auto/kopen/control",
        "BMW 218i Active Tourer",
    ).qualify()
    c.detail_jobs[ordinary.url] = ("portal", None, ordinary)
    c.detail_jobs[other.url] = ("portal", None, other)
    ordered = [ordinary.url, items[0].url, items[1].url, other.url, items[2].url]
    monkeypatch.setattr(
        cm,
        "fair_detail_jobs",
        lambda jobs, cursor: sorted(jobs, key=lambda job: ordered.index(job[0])),
    )
    c.config["detail_batch_limit"] = c.config["detail_limit"] = 5
    original = c.fetcher.get
    trace = []
    classified = [False]
    real_classifier = td.trovit_captcha_refusal

    def classify(body, final):
        answer = real_classifier(body, final)
        classified[0] |= answer
        return answer

    class ObservedLock:
        def __init__(self):
            self.lock = Lock()

        def __enter__(self):
            self.lock.acquire()

        def __exit__(self, *args):
            if classified[0]:
                # This runs after the actual worker has updated blocked_sources.
                refused.set()
            self.lock.release()

    monkeypatch.setattr(td, "trovit_captcha_refusal", classify)
    monkeypatch.setattr(cm, "Lock", ObservedLock)

    def get(url):
        trace.append(url)
        if url == ordinary.url:
            assert refused.wait(3), (
                "later getter must finish before ordered first future"
            )
            slow_done.set()
            return Response(
                url, url, 200, None, native.CLOCK.isoformat(), "controlled_ordinary"
            )
        if url == other.url:
            return Response(
                url, url, 200, None, native.CLOCK.isoformat(), "controlled_unrelated"
            )
        response = original(url)
        return response

    c.fetcher.get = get
    c.details(db.start_run())
    assert slow_done.is_set() and calls == [items[0].external_id]
    assert ordinary.url in trace and other.url in trace
    assert items[1].url not in trace and items[2].url not in trace
    assert c.detail_stats["trovit"]["attempted"] == 1
    db.conn.close()


@pytest.mark.parametrize(
    "error", [None, "detail_unparsed", "detail_parse_error", REFUSAL]
)
@pytest.mark.parametrize("cache_hours", [0, 24])
def test_historical_own_captcha_reopen_and_revisions_cannot_release_refusal(
    tmp_path, monkeypatch, error, cache_hours
):
    pair = native.MANIFEST["pairs"][0]
    db, c, _, denied, calls, _clock = native.setup(
        tmp_path, monkeypatch, pair, detail_cache_hours=cache_hours
    )
    db.conn.execute("UPDATE fetches SET error=? WHERE id=?", (error, denied))
    db.conn.commit()
    before = native.history(db)
    db, c = native.reopen(db, c)
    c.source_parser_refresh_since["trovit"] = (
        native.CLOCK - native.timedelta(seconds=10)
    ).isoformat()
    c.parser_refresh_since = c.source_parser_refresh_since["trovit"]
    item, listing = native.fresh_search(db, pair)
    assert in_error_backoff(c, item.url, item)
    old_fetch_rows = before["fetches"]
    c.queue_details(item, listing)
    c.details(db.start_run())
    assert not calls
    assert [
        tuple(r)
        for r in db.conn.execute(
            "SELECT * FROM fetches WHERE id<=? ORDER BY id", (denied,)
        )
    ] == old_fetch_rows
    db.conn.close()


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_refusal_preserves_paid_caps_and_pending_native_ids(
    tmp_path, monkeypatch, limit
):
    db, c, ids, items, calls, _ = queue_three(tmp_path, monkeypatch, limit=limit)
    c.details(db.start_run())
    assert len(calls) == min(limit, 1)
    assert set(ids) <= {r[0] for r in db.conn.execute("SELECT id FROM listings")}
    assert c.detail_jobs and {item.external_id for item in items} <= {
        r[0]
        for r in db.conn.execute(
            "SELECT external_id FROM listings WHERE source='trovit'"
        )
    }
    db.conn.close()


@pytest.mark.parametrize("error", ["detail_unparsed", "detail_parse_error"])
def test_real_nonchallenge_parser_revision_exception_still_allowed(
    tmp_path, monkeypatch, error
):
    pair = native.MANIFEST["pairs"][0]
    db, c, _, denied, _, _ = native.setup(tmp_path, monkeypatch, pair)
    db.conn.execute(
        "UPDATE fetches SET error=?,final_url=? WHERE id=?",
        (error, "https://autos.trovit.nl/listing/owned.ABC/", denied),
    )
    db.conn.commit()
    c.source_parser_refresh_since["trovit"] = (
        native.CLOCK - native.timedelta(seconds=10)
    ).isoformat()
    item = native.native_card(pair, current=True)
    assert not in_error_backoff(c, item.url, item)
    db.conn.close()


def test_verified_trovit_family_alias_held_without_pausing_unrelated_gaspedaal(
    tmp_path, monkeypatch
):
    from car_market import collect as cm

    db, c, ids, items, calls, _ = queue_three(tmp_path, monkeypatch)
    c.detail_jobs.clear()
    alias = dataclasses.replace(items[1], source="gaspedaal")
    other = Listing(
        "gaspedaal",
        "synthetic-unrelated",
        "https://dealer.invalid/unrelated",
        "Unrelated scheduling control",
    )
    c.detail_jobs[items[0].url] = ("trovit", ids[0], items[0])
    c.detail_jobs[alias.url] = ("portal", ids[1], alias)
    c.detail_jobs[other.url] = ("portal", None, other)
    order = [items[0].url, alias.url, other.url]
    monkeypatch.setattr(
        cm,
        "fair_detail_jobs",
        lambda jobs, cursor: sorted(jobs, key=lambda j: order.index(j[0])),
    )
    original_get = c.fetcher.get
    unrelated = []

    def get(url):
        if url == other.url:
            unrelated.append(url)
            return Response(
                url, url, 200, None, native.CLOCK.isoformat(), "controlled_unrelated"
            )
        return original_get(url)

    c.fetcher.get = get
    c.details(db.start_run())
    assert calls == [items[0].external_id] and unrelated == [other.url]
    assert c.detail_stats["gaspedaal"]["attempted"] == 1
    db.conn.close()
