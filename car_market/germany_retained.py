"""Atomic offline replay of native DE raw data, without transferring candidate identities.

replay_verified_raw(Database, candidate_path, *, parser=None) -> dict.
Both databases must already be bound to DE. The callback contract is
parser(source, body, final_url) -> Page, validating native IDs/countries
throughout the payload; the default is parse_germany_page. No network is needed.
"""

import copy
import gzip
import hashlib
import ipaddress
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .collect import Collector
from .db import now
from .germany import canonical_de_id, parse_germany_page, source_for_url
from .germany_bmw import BMW_SEED_URL, _api_query
from .identity import normalized_vin
from .provenance import timestamp

MAX_RAW_BYTES = 12_000_000
LEDGER_PREFIX = "germany_retained:v1:"


def _require(condition, reason):
    if not condition:
        raise ValueError("retained_replay:" + reason)


def _public_uri(url, source):
    try:
        _require(isinstance(url, str) and not any(ord(x) <= 32 for x in url), "unsafe_uri")
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        _require(
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 443}
            and not parsed.fragment
            and "." in host
            and not host.endswith(
                (".local", ".localhost", ".internal", ".invalid", ".test", ".example")
            ),
            "unsafe_uri",
        )
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        _require(address is None or address.is_global, "unsafe_uri")
        _require(source_for_url(url) == source, "uri_source_mismatch")
        _require(
            not any(
                re.search(r"token|api[_-]?key|password|authorization|session", key, re.IGNORECASE)
                for key in parse_qs(parsed.query)
            ),
            "credential_uri",
        )
        return parsed
    except (TypeError, ValueError) as exc:
        if str(exc).startswith("retained_replay:"):
            raise
        raise ValueError("retained_replay:unsafe_uri") from exc


def _raw_body(root, raw_path, digest):
    _require(isinstance(raw_path, str) and raw_path, "raw_missing")
    relative = Path(raw_path)
    _require(
        not relative.is_absolute() and relative.parts[0] == "raw" and ".." not in relative.parts,
        "raw_path_escape",
    )
    path = root / relative
    _require(path.resolve().is_relative_to(root) and not path.is_symlink(), "raw_path_escape")
    _require(path.is_file(), "raw_missing")
    _require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest), "invalid_sha256")
    with gzip.open(path, "rb") as stream:
        body = stream.read(MAX_RAW_BYTES + 1)
    _require(len(body) <= MAX_RAW_BYTES, "raw_size_limit")
    _require(hashlib.sha256(body).hexdigest() == digest, "raw_sha256_mismatch")
    return body.decode("utf-8")


def _binding(conn):
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_state'"
    ).fetchone():
        return None
    row = conn.execute("SELECT value FROM monitor_state WHERE key='market_scope'").fetchone()
    return json.loads(row[0]).get("country") if row else None


def _proof_key(row):
    # The same source capture in candidate copies counts as one piece of evidence.
    proof = [
        row[key]
        for key in (
            "source",
            "external_id",
            "fetch_source",
            "content_hash",
            "fetched_at",
            "requested_url",
            "final_url",
        )
    ]
    return (
        LEDGER_PREFIX + hashlib.sha256(json.dumps(proof, ensure_ascii=False).encode()).hexdigest()
    )


def _bmw_api_stock_bound(item, row, body):
    """A native stock ID does not require an invented BMW detail URI."""
    if not (
        item.source == "bmw_de"
        and item.url == BMW_SEED_URL
        and item.raw.get("detail_url_status") in {"not_observed", "unverified"}
        and item.raw.get("publication_url_verified") is False
        and item.vin is not None
        and normalized_vin(item.vin) == item.vin
        and item.raw.get("vss_id") == item.external_id
    ):
        return False
    _api_query(row["requested_url"])
    _api_query(row["final_url"])
    document = json.loads(body)
    native = document.get("bmw_native_inventory", document) if isinstance(document, dict) else {}
    hits = native.get("hits", []) if isinstance(native, dict) else []
    own = [
        hit["vehicle"]
        for hit in hits
        if isinstance(hit, dict)
        and isinstance(hit.get("vehicle"), dict)
        and str(hit["vehicle"].get("vssId", "")).lower() == item.external_id
    ]
    if len(own) != 1:
        return False
    vehicle = own[0]
    ordering = vehicle.get("ordering", {})
    countries = [
        vehicle.get("country"),
        ordering.get("retailData", {}).get("locationOutletAddress", {}).get("countryCode"),
    ]
    supplied = [str(value).upper() for value in countries if value]
    return (
        bool(supplied)
        and all(value == "DE" for value in supplied)
        and vehicle.get("documentId") == item.raw.get("native_document_id")
        and bool(vehicle.get("documentId"))
        and normalized_vin(vehicle.get("ordering", {}).get("productionData", {}).get("vin17"))
        == item.vin
    )


