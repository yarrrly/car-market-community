"""Original AutoUncle unchanged-facts/different-body cache regressions."""

import copy
import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from car_market.db import Database
from car_market.fetch import Response
from car_market.mercedes_autouncle_cache import accepted_capture
from car_market.mercedes_collect import MercedesCollector
from car_market.model_profiles import MERCEDES_PROFILE
from car_market.parse import Listing

FIXTURES = Path(__file__).parent / "fixtures/mercedes_autouncle_cache"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    for module in (
        "car_market.db",
        "car_market.provenance",
        "car_market.model_profiles",
        "car_market.mercedes_collect",
    ):
        monkeypatch.setattr(module + ".datetime", Clock)


def native(identity, version):
    filename = identity + "-" + version + ".html.gz"
    meta = next(row for row in MANIFEST if row["fixture"] == filename)
    encoded = (FIXTURES / filename).read_bytes()
    assert hashlib.sha256(encoded).hexdigest() == meta["projection_gzip_SHA256"]
    body = gzip.decompress(encoded)
    assert hashlib.sha256(body).hexdigest() == meta["projection_body_SHA256"]
    assert meta["original_body_SHA256"] != meta["projection_body_SHA256"]
    return body.decode(), meta


def worker(path, **config):
    db = Database(path / "data/market.sqlite3")
    result = MercedesCollector(
        db,
        {
            "scope": {"country": "NL", "model_profile": MERCEDES_PROFILE},
            "queries": [],
            "workers": 1,
            "detail_limit": 1,
            "detail_cache_hours": 6,
            "detail_error_backoff_hours": 6,
            **config,
        },
    )
    result._detail_get = lambda url: pytest.fail("No native cache control may issue real HTTP")
    return result


def history(collector):
    return {
        table: [
            tuple(row)
            for row in collector.db.conn.execute("SELECT * FROM " + table + " ORDER BY id")
        ]
        for table in ("observations", "fetches", "events")
    }


def seed(path, identity="13053744", recent_at=None):
    before_body, before_meta = native(identity, "original")
    body, meta = native(identity, "reconfirmed")
    collector = worker(path)
    url = meta["native_URI"]
    old = collector._parse("autouncle", before_body, url).listings[0]
    current = collector._parse("autouncle", body, url).listings[0]
    old_run = collector.db.start_run("fixture_original")
    old_fetch = collector.db.save_fetch(
        old_run, "autouncle", url, url, 200, before_body, before_meta["original_capture_clock"]
    )
    listing = collector._observe(
        old_run, copy.deepcopy(old), before_meta["original_capture_clock"], old_fetch
    )
    run = collector.db.start_run("fixture_reconfirmation")
    stamp = recent_at or meta["original_capture_clock"]
    fetch = collector.db.save_fetch(run, "autouncle", url, url, 200, body, stamp)
    assert collector._observe(run, copy.deepcopy(current), stamp, fetch) == listing
    assert collector.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    assert collector.db.conn.execute("SELECT fetch_id FROM observations").fetchone()[0] == old_fetch
    assert before_meta["original_body_SHA256"] != meta["original_body_SHA256"]
    assert before_meta["projection_body_SHA256"] != meta["projection_body_SHA256"]
    payload = collector.db.conn.execute(
        "SELECT details_json FROM latest_observations WHERE listing_id=?", (listing,)
    ).fetchone()[0]
    original = Listing(**json.loads(payload))
    collector.detail_jobs[url] = ("autouncle", listing, original)
    row = dict(collector.db.conn.execute("SELECT * FROM fetches WHERE id=?", (fetch,)).fetchone())
    return collector, url, listing, row, current, body


