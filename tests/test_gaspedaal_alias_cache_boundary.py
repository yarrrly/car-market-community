"""Per-record admission survives a shared native-derived parsed-body cache.

URI mutations and the combined body are synthetic; native source facts and
original capture clocks remain unchanged. No live incident is claimed.
"""

import copy
import json

import pytest
import test_gaspedaal_alias_policy as native

from car_market import gaspedaal_detail_policy as policy
from car_market.collect import Collector
from car_market.db import Database
from car_market.parse import json_documents, parse_page

SELF = "https://www.gaspedaal.nl/bmw/2-serie-active-tourer/218i#141377748"
ORDERS = ("subject_only", "native_first", "subject_first")


@pytest.fixture(autouse=True)
def fixed_clock_and_no_network(monkeypatch):
    native.clock_and_no_network.__wrapped__(monkeypatch)


def shared_capture(directory, subject_uri=SELF):
    records = [
        next(json_documents((native.ROOT / name).read_text()))["selectedOwnRecord"]
        for name in ("gas-139378570.html", "gas-141377748.html")
    ]
    records[1]["url"] = subject_uri
    body = (
        '<script type="application/json">'
        + json.dumps({"selectedOwnRecords": records})
        + "</script>"
    )
    capture = next(
        record["original_capture"]
        for record in native.MANIFEST["records"]
        if record["fixture"] == "gas-141377748.html"
    )
    db = Database(directory / "market.sqlite3")
    run = db.start_run(timestamp=native.CLOCK.isoformat())
    fetch = db.save_fetch(
        run,
        "gaspedaal",
        capture["url"],
        capture["final_url"],
        200,
        body,
        capture["fetched_at"],
        transport="controlled_native_projection",
    )
    slots = {}
    for item in parse_page("gaspedaal", body, capture["final_url"]).listings:
        slots[item.external_id] = (
            item,
            db.observe(run, item, capture["fetched_at"], fetch),
        )
    aw = next(
        record
        for record in native.MANIFEST["records"]
        if record["source"] == "autowereld"
    )
    meta = aw["original_capture"]
    own_body = (native.ROOT / aw["fixture"]).read_text()
    af = db.save_fetch(
        run,
        "autowereld",
        native.DIRECT,
        native.DIRECT,
        200,
        own_body,
        meta["fetched_at"],
        transport="native_projection",
    )
    db.observe(
        run,
        parse_page("autowereld", own_body, native.DIRECT).listings[0],
        meta["fetched_at"],
        af,
    )
    for mapping in native.MANIFEST["redirect_mappings"]:
        db.save_fetch(
            run,
            mapping["source"],
            mapping["url"],
            mapping["final_url"],
            mapping["status"],
            None,
            mapping["fetched_at"],
            error=mapping["error"],
        )
    native.refusal(db, run)
    worker = Collector(db, {"scope": {"country": "NL"}})
    prime, pid = slots["139378570"]
    subject, sid = slots["141377748"]
    prime_uri = prime.raw["portalen"][0]["klikUrl"]
    jobs = {
        "native": (prime_uri, ("autowereld", pid, prime)),
        "subject": (native.ALIAS, ("autowereld", sid, subject)),
    }
    return db, worker, jobs, fetch


def ordered(jobs, order):
    if order == "subject_only":
        return [jobs["subject"]]
    return (
        [jobs["native"], jobs["subject"]]
        if order == "native_first"
        else [jobs["subject"], jobs["native"]]
    )


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize(
    "uri",
    [
        "https://review.invalid/foreign-publication#141377748",
        "https://www.gaspedaal.nl.review.invalid/own#141377748",
        "ftp://www.gaspedaal.nl/own#141377748",
        "https://www.gaspedaal.nl:bad/own#141377748",
        "https://www.gaspedaal.nl/own#139378570",
        "https://www.gaspedaal.nl/own",
        "https://www.gaspedaal.nl:444/own#141377748",
        "https://review@www.gaspedaal.nl/own#141377748",
    ],
)
def test_invalid_self_uri_never_inherits_cached_record_admission(tmp_path, order, uri):
    db, worker, jobs, _fetch = shared_capture(tmp_path, uri)
    before, changes = native.history(db), db.conn.total_changes
    assert native.ALIAS not in policy.prepare(worker, ordered(jobs, order))
    assert native.history(db) == before and db.conn.total_changes == changes
    db.conn.close()


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize(
    "uri",
    [
        SELF,
        "http://www.gaspedaal.nl:80/own#141377748",
        "https://gaspedaal.nl:443/own?native=1#141377748",
    ],
)
def test_positive_same_capture_publication_is_order_independent(tmp_path, order, uri):
    db, worker, jobs, _fetch = shared_capture(tmp_path, uri)
    changes = db.conn.total_changes
    assert policy.prepare(worker, ordered(jobs, order))[native.ALIAS][1] is True
    assert db.conn.total_changes == changes
    db.conn.close()


@pytest.mark.parametrize(
    "field,uri",
    [
        ("url", "https://review.invalid/search"),
        ("url", "ftp://www.gaspedaal.nl/search"),
        ("url", "https://www.gaspedaal.nl/search#unexpected"),
        ("final_url", "https://review.invalid/search"),
        ("final_url", "ftp://www.gaspedaal.nl/search"),
    ],
)
def test_capture_uri_admission_is_checked_even_after_page_was_cached(
    tmp_path, field, uri
):
    db, worker, jobs, fetch = shared_capture(tmp_path)
    pages = {}
    assert policy._owned(worker, jobs["native"][1][2], native.CLOCK, pages) is not None
    assert len(pages) == 1
    # Deliberate disposable-row mutation tests that mutable authorization inputs
    # are re-read. Original native history/ledger is never modified.
    sql = {
        "url": "UPDATE fetches SET url=? WHERE id=?",
        "final_url": "UPDATE fetches SET final_url=? WHERE id=?",
    }
    db.conn.execute(sql[field], (uri, fetch))
    changes = db.conn.total_changes
    assert policy._owned(worker, jobs["subject"][1][2], native.CLOCK, pages) is None
    assert db.conn.total_changes == changes
    db.conn.close()


@pytest.mark.parametrize("fault", ("canonical", "country", "persisted_scope"))
def test_own_autowereld_authorization_remains_per_record_after_cache_hit(
    tmp_path, fault
):
    db, _run, items = native.seed(tmp_path)
    worker = Collector(db, {"scope": {"country": "NL"}})
    direct, listing = items["direct"]
    pages = {}
    assert policy._owned(worker, direct, native.CLOCK, pages) is not None
    changed = copy.deepcopy(direct)
    if fault == "canonical":
        changed.raw["identity_evidence"]["canonical"] = "https://review.invalid/not-own"
    elif fault == "country":
        changed.raw["country_code"] = "BE"
    else:
        db.conn.execute("UPDATE listings SET scope='excluded' WHERE id=?", (listing,))
    changes = db.conn.total_changes
    assert policy._owned(worker, changed, native.CLOCK, pages) is None
    assert db.conn.total_changes == changes
    db.conn.close()
