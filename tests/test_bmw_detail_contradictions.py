"""Bound native search contradictions cannot be satisfied by older own evidence."""

import json
from datetime import timedelta

import pytest
from bs4 import BeautifulSoup
from test_bmw_detail_policy import CASES, NativeReplay

from car_market.germany_retained import _raw_body

IDENTITY = "87605782"
NEW_VIN = "WBA21BX0907R95486"


def mutate_native_search(body, changes):
    soup = BeautifulSoup(body, "html.parser")
    node = soup.select_one("script#__NEXT_DATA__")
    document = json.loads(node.string)
    own = next(
        row
        for row in document["props"]["pageProps"]["searchResults"]["cars"]
        if str(row["id"]) == IDENTITY
    )
    for path, value in changes.items():
        target = own
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    node.string = json.dumps(document)
    return str(soup)


def history(conn):
    return {
        table: [
            tuple(row)
            for row in conn.execute("SELECT * FROM " + table + " ORDER BY id")
        ]
        for table in ("fetches", "observations", "events")
    }


def assert_history_unchanged(conn, original):
    current = history(conn)
    assert all(current[table][: len(rows)] == rows for table, rows in original.items())


def native_contradiction(tmp_path, monkeypatch, scenario, guard, fast):
    replay = NativeReplay(tmp_path, monkeypatch, "instamotion_de", fast=fast)
    try:
        # Same underspecified title in both passes; stable native fields change independently.
        replay.search_body = mutate_native_search(
            replay.search_body, {"title": "BMW 218i"}
        )
        replay.run()
        assert replay.accepted() > 0
        census = set(replay.ledger["cards"])
        original = history(replay.db.conn)
        initial_http = sum(
            url == replay.detail_url for url, detail in replay.calls if detail
        )
        replay.clock += timedelta(minutes=5)
        changes = {
            "country": {"location_country.iso_code": "NL"},
            "model": {
                "model.const_key": "MAKE_BMW-MODELFAMILY_220",
                "model.label": "220",
            },
            "vin": {"vin": NEW_VIN},
        }[scenario]
        replay.search_body = mutate_native_search(replay.search_body, changes)
        if guard == "cap0":
            replay.config.update(
                detail_limit=0, detail_batch_limit=0, fast_detail_limit=0
            )
        if guard == "backoff":
            blocked_run = replay.db.start_run(timestamp=replay.stamp())
            replay.db.save_fetch(
                blocked_run,
                replay.source,
                "https://www.instamotion.com/de/auto/other-id/blocked",
                "https://www.instamotion.com/de/auto/other-id/blocked",
                429,
                "controlled sibling target-host block",
                replay.stamp(),
                "test",
                None,
            )
        replay.restart()
        replay.run()
        assert set(replay.ledger["cards"]) == census
        assert_history_unchanged(replay.db.conn, original)
        detail = (
            replay.ledger.get("search_exclusions", {}).get(IDENTITY)
            or replay.ledger["details"][IDENTITY]
        )
        card = replay.ledger["cards"][IDENTITY]
        target_http = (
            sum(url == replay.detail_url for url, hit in replay.calls if hit)
            - initial_http
        )
        assert not detail.get("cached")
        if scenario in {"country", "model"}:
            assert IDENTITY not in replay.ledger["details"]
            assert card["scope"] == "excluded" and detail["scope"] == "excluded"
            assert detail["status"] == (
                "foreign_country" if scenario == "country" else "excluded_model"
            )
            assert detail["reason"] == (
                "search_country_excluded"
                if scenario == "country"
                else "search_model_excluded"
            )
            assert card["market_country"] == ("NL" if scenario == "country" else "DE")
            assert detail["fetch_id"] == card["fetch_id"]
            assert detail["fetched_at"] == replay.stamp()
            assert IDENTITY not in replay.ledger["pending_details"] and target_http == 0
        else:
            if scenario == "model":
                assert card["raw"]["model_family"] == "220"
            else:
                assert card["vin"] == NEW_VIN
            assert detail["status"] == "deferred"
            assert replay.ledger["pending_details"][IDENTITY] == detail
            assert detail["reason"] == (
                "detail_budget_deferred"
                if guard == "cap0"
                else "detail_host_backoff:429"
                if guard == "backoff"
                else "detail_search_conflict:"
                + ("raw.model_family" if scenario == "model" else "vin")
            )
            assert target_http == (1 if guard == "allowed" else 0)
            # The changed own search fact survives even when a fresh VDP repeats the old fact.
            own = replay.db.conn.execute(
                "SELECT o.details_json FROM listings l JOIN latest_observations o ON o.listing_id=l.id "
                "WHERE l.source=? AND l.external_id=?",
                (replay.source, IDENTITY),
            ).fetchone()
            current = json.loads(own[0])
            if scenario == "model":
                assert current["raw"]["model_family"] == "220"
            else:
                # A conflicting VIN must retain pending native evidence without changing identity.
                assert current["vin"] != NEW_VIN
                assert (
                    "VIN identities"
                    in replay.ledger["search_observation_errors"][IDENTITY]["reason"]
                )
        return {
            "scenario": scenario,
            "guard": guard,
            "fast": fast,
            "target_detail_http": target_http,
            "card": {
                k: card.get(k)
                for k in ("scope", "market_country", "vin", "fetch_id", "fetched_at")
            },
            "native_model": card["raw"].get("model_family"),
            "detail": {
                k: detail.get(k)
                for k in (
                    "status",
                    "scope",
                    "country",
                    "reason",
                    "cached",
                    "fetch_id",
                    "fetched_at",
                )
            },
            "pending": IDENTITY in replay.ledger["pending_details"],
            "census_preserved": True,
            "prior_fetch_observation_event_rows_unchanged": True,
        }
    finally:
        replay.db.conn.close()