@pytest.mark.parametrize("identity", ["13053744", "14467994"])
@pytest.mark.parametrize("fast,limit", [(False, 1), (True, 1), (False, 0), (True, 0)])
def test_original_unchanged_native_body_is_cached_after_restart_without_retiming(
    tmp_path, identity, fast, limit
):
    collector, url, listing, row, own, body = seed(tmp_path, identity)
    old = history(collector)
    proofs = [
        tuple(value)
        for value in collector.db.conn.execute("SELECT * FROM field_verifications ORDER BY field")
    ]
    last_seen = collector.db.conn.execute(
        "SELECT last_seen FROM listings WHERE id=?", (listing,)
    ).fetchone()[0]
    queued = collector.detail_jobs[url]
    collector.db.conn.close()
    restored = worker(tmp_path, detail_limit=limit)
    restored.fast = fast
    restored.detail_jobs[url] = queued
    run = restored.db.start_run("test")
    restored.details(run)
    assert restored.detail_stats["autouncle"]["cached"] == 1
    assert restored.detail_stats["autouncle"]["attempted"] == 0
    assert history(restored) == old
    assert [
        tuple(value)
        for value in restored.db.conn.execute("SELECT * FROM field_verifications ORDER BY field")
    ] == proofs
    assert (
        restored.db.conn.execute(
            "SELECT last_seen FROM listings WHERE id=?", (listing,)
        ).fetchone()[0]
        == last_seen
    )
    accepted = accepted_capture(restored, row, own, queued[2], url, body, NOW)
    assert accepted["fetch_id"] == row["id"] and accepted["observed_at"] == row["fetched_at"]
    restored.db.conn.close()


@pytest.mark.parametrize(
    "change",
    [
        "price_only",
        "missing_year",
        "wrong_hash",
        "wrong_fetch",
        "wrong_source",
        "wrong_origin_ID",
        "inherited",
        "legacy_clock",
        "invalid_clock",
        "future_clock",
        "naive_clock",
    ],
)
def test_invalid_or_incomplete_verification_is_not_an_accepted_body(tmp_path, change):
    collector, url, _listing, row, own, body = seed(tmp_path)
    conn = collector.db.conn
    if change == "price_only":
        conn.execute("DELETE FROM field_verifications WHERE field<>'price'")
    elif change == "missing_year":
        conn.execute("DELETE FROM field_verifications WHERE field='year'")
    elif change == "wrong_hash":
        conn.execute("UPDATE field_verifications SET value_hash=? WHERE field='year'", ("0" * 64,))
    elif change == "wrong_fetch":
        conn.execute("UPDATE field_verifications SET fetch_id=1 WHERE field='year'")
    else:
        proof = conn.execute(
            "SELECT evidence_json FROM field_verifications WHERE field='year'"
        ).fetchone()
        origin = json.loads(proof[0])
        if change == "wrong_source":
            origin["source"] = "autoscout24"
        elif change == "wrong_origin_ID":
            origin["external_id"] = "foreign-publication"
        elif change == "inherited":
            origin["inherited"] = True
        elif change == "legacy_clock":
            origin["legacy_origin_time_unverified"] = True
        else:
            bad = {
                "invalid_clock": "invalid",
                "future_clock": (NOW + timedelta(minutes=1)).isoformat(),
                "naive_clock": "2026-10-12T09:57:22.615288",
            }[change]
            origin["observed_at"] = bad
            conn.execute("UPDATE field_verifications SET verified_at=? WHERE field='year'", (bad,))
        conn.execute(
            "UPDATE field_verifications SET evidence_json=? WHERE field='year'",
            (json.dumps(origin),),
        )
    conn.commit()
    before = history(collector)
    collector.config["detail_limit"] = 0
    run = collector.db.start_run("test")
    collector.details(run)
    assert collector.detail_stats["autouncle"].get("cached", 0) == 0
    assert collector.detail_stats["autouncle"]["attempted"] == 0
    assert (
        accepted_capture(collector, row, own, collector.detail_jobs[url][2], url, body, NOW) is None
    )
    assert history(collector) == before
    collector.db.conn.close()


