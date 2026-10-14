"""Real own raw data and mutations: replay, strict provenance and atomic rollback."""

import copy
import gzip
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from car_market.collect import Collector
from car_market.db import Database
from car_market.germany import parse_germany_page
from car_market.germany_discovery import parse_discovery_publisher
from car_market.germany_retained import LEDGER_PREFIX, replay_verified_raw

FIXTURES = Path(__file__).parent / "fixtures"
DISCOVERY = FIXTURES / "germany_discovery"
META = {r["fixture"]: r for r in json.loads((DISCOVERY / "manifest.json").read_text())}


def own_fixture(name="quoka-own-active.html"):
    meta = META[name]
    source = "quoka_de" if name.startswith("quoka") else "dealer:autoland.de"
    body = (DISCOVERY / name).read_text()
    item = parse_germany_page(source, body, meta["final_url"]).listings[0]
    return item, body, meta["final_url"], meta["fetched_at"]


def german_db(path):
    db = Database(path)
    # Destination binding is a caller operation, outside replay's transaction.
    Collector(db, {"scope": {"country": "DE"}})
    return db


def candidate(tmp_path, records=None):
    db = german_db(tmp_path / "candidate" / "market.sqlite3")
    run = db.start_run("test_fixture_candidate")
    for item, body, url, date in records or [own_fixture()]:
        fetch = db.save_fetch(run, item.source, url, url, 200, body, date)
        db.observe(run, copy.deepcopy(item), date, fetch)
    return db