def replay_verified_raw(destination, candidate_path, *, parser=None):
    """Import only the latest publication observations; failure rolls back the entire batch.

    Return selected/imported/skipped_imported/skipped_excluded, run_id,
    fetches_created and publications with original dates and destination fetch IDs.
    Unverified raw data raises ValueError; VIN conflicts are not suppressed.
    Repeating creates no run/observation and changes no existing IDs/dates.
    """
    _require(_binding(destination.conn) == "DE", "destination_not_DE")
    _require(
        not destination.conn.in_transaction and not destination._in_transaction,
        "destination_transaction_active",
    )
    supplied_path = Path(candidate_path).expanduser()
    _require(not supplied_path.is_symlink(), "candidate_symlink")
    candidate = supplied_path.resolve(strict=True)
    _require(
        candidate.is_file() and not candidate.samefile(destination.path), "candidate_is_destination"
    )
    root = candidate.parent
    destination_root = destination.path.parent.resolve()
    raw_root = destination_root / "raw"
    _require(
        raw_root.is_dir() and raw_root.resolve().is_relative_to(destination_root),
        "destination_raw_path_escape",
    )
    decode = parser or parse_germany_page
    report = {
        "version": 1,
        "candidate_path": str(candidate),
        "selected": 0,
        "imported": 0,
        "skipped_imported": 0,
        "skipped_excluded": 0,
        "fetches_created": 0,
        "run_id": None,
        "publications": [],
    }
    source = sqlite3.connect(candidate.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    created_raw = set()
    try:
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        _require(_binding(source) == "DE", "candidate_not_DE")
        # Do not read details_json/vehicle_id or trust a candidate-defined VIEW.
        rows = source.execute("""SELECT l.id AS candidate_listing_id,l.source,l.external_id,l.url AS listing_url,
            o.id AS observation_id,o.observed_at,o.fetch_id AS candidate_fetch_id,
            f.source AS fetch_source,f.url AS requested_url,f.final_url,f.status,
            f.error,f.fetched_at,f.content_hash,f.raw_path
            FROM listings l LEFT JOIN observations o ON o.id=(
                SELECT n.id FROM observations n WHERE n.listing_id=l.id
                ORDER BY julianday(n.observed_at) DESC,n.id DESC LIMIT 1)
            LEFT JOIN fetches f ON f.id=o.fetch_id ORDER BY l.id""").fetchall()
        report["selected"] = len(rows)
        collector = Collector.__new__(Collector)
        collector.db, collector.country = destination, "DE"
        bodies, pages, mapped_fetches = {}, {}, {}
        with destination.transaction():
            for row in rows:
                _require(
                    row["observation_id"] is not None and row["candidate_fetch_id"] is not None,
                    "latest_fetch_missing",
                )
                _require(row["status"] == 200 and not row["error"], "fetch_not_successful")
                _require(row["fetch_source"] == row["source"], "fetch_source_mismatch")
                for uri in (row["requested_url"], row["final_url"], row["listing_url"]):
                    _public_uri(uri, row["source"])
                _require(urlsplit(row["listing_url"]).path not in {"", "/"}, "publication_root_uri")
                _require(
                    canonical_de_id(row["source"], row["listing_url"]) == row["external_id"]
                    or row["source"] == "bmw_de"
                    and row["listing_url"] == BMW_SEED_URL,
                    "candidate_publication_id_mismatch",
                )
                date = timestamp(row["fetched_at"])
                _require(
                    date is not None
                    and date <= timestamp(now())
                    and timestamp(row["observed_at"]) == date,
                    "source_date_mismatch",
                )
                fetch = row["candidate_fetch_id"]
                if fetch not in bodies:
                    bodies[fetch] = _raw_body(root, row["raw_path"], row["content_hash"])
                body = bodies[fetch]
                ledger_key = _proof_key(row)
                previous = destination.conn.execute(
                    "SELECT value FROM monitor_state WHERE key=?", (ledger_key,)
                ).fetchone()
                if previous:
                    proof = json.loads(previous[0])
                    report["skipped_imported"] += 1
                    report["publications"].append({**proof, "status": "already_imported"})
                    continue
                if fetch not in pages:
                    pages[fetch] = decode(row["source"], body, row["final_url"])
                matches = [
                    item
                    for item in pages[fetch].listings
                    if item.source == row["source"] and item.external_id == row["external_id"]
                ]
                _require(len(matches) == 1, "parsed_own_publication_missing_or_ambiguous")
                item = copy.deepcopy(matches[0])
                _public_uri(item.url, item.source)
                _require(
                    urlsplit(item.url).path not in {"", "/"}
                    and (
                        canonical_de_id(item.source, item.url) == item.external_id
                        or _bmw_api_stock_bound(item, row, body)
                    ),
                    "parsed_publication_id_mismatch",
                )
                _require(
                    item.raw.get("market_country") == "DE"
                    and bool(item.raw.get("country_evidence")),
                    "own_country_unverified_or_foreign",
                )
                _require(not item.kenteken, "registration_not_allowed")
                _require(
                    item.vin is None or normalized_vin(item.vin) == item.vin, "invalid_own_vin"
                )
                if item.scope == "excluded":
                    report["skipped_excluded"] += 1
                    continue
                # Parser-supplied provenance is not destination fetch provenance.
                item.raw.pop("field_evidence", None)
                if report["run_id"] is None:
                    report["run_id"] = destination.start_run("germany_retained_replay")
                run = report["run_id"]
                if fetch not in mapped_fetches:
                    target = raw_root / (row["content_hash"] + ".html.gz")
                    _require(
                        not target.is_symlink()
                        and target.resolve().is_relative_to(destination_root),
                        "destination_raw_path_escape",
                    )
                    if not target.exists():
                        created_raw.add(target)
                    mapped_fetches[fetch] = destination.save_fetch(
                        run,
                        row["source"],
                        row["requested_url"],
                        row["final_url"],
                        200,
                        body,
                        row["fetched_at"],
                        "retained_public_replay",
                        retain_raw=True,
                    )
                    report["fetches_created"] += 1
                mapped = mapped_fetches[fetch]
                listing = collector._observe(
                    run, item, row["fetched_at"], mapped, "germany_retained"
                )
                _require(listing is not None, "observation_rejected")
                proof = {
                    "source": item.source,
                    "external_id": item.external_id,
                    "listing_id": listing,
                    "fetch_id": mapped,
                    "run_id": run,
                    "source_date": row["fetched_at"],
                    "content_hash": row["content_hash"],
                    "candidate_listing_id": row["candidate_listing_id"],
                    "candidate_fetch_id": fetch,
                }
                destination.conn.execute(
                    "INSERT INTO monitor_state(key,value) VALUES (?,?)",
                    (ledger_key, json.dumps(proof, ensure_ascii=False, sort_keys=True)),
                )
                report["publications"].append({**proof, "status": "imported"})
                report["imported"] += 1
            if report["run_id"] is not None:
                destination.finish_run(report["run_id"], "complete", report)
        return report
    except BaseException:
        # SQL rollback does not delete a file created by save_fetch. Remove only our
        # new, unused raw files; retain existing destination captures.
        for path in created_raw:
            relative = str(path.relative_to(destination_root))
            referenced = destination.conn.execute(
                "SELECT 1 FROM fetches WHERE raw_path=? LIMIT 1", (relative,)
            ).fetchone()
            if not referenced and path.is_file() and not path.is_symlink():
                path.unlink()
        raise
    finally:
        source.close()