@pytest.mark.parametrize(
    "change",
    [
        "SQL_price",
        "payload_price",
        "payload_source",
        "payload_ID",
        "payload_URL",
        "payload_country",
        "payload_model",
        "payload_body",
        "payload_registration",
        "field_conflict",
        "fresh_price",
        "fresh_country",
        "fresh_model",
        "fresh_VIN",
        "own_ID",
        "own_URI",
        "own_future_year",
        "native_model_field",
        "bound_profile",
        "config_profile",
        "config_generation",
        "metadata_only",
        "no_owned_URI",
        "body_hash",
        "prior_future_clock",
        "prior_naive_clock",
        "prior_wrong_source",
        "prior_failed_capture",
    ],
)
def test_current_or_native_binding_conflicts_hold_cache(tmp_path, change):
    collector, url, _listing, row, own, body = seed(tmp_path)
    original = collector.detail_jobs[url][2]
    conn = collector.db.conn
    if change == "SQL_price":
        conn.execute("UPDATE observations SET price_eur=1")
    elif change.startswith("payload_") or change == "field_conflict":
        payload = json.loads(conn.execute("SELECT details_json FROM observations").fetchone()[0])
        if change == "payload_price":
            payload["price"] = 1
        elif change == "payload_source":
            payload["source"] = "autoscout24"
        elif change == "payload_ID":
            payload["external_id"] = "foreign"
        elif change == "payload_URL":
            payload["url"] = url + "-foreign"
        elif change == "payload_country":
            payload["raw"]["country"] = "BE"
            payload["raw"]["country_code"] = "BE"
            payload["raw"]["market_country"] = "BE"
        elif change == "payload_model":
            payload["raw"]["model_profile"] = "bmw_218i_active_tourer"
        elif change == "payload_body":
            payload["raw"]["body_type"] = "sedan"
        elif change == "payload_registration":
            payload["raw"]["first_registration"] = "2035-99-99"
        else:
            payload["raw"]["field_conflicts"] = {"year": []}
        conn.execute("UPDATE observations SET details_json=?", (json.dumps(payload),))
    elif change == "fresh_price":
        original.price = 1
    elif change == "fresh_country":
        original.raw["market_country"] = "BE"
    elif change == "fresh_model":
        original.raw["model_brand"] = "BMW"
    elif change == "fresh_VIN":
        original.vin = "WDD2053401F123456"
    elif change == "own_ID":
        own.external_id = "foreign"
    elif change == "own_URI":
        own.url += "-foreign"
    elif change == "own_future_year":
        own.year = NOW.year + 1
    elif change == "native_model_field":
        own.raw["model_variant"] = "C200"
    elif change == "bound_profile":
        conn.execute(
            "UPDATE monitor_state SET value=? WHERE key='market_scope'",
            (json.dumps({"country": "NL", "model_profile": "bmw_218i_active_tourer"}),),
        )
    elif change == "config_profile":
        collector.config["scope"]["model_profile"] = "bmw_218i_active_tourer"
    elif change == "config_generation":
        collector.config["scope"]["generation"] = "C204"
    elif change == "metadata_only":
        row["raw_path"] = None
    elif change == "no_owned_URI":
        own.raw.pop("@id", None)
        own.raw.pop("url", None)
        own.raw.get("offers", {}).pop("url", None)
    elif change == "body_hash":
        body += "changed"
    elif change == "prior_future_clock":
        conn.execute(
            "UPDATE observations SET observed_at=?", ((NOW + timedelta(hours=1)).isoformat(),)
        )
    elif change == "prior_naive_clock":
        conn.execute("UPDATE observations SET observed_at='2026-10-08T15:31:18.968030'")
    elif change == "prior_wrong_source":
        conn.execute("UPDATE fetches SET source='autoscout24' WHERE id=1")
    elif change == "prior_failed_capture":
        conn.execute("UPDATE fetches SET error='detail_unparsed' WHERE id=1")
    conn.commit()
    before = history(collector)
    assert accepted_capture(collector, row, own, original, url, body, NOW) is None
    assert history(collector) == before
    collector.db.conn.close()


@pytest.mark.parametrize("kind", ["global", "source"])
@pytest.mark.parametrize("cutoff", ["after", "invalid", "future"])
def test_parser_revisions_are_guarded_without_retiming(tmp_path, kind, cutoff):
    collector, url, _listing, row, own, body = seed(tmp_path)
    value = {
        "after": NOW.isoformat(),
        "invalid": "bad clock",
        "future": (NOW + timedelta(hours=1)).isoformat(),
    }[cutoff]
    if kind == "global":
        collector.parser_refresh_since = value
    else:
        collector.source_parser_refresh_since["autouncle"] = value
    assert (
        accepted_capture(collector, row, own, collector.detail_jobs[url][2], url, body, NOW) is None
    )
    collector.config["detail_limit"] = 0
    collector.details(collector.db.start_run("test"))
    assert collector.detail_stats["autouncle"].get("cached", 0) == 0
    collector.db.conn.close()