@pytest.mark.parametrize("scenario", ["country", "model", "vin"])
@pytest.mark.parametrize("guard", ["allowed", "cap0", "backoff"])
@pytest.mark.parametrize("fast", [False, True])
def test_native_contradiction_after_restart(
    tmp_path, monkeypatch, scenario, guard, fast
):
    native_contradiction(tmp_path, monkeypatch, scenario, guard, fast)


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("country", [None, "UNKNOWN"])
def test_underspecified_search_reuses_compatible_original_cache(
    tmp_path, monkeypatch, fast, country
):
    replay = NativeReplay(tmp_path, monkeypatch, "instamotion_de", fast=fast)
    try:
        replay.run()
        first = dict(replay.ledger["details"][IDENTITY])
        original = history(replay.db.conn)
        replay.search_body = mutate_native_search(
            replay.search_body,
            {
                "title": "BMW 218i",
                "location_country.iso_code": country,
                "model.label": None,
            },
        )
        replay.clock += timedelta(minutes=5)
        replay.config.update(detail_limit=0, detail_batch_limit=0, fast_detail_limit=0)
        replay.restart()
        replay.run()
        detail = replay.ledger["details"][IDENTITY]
        assert (
            detail["cached"]
            and detail["status"] == "own_de"
            and detail["scope"] == "target"
        )
        assert (
            detail["fetch_id"] == first["fetch_id"]
            and detail["fetched_at"] == first["fetched_at"]
        )
        assert replay.detail_calls() == 1
        assert_history_unchanged(replay.db.conn, original)
    finally:
        replay.db.conn.close()


@pytest.mark.parametrize("source", CASES)
@pytest.mark.parametrize("cap0", [False, True])
def test_same_sha_fetch_requires_its_own_accepted_observation(
    tmp_path, monkeypatch, source, cap0
):
    replay = NativeReplay(tmp_path, monkeypatch, source)
    try:
        replay.run()
        original = history(replay.db.conn)
        old = replay.db.conn.execute(
            "SELECT * FROM fetches WHERE url=?", (replay.detail_url,)
        ).fetchone()
        replay.clock += timedelta(minutes=5)
        run = replay.db.start_run(timestamp=replay.stamp())
        fresh = replay.db.save_fetch(
            run,
            source,
            old["url"],
            old["final_url"],
            200,
            _raw_body(replay.db.path.parent, old["raw_path"], old["content_hash"]),
            replay.stamp(),
            old["transport"],
            None,
        )
        assert (
            replay.db.conn.execute(
                "SELECT count(*) FROM observations WHERE fetch_id=?", (fresh,)
            ).fetchone()[0]
            == 0
        )
        if cap0:
            replay.config.update(
                detail_limit=0, detail_batch_limit=0, fast_detail_limit=0
            )
        replay.clock += timedelta(minutes=1)
        replay.restart()
        replay.run()
        assert replay.detail_calls() == (1 if cap0 else 2)
        assert_history_unchanged(replay.db.conn, original)
        details = (
            replay.ledger["details"]
            if source == "instamotion_de"
            else replay.ledger["detail_results"]
        )
        assert not any(row.get("cached") for row in details.values())
        if cap0:
            assert replay.ledger["pending_details"]
    finally:
        replay.db.conn.close()


@pytest.mark.parametrize("source", CASES)
@pytest.mark.parametrize("fast", [False, True])
def test_expired_unchanged_detail_accepts_exact_new_capture_then_caches(
    tmp_path, monkeypatch, source, fast
):
    replay = NativeReplay(tmp_path, monkeypatch, source, fast)
    try:
        replay.run()
        original = history(replay.db.conn)
        replay.clock += timedelta(hours=25)
        replay.config.update(detail_limit=0, detail_batch_limit=0, fast_detail_limit=0)
        replay.restart()
        replay.run()
        assert replay.detail_calls() == 1 and replay.ledger["pending_details"]
        replay.config.update(detail_limit=4, detail_batch_limit=4, fast_detail_limit=4)
        replay.clock += timedelta(minutes=5)
        replay.restart()
        replay.run()
        assert replay.detail_calls() == 2
        latest = replay.db.conn.execute(
            "SELECT * FROM fetches WHERE url=? ORDER BY id DESC", (replay.detail_url,)
        ).fetchone()
        assert (
            replay.db.conn.execute(
                "SELECT count(*) FROM observations WHERE fetch_id=? AND observed_at=?",
                (latest["id"], latest["fetched_at"]),
            ).fetchone()[0]
            > 0
        )
        replay.clock += timedelta(minutes=5)
        replay.restart()
        replay.run()
        assert replay.detail_calls() == 2
        details = (
            replay.ledger["details"]
            if source == "instamotion_de"
            else replay.ledger["detail_results"]
        )
        cached = next(row for row in details.values() if row.get("cached"))
        assert (
            cached["fetch_id"] == latest["id"]
            and cached["fetched_at"] == latest["fetched_at"]
        )
        assert_history_unchanged(replay.db.conn, original)
    finally:
        replay.db.conn.close()