def state(db):
    tables = [
        r[0]
        for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    return {
        "tables": {
            name: [tuple(r) for r in db.conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
            for name in tables
        },
        "raw": {p.name: p.read_bytes() for p in db.raw.iterdir()},
    }


def test_reparse_ignores_candidate_country_vin_uuid_and_maps_fresh_field_provenance(tmp_path):
    src = candidate(tmp_path)
    source_item, _, _, date = own_fixture()
    src.conn.execute(
        "UPDATE observations SET details_json=?",
        (
            json.dumps(
                {
                    "raw": {
                        "market_country": "NL",
                        "field_evidence": {"title": {"fetch_id": 999999}},
                    },
                    "vin": "WBA21BX0X07T40980",
                }
            ),
        ),
    )
    candidate_uuid = src.conn.execute("SELECT vehicle_id FROM listings").fetchone()[0]
    src.conn.commit()
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    # Candidate fetch ID must not accidentally equal destination's mapped ID.
    initial = dest.start_run("prior")
    dest.save_fetch(initial, "quoka_de", source_item.url, source_item.url, 200, "prior", date)
    report = replay_verified_raw(dest, src.path)
    assert (report["selected"], report["imported"], report["fetches_created"]) == (1, 1, 1)
    row = dest.conn.execute("SELECT * FROM latest_observations").fetchone()
    payload = json.loads(row["details_json"])
    assert payload["raw"]["market_country"] == "DE" and row["vin"] is None
    assert row["observed_at"] == date and row["fetch_id"] != 1
    evidence = payload["raw"]["field_evidence"]["title"]
    assert evidence["fetch_id"] == row["fetch_id"] and evidence["observed_at"] == date
    assert evidence["source"] == source_item.source and not evidence["inherited"]
    assert dest.conn.execute("SELECT vehicle_id FROM listings").fetchone()[0] != candidate_uuid
    verified = dest.conn.execute(
        "SELECT fetch_id,verified_at FROM field_verifications WHERE field='title'"
    ).fetchone()
    assert tuple(verified) == (row["fetch_id"], date)
    assert dest.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_repeat_preserves_all_ids_dates_observations_runs_and_raw_even_if_details_forged(tmp_path):
    src = candidate(tmp_path)
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    first = replay_verified_raw(dest, src.path)
    before = state(dest)
    src.conn.execute("UPDATE observations SET details_json='{}'")
    src.conn.commit()
    second = replay_verified_raw(dest, src.path)
    assert (
        second["imported"],
        second["skipped_imported"],
        second["fetches_created"],
        second["run_id"],
    ) == (0, 1, 0, None)
    assert second["publications"][0]["fetch_id"] == first["publications"][0]["fetch_id"]
    assert state(dest) == before


@pytest.mark.parametrize("country", ["NL", None])
def test_candidate_country_forgery_cannot_restore_foreign_or_unknown_own_raw(tmp_path, country):
    item, _, url, date = own_fixture("autoland-own.html")
    soup = BeautifulSoup((DISCOVERY / "autoland-own.html").read_text(), "html.parser")
    script = soup.select_one('script[type="application/ld+json"]')
    vehicle = json.loads(script.get_text())
    offers = vehicle["offers"]
    offers = offers if isinstance(offers, list) else [offers]
    for offer in offers:
        offer["seller"]["address"]["addressCountry"] = country
    script.string = json.dumps(vehicle)
    src = candidate(tmp_path, [(item, str(soup), url, date)])
    assert (
        json.loads(src.conn.execute("SELECT details_json FROM observations").fetchone()[0])["raw"][
            "market_country"
        ]
        == "DE"
    )
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    before = state(dest)
    with pytest.raises(ValueError, match="retained_replay:"):
        replay_verified_raw(dest, src.path)
    assert state(dest) == before


@pytest.mark.parametrize(
    "problem",
    [
        "hash",
        "absolute",
        "parent",
        "symlink_file",
        "symlink_directory",
        "status",
        "fetch_source",
        "request_source",
        "request_credentials",
        "request_private",
        "final_source",
        "listing_id",
        "date",
        "missing_fetch",
    ],
)
def test_retained_source_hash_uri_path_and_date_boundaries_are_fail_closed(tmp_path, problem):
    src = candidate(tmp_path)
    row = src.conn.execute("SELECT * FROM fetches").fetchone()
    raw = src.path.parent / row["raw_path"]
    if problem == "hash":
        raw.write_bytes(gzip.compress(b"changed body"))
    elif problem == "absolute":
        src.conn.execute("UPDATE fetches SET raw_path=?", (str(raw),))
    elif problem == "parent":
        src.conn.execute("UPDATE fetches SET raw_path='raw/../../outside.html.gz'")
    elif problem in {"symlink_file", "symlink_directory"}:
        outside = tmp_path / "outside"
        outside.mkdir()
        if problem == "symlink_file":
            target = outside / raw.name
            raw.rename(target)
            raw.symlink_to(target)
        else:
            for path in src.raw.iterdir():
                path.rename(outside / path.name)
            src.raw.rmdir()
            src.raw.symlink_to(outside, target_is_directory=True)
    elif problem == "status":
        src.conn.execute("UPDATE fetches SET status=429")
    elif problem == "fetch_source":
        src.conn.execute("UPDATE fetches SET source='gebrauchtwagen_de'")
    elif problem == "request_source":
        src.conn.execute(
            "UPDATE fetches SET url='https://www.12gebrauchtwagen.de/c/partner?offer_id=123'"
        )
    elif problem == "request_credentials":
        src.conn.execute("UPDATE fetches SET url='https://user:password@www.quoka.de/'")
    elif problem == "request_private":
        src.conn.execute("UPDATE fetches SET url='https://127.0.0.1/'")
    elif problem == "final_source":
        src.conn.execute("UPDATE fetches SET final_url='https://example.org/'")
    elif problem == "listing_id":
        src.conn.execute("UPDATE listings SET external_id='different-id'")
    elif problem == "date":
        src.conn.execute("UPDATE observations SET observed_at='2026-10-08T01:00:00+00:00'")
    else:
        src.conn.execute("UPDATE observations SET fetch_id=NULL")
    src.conn.commit()
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    before = state(dest)
    with pytest.raises(ValueError, match="retained_replay:"):
        replay_verified_raw(dest, src.path)
    assert state(dest) == before


def test_latest_only_does_not_resurrect_an_older_valid_fetch(tmp_path):
    src = candidate(tmp_path)
    item, _, url, _ = own_fixture()
    date = "2026-10-08T05:20:00+00:00"
    run = src.start_run("newer")
    fetch = src.save_fetch(run, item.source, url, url, 410, None, date)
    src.observe(run, copy.deepcopy(item), date, fetch, preserve_observation=True)
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    with pytest.raises(ValueError, match="fetch_not_successful"):
        replay_verified_raw(dest, src.path)
    assert dest.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 0


def test_real_multi_item_inventory_reparses_whole_payload_and_maps_one_fetch(tmp_path):
    fixtures = FIXTURES / "germany_dealer_inventory"
    body = (fixtures / "autoland-p1.html").read_text()
    url, source, date = (
        "https://autoland.de/bmw/2er-reihe",
        "dealer:autoland.de",
        "2026-10-08T05:02:14.898303+00:00",
    )
    items = [x for x in parse_germany_page(source, body, url).listings if x.scope == "target"][:2]
    assert len(items) == 2
    src = german_db(tmp_path / "candidate" / "market.sqlite3")
    run = src.start_run("inventory")
    fetch = src.save_fetch(run, source, url, url, 200, body, date)
    for item in items:
        src.observe(run, copy.deepcopy(item), date, fetch)
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    calls = []

    def parser(source, body, final_url):
        calls.append(final_url)
        return parse_germany_page(source, body, final_url)

    report = replay_verified_raw(dest, src.path, parser=parser)
    assert (report["imported"], report["fetches_created"]) == (2, 1)
    assert calls == [url]
    assert dest.conn.execute("SELECT count(DISTINCT fetch_id) FROM observations").fetchone()[0] == 1
    assert {r[0] for r in dest.conn.execute("SELECT external_id FROM listings")} == {
        x.external_id for x in items
    }


def test_unknown_search_country_cannot_be_written_even_with_matching_candidate_id(tmp_path):
    meta = META["quoka-search.html"]
    body, url = (DISCOVERY / "quoka-search.html").read_text(), meta["final_url"]
    item = parse_germany_page("quoka_de", body, url).listings[0]
    item.raw["market_country"] = "DE"  # Deliberately forged candidate proof.
    src = candidate(tmp_path, [(item, body, url, meta["fetched_at"])])
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    with pytest.raises(ValueError, match="own_country_unverified_or_foreign"):
        replay_verified_raw(dest, src.path)
    assert dest.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 0


def weller_record():
    meta = META["weller-own.html"]
    body = (DISCOVERY / "weller-own.html").read_text()
    item = parse_discovery_publisher("dealer:wellergruppe.de", body, meta["final_url"]).listings[0]
    return item, body, meta["final_url"], meta["fetched_at"]


def test_exact_destination_vin_and_existing_publication_preserve_destination_ids(tmp_path):
    record = weller_record()
    src = candidate(tmp_path, [record])
    item, _, _, _ = record
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    run = dest.start_run("established")
    previous = copy.deepcopy(item)
    dest.observe(run, previous, "2026-10-07T01:00:00+00:00")
    before = dest.conn.execute("SELECT id,vehicle_id FROM listings").fetchone()
    report = replay_verified_raw(dest, src.path, parser=parse_discovery_publisher)
    assert report["imported"] == 1
    assert tuple(dest.conn.execute("SELECT id,vehicle_id FROM listings").fetchone()) == tuple(
        before
    )
    assert dest.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 1


def test_conflicting_vin_rolls_back_prior_publication_fetch_run_ledger_and_raw(tmp_path):
    record = weller_record()
    src = candidate(tmp_path, [own_fixture(), record])
    item, _, _, _ = record
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    run = dest.start_run("established")
    conflicting = copy.deepcopy(item)
    conflicting.vin = "WBA21BX0X07T40980"
    dest.observe(run, conflicting, "2026-10-07T01:00:00+00:00")
    before = state(dest)
    with pytest.raises(ValueError, match="Conflicting registration/VIN"):
        replay_verified_raw(
            dest,
            src.path,
            parser=lambda source, body, url: (
                parse_germany_page(source, body, url)
                if source == "quoka_de"
                else parse_discovery_publisher(source, body, url)
            ),
        )
    assert state(dest) == before
    assert (
        dest.conn.execute(
            "SELECT count(*) FROM monitor_state WHERE key LIKE ?", (LEDGER_PREFIX + "%",)
        ).fetchone()[0]
        == 0
    )


def test_parser_failure_after_one_write_rolls_back_whole_batch(tmp_path):
    src = candidate(tmp_path, [own_fixture(), own_fixture("quoka-own-encoded-title.html")])
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    before = state(dest)
    calls = []

    def parser(source, body, url):
        calls.append(url)
        if len(calls) == 2:
            raise RuntimeError("fixture parser failure")
        return parse_germany_page(source, body, url)

    with pytest.raises(RuntimeError, match="fixture parser failure"):
        replay_verified_raw(dest, src.path, parser=parser)
    assert len(calls) == 2 and state(dest) == before


def test_nl_binding_is_rejected_without_changing_either_database(tmp_path):
    src = candidate(tmp_path)
    dest = Database(tmp_path / "nl" / "market.sqlite3")
    before = state(dest)
    with pytest.raises(ValueError, match="destination_not_DE"):
        replay_verified_raw(dest, src.path)
    assert state(dest) == before


def test_single_transaction_no_network_and_callback_provenance_is_remapped(tmp_path, monkeypatch):
    from car_market.fetch import Fetcher

    src = candidate(tmp_path)
    dest = german_db(tmp_path / "destination" / "market.sqlite3")

    def forbidden(*args, **kwargs):
        pytest.fail("Replay must not instantiate a network fetcher")

    monkeypatch.setattr(Fetcher, "__init__", forbidden)
    trace = []
    dest.conn.set_trace_callback(trace.append)

    def parser(source, body, url):
        page = parse_germany_page(source, body, url)
        page.listings[0].raw["field_evidence"] = {
            "title": {
                "fetch_id": 777777,
                "observed_at": "2020-01-01T00:00:00+00:00",
                "source": "foreign",
            }
        }
        return page

    report = replay_verified_raw(dest, src.path, parser=parser)
    assert sum(x == "BEGIN IMMEDIATE" for x in trace) == 1
    assert sum(x == "COMMIT" for x in trace) == 1
    assert not any(x.startswith("ROLLBACK") for x in trace)
    payload = json.loads(dest.conn.execute("SELECT details_json FROM observations").fetchone()[0])
    origin = payload["raw"]["field_evidence"]["title"]
    assert origin["fetch_id"] == report["publications"][0]["fetch_id"]
    assert origin["source"] == "quoka_de" and origin["observed_at"] == own_fixture()[3]


@pytest.mark.parametrize("problem", ["plate", "vin", "country", "proof", "parsed_id", "duplicate"])
def test_callback_item_still_requires_own_country_publication_identity_and_no_plate(
    tmp_path, problem
):
    src = candidate(tmp_path)
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    before = state(dest)

    def parser(source, body, url):
        page = parse_germany_page(source, body, url)
        own = page.listings[0]
        if problem == "plate":
            own.kenteken = "AB1234"
        elif problem == "vin":
            own.vin = "UUID-OR-TRUNCATED"
        elif problem == "country":
            own.raw["market_country"] = "NL"
        elif problem == "proof":
            own.raw.pop("country_evidence")
        elif problem == "parsed_id":
            own.external_id = "different"
        else:
            page.listings.append(copy.deepcopy(own))
        return page

    with pytest.raises(ValueError, match="retained_replay:"):
        replay_verified_raw(dest, src.path, parser=parser)
    assert state(dest) == before


def test_new_publication_with_exact_own_vin_joins_established_destination_vehicle(tmp_path):
    record = weller_record()
    src = candidate(tmp_path, [record])
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    prior = copy.deepcopy(record[0])
    prior.source = "dealer:another-publication.de"
    prior.url = prior.external_id = "https://another-publication.de/vehicle/own-stock-1"
    dest.observe(dest.start_run("prior_same_vin"), prior, "2026-10-07T01:00:00+00:00")
    established = dest.conn.execute("SELECT vehicle_id FROM listings").fetchone()[0]
    replay_verified_raw(dest, src.path, parser=parse_discovery_publisher)
    assert {r[0] for r in dest.conn.execute("SELECT vehicle_id FROM listings")} == {established}
    assert dest.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 1


def test_repeat_still_checks_retained_raw_sha_before_skipping_ledger(tmp_path):
    src = candidate(tmp_path)
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    replay_verified_raw(dest, src.path)
    before = state(dest)
    raw_path = src.conn.execute("SELECT raw_path FROM fetches").fetchone()[0]
    (src.path.parent / raw_path).write_bytes(gzip.compress(b"replaced after first replay"))
    with pytest.raises(ValueError, match="raw_sha256_mismatch"):
        replay_verified_raw(dest, src.path)
    assert state(dest) == before


def test_candidate_market_binding_is_required(tmp_path):
    src = candidate(tmp_path)
    src.conn.execute(
        "UPDATE monitor_state SET value='{\"country\":\"NL\"}' WHERE key='market_scope'"
    )
    src.conn.commit()
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    before = state(dest)
    with pytest.raises(ValueError, match="candidate_not_DE"):
        replay_verified_raw(dest, src.path)
    assert state(dest) == before


def bmw_records():
    folder = FIXTURES / "germany_bmw"
    meta = next(
        x
        for x in json.loads((folder / "manifest.json").read_text())
        if x["fixture"] == "active-tourer-inventory.json"
    )
    body = (folder / meta["fixture"]).read_text()
    items = [
        x
        for x in parse_germany_page("bmw_de", body, meta["final_url"]).listings
        if x.scope == "target" and x.vin
    ][:2]
    assert len(items) == 2
    return items, body, meta["final_url"], meta["fetched_at"]


def test_bmw_own_api_stock_ids_replay_with_unverified_seed_and_original_dates(tmp_path):
    from car_market.germany_bmw import BMW_SEED_URL

    items, body, url, date = bmw_records()
    src = german_db(tmp_path / "candidate" / "market.sqlite3")
    run = src.start_run("native_bmw_inventory")
    fetch = src.save_fetch(run, "bmw_de", url, url, 200, body, date)
    for item in items:
        assert item.url == BMW_SEED_URL and item.raw["publication_url_verified"] is False
        src.observe(run, copy.deepcopy(item), date, fetch)
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    report = replay_verified_raw(dest, src.path, parser=parse_germany_page)
    assert (report["imported"], report["fetches_created"]) == (2, 1)
    before = state(dest)
    for row in dest.conn.execute("SELECT details_json,observed_at FROM observations"):
        parsed = json.loads(row[0])
        assert (
            parsed["url"] == BMW_SEED_URL and parsed["raw"]["detail_url_status"] == "not_observed"
        )
        assert parsed["raw"]["publication_url_verified"] is False
        assert row[1] == date and parsed["vin"] in {x.vin for x in items}
    repeat = replay_verified_raw(dest, src.path)
    assert repeat["skipped_imported"] == 2 and repeat["imported"] == 0 and state(dest) == before


@pytest.mark.parametrize(
    "problem", ["vin_missing", "vss_id", "document_id", "namespace", "seed_forgery", "foreign"]
)
def test_bmw_seed_exception_cannot_borrow_native_id_vin_or_api_namespace(tmp_path, problem):
    from car_market.germany_bmw import BMW_SEED_URL

    items, body, url, date = bmw_records()
    src = candidate(tmp_path, [(items[0], body, url, date)])
    if problem == "namespace":
        src.conn.execute("UPDATE fetches SET url=?,final_url=?", (BMW_SEED_URL, BMW_SEED_URL))
        src.conn.commit()
    dest = german_db(tmp_path / "destination" / "market.sqlite3")
    before = state(dest)

    def parser(source, body, final):
        page = parse_germany_page(source, body, url)
        own = next(x for x in page.listings if x.external_id == items[0].external_id)
        if problem == "vin_missing":
            own.vin = None
        elif problem == "vss_id":
            own.raw["vss_id"] = "00000000-0000-4000-8000-000000000000"
        elif problem == "document_id":
            own.raw["native_document_id"] = "unbound-document"
        elif problem == "seed_forgery":
            own.url = BMW_SEED_URL + "/results"
        elif problem == "foreign":
            own.raw["market_country"] = "NL"
        return page

    with pytest.raises(ValueError):
        replay_verified_raw(dest, src.path, parser=parser)
    assert state(dest) == before