@pytest.mark.parametrize(
    "age",
    [
        timedelta(hours=6) - timedelta(microseconds=1),
        timedelta(hours=6),
        timedelta(hours=6, microseconds=1),
    ],
)
def test_exact_TTL_uses_actual_reconfirmed_fetch_clock(tmp_path, age):
    collector, url, _listing, row, own, body = seed(tmp_path, recent_at=(NOW - age).isoformat())
    result = accepted_capture(collector, row, own, collector.detail_jobs[url][2], url, body, NOW)
    assert bool(result) is (age < timedelta(hours=6))
    collector.db.conn.close()


@pytest.mark.parametrize("status", [403, 404, 429])
def test_recent_own_refusal_keeps_existing_backoff_before_cache(tmp_path, status):
    collector, url, _listing, _row, _own, _body = seed(tmp_path)
    run = collector.db.start_run("refusal")
    collector.db.save_fetch(
        run,
        "autouncle",
        url,
        url,
        status,
        "held",
        (NOW - timedelta(minutes=1)).isoformat(),
        error="http_" + str(status),
    )
    before = history(collector)
    collector.details(collector.db.start_run("test"))
    assert collector.detail_stats["autouncle"].get("cached", 0) == 0
    assert collector.detail_stats["autouncle"]["backoff"] == 1
    assert history(collector) == before
    collector.db.conn.close()


def test_cross_source_actual_origin_refusal_still_blocks_cache(tmp_path):
    collector, _url, _listing, _row, _own, _body = seed(tmp_path)
    run = collector.db.start_run("refusal")
    collector.db.save_fetch(
        run,
        "portal",
        "https://other.example/redirect",
        "https://www.autouncle.nl",
        429,
        "held",
        (NOW - timedelta(minutes=1)).isoformat(),
        error="http_429",
    )
    collector.details(collector.db.start_run("test"))
    assert collector.detail_stats["autouncle"]["backoff"] == 1
    assert collector.detail_stats["autouncle"].get("cached", 0) == 0
    collector.db.conn.close()


def test_unrelated_origin_does_not_block_exact_owned_cache(tmp_path):
    collector, _url, _listing, _row, _own, _body = seed(tmp_path)
    run = collector.db.start_run("refusal")
    collector.db.save_fetch(
        run,
        "autouncle",
        "https://other.example/held",
        "https://other.example/held",
        403,
        "held",
        (NOW - timedelta(minutes=1)).isoformat(),
        error="http_403",
    )
    collector.details(collector.db.start_run("test"))
    assert collector.detail_stats["autouncle"]["cached"] == 1
    collector.db.conn.close()


def test_underspecified_own_queue_keeps_compatible_native_cache(tmp_path):
    collector, url, listing, _row, _own, _body = seed(tmp_path)
    original = Listing("autouncle", url, url, "")
    collector.detail_jobs[url] = ("autouncle", listing, original)
    collector.details(collector.db.start_run("test"))
    assert collector.detail_stats["autouncle"]["cached"] == 1
    collector.db.conn.close()


def test_no_details_routing_never_fetches_or_retimes_original_proof(tmp_path):
    collector, _url, _listing, _row, _own, _body = seed(tmp_path)
    before = history(collector)
    collector.run(fast=True, details=False, checks=False)
    assert history(collector) == before
    collector.db.conn.close()


@pytest.mark.parametrize(
    "change",
    ["missing_table", "malformed_origin", "malformed_payload", "missing_raw", "corrupt_raw"],
)
def test_unavailable_or_malformed_existing_proof_is_safely_deferred(tmp_path, change):
    collector, _url, _listing, row, _own, _body = seed(tmp_path)
    conn = collector.db.conn
    if change == "missing_table":
        conn.execute("DROP TABLE field_verifications")
    elif change == "malformed_origin":
        conn.execute("UPDATE field_verifications SET evidence_json='not JSON' WHERE field='year'")
    elif change == "malformed_payload":
        conn.execute("UPDATE observations SET details_json='not JSON'")
    else:
        archive = collector.db.path.parent / row["raw_path"]
        if change == "missing_raw":
            archive.unlink()
        else:
            archive.write_bytes(b"invalid gzip")
    conn.commit()
    collector.config["detail_limit"] = 0
    before = history(collector)
    collector.details(collector.db.start_run("test"))
    assert collector.detail_stats["autouncle"].get("cached", 0) == 0
    assert collector.detail_stats["autouncle"]["attempted"] == 0
    assert history(collector) == before
    collector.db.conn.close()


def test_cached_native_receipt_leaves_shared_allowance_for_new_publication(tmp_path):
    collector, _url, _listing, _row, _own, _body = seed(tmp_path)
    body, meta = native("14467994", "reconfirmed")
    own = collector._parse("autouncle", body, meta["native_URI"]).listings[0]
    collector.detail_jobs[own.url] = ("autouncle", None, own)
    calls = []

    def controlled_get(url):
        calls.append(url)
        return Response(url, url, 200, body, NOW.isoformat())

    collector._detail_get = controlled_get
    collector.details(collector.db.start_run("test"))
    assert calls == [own.url]
    assert collector.detail_stats["autouncle"]["cached"] == 1
    assert collector.detail_stats["autouncle"]["attempted"] == 1
    assert collector.detail_stats["autouncle"]["parsed"] == 1
    assert collector.db.conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 3
    collector.db.conn.close()


def seed_exact_observation(path, *, mileage=None, accepted_at=None):
    """Synthetic receipt clocks; exact owned record from the original projection."""
    import re

    body, meta = native("13053744", "reconfirmed")
    if mileage is not None:
        match = re.search(r"(<script[^>]*>)(.*?)(</script>)", body, re.DOTALL)
        record = json.loads(match.group(2))
        record["mileageFromOdometer"]["value"] = mileage
        body = body[: match.start(2)] + json.dumps(record) + body[match.end(2) :]
    collector = worker(path)
    url = meta["native_URI"]
    own = collector._parse("autouncle", body, url).listings[0]
    if mileage is not None:
        assert own.mileage == mileage
    current_stamp = meta["original_capture_clock"]
    stamp = (
        accepted_at or (datetime.fromisoformat(current_stamp) - timedelta(minutes=1)).isoformat()
    )
    run = collector.db.start_run("exact_observation")
    fetch = collector.db.save_fetch(run, "autouncle", url, url, 200, body, stamp)
    listing = collector._observe(run, copy.deepcopy(own), stamp, fetch)
    repeated = collector.db.save_fetch(run, "autouncle", url, url, 200, body, current_stamp)
    payload = collector.db.conn.execute(
        "SELECT details_json FROM latest_observations WHERE listing_id=?", (listing,)
    ).fetchone()[0]
    original = Listing(**json.loads(payload))
    collector.detail_jobs[url] = ("autouncle", listing, original)
    row = dict(
        collector.db.conn.execute("SELECT * FROM fetches WHERE id=?", (repeated,)).fetchone()
    )
    return collector, url, row, own, body, fetch, stamp


@pytest.mark.parametrize("proofs", ["empty", "partial", "old_schema"])
def test_existing_exact_observation_cache_survives_without_latest_verifications(tmp_path, proofs):
    collector, url, row, own, body, fetch, stamp = seed_exact_observation(tmp_path)
    assert collector._accepted_detail_capture("autouncle", url, row)["fetch_id"] == fetch
    if proofs == "old_schema":
        collector.db.conn.execute("DROP TABLE field_verifications")
    else:
        collector.db.conn.execute(
            "DELETE FROM field_verifications"
            + (" WHERE field<>'price'" if proofs == "partial" else "")
        )
    collector.db.conn.commit()
    before = history(collector)
    result = accepted_capture(collector, row, own, collector.detail_jobs[url][2], url, body, NOW)
    assert result and result["fetch_id"] == fetch
    assert result["observed_at"] == stamp and stamp != row["fetched_at"]
    collector.details(collector.db.start_run("test"))
    assert collector.detail_stats["autouncle"]["cached"] == 1
    assert history(collector) == before
    collector.db.conn.close()


@pytest.mark.parametrize(
    "change", ["expired", "global_revision", "source_revision", "fresh_price", "fresh_country"]
)
def test_exact_observation_cannot_bypass_original_clock_or_current_conflict(tmp_path, change):
    accepted_at = (NOW - timedelta(hours=6)).isoformat() if change == "expired" else None
    collector, url, row, own, body, _fetch, stamp = seed_exact_observation(
        tmp_path, accepted_at=accepted_at
    )
    collector.db.conn.execute("DELETE FROM field_verifications")
    collector.db.conn.commit()
    original = collector.detail_jobs[url][2]
    if change.endswith("revision"):
        cutoff = (datetime.fromisoformat(stamp) + timedelta(seconds=1)).isoformat()
        if change == "global_revision":
            collector.parser_refresh_since = cutoff
        else:
            collector.source_parser_refresh_since["autouncle"] = cutoff
    elif change == "fresh_price":
        original.price = 1
    elif change == "fresh_country":
        original.raw["market_country"] = "BE"
    before = history(collector)
    assert accepted_capture(collector, row, own, original, url, body, NOW) is None
    collector.config["detail_limit"] = 0
    collector.details(collector.db.start_run("test"))
    assert collector.detail_stats["autouncle"].get("cached", 0) == 0
    assert history(collector) == before
    collector.db.conn.close()


def test_native_zero_mileage_exact_cache_matches_zero_without_HTTP(tmp_path):
    collector, url, row, own, body, _fetch, _stamp = seed_exact_observation(tmp_path, mileage=0)
    before = history(collector)
    result = accepted_capture(collector, row, own, collector.detail_jobs[url][2], url, body, NOW)
    assert result and own.mileage == 0
    collector.details(collector.db.start_run("test"))
    assert collector.detail_stats["autouncle"]["cached"] == 1
    assert history(collector) == before
    collector.db.conn.close()


@pytest.mark.parametrize("invalid", [-1, True, float("nan"), float("inf")])
def test_invalid_mileage_remains_held_even_with_exact_observation(tmp_path, invalid):
    collector, url, row, own, body, _fetch, _stamp = seed_exact_observation(tmp_path, mileage=0)
    own.mileage = invalid
    assert (
        accepted_capture(collector, row, own, collector.detail_jobs[url][2], url, body, NOW) is None
    )
    collector.db.conn.close()


def test_zero_mileage_unchanged_new_body_uses_exact_field_verifications(tmp_path):
    collector, url, _row, _own, body, original_fetch, _stamp = seed_exact_observation(
        tmp_path, mileage=0
    )
    changed_body = body + "<!-- synthetic different envelope; owned values unchanged -->"
    current = collector._parse("autouncle", changed_body, url).listings[0]
    run = collector.db.start_run("zero_same_value_reconfirmation")
    stamp = native("13053744", "reconfirmed")[1]["original_capture_clock"]
    fetch = collector.db.save_fetch(run, "autouncle", url, url, 200, changed_body, stamp)
    listing = collector.detail_jobs[url][1]
    collector._observe(run, copy.deepcopy(current), stamp, fetch)
    assert collector.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    row = dict(collector.db.conn.execute("SELECT * FROM fetches WHERE id=?", (fetch,)).fetchone())
    assert collector._accepted_detail_capture("autouncle", url, row) is None
    before = history(collector)
    result = accepted_capture(
        collector, row, current, collector.detail_jobs[url][2], url, changed_body, NOW
    )
    assert result and result["proof_kind"] == "exact_native_field_verifications"
    assert result["fetch_id"] == fetch and fetch != original_fetch
    collector.details(collector.db.start_run("test"))
    assert collector.detail_stats["autouncle"]["cached"] == 1
    assert history(collector) == before
    assert collector.db.conn.execute("SELECT listing_id FROM observations").fetchone()[0] == listing
    collector.db.conn.close()
