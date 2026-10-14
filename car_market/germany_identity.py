"""Link DE publications using two retained source responses, without fuzzy matching."""

import gzip
import hashlib
import json
import pathlib
import re
import sqlite3
import uuid
import zlib
from contextlib import contextmanager
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .db import now
from .identity import normalized_vin
from .parse import fuel, gearbox
from .provenance import timestamp


@contextmanager
def _atomic(conn):
    """Preserve the caller transaction; roll back a failed root transfer."""
    nested = conn.in_transaction
    name = "de_publication_" + uuid.uuid4().hex
    conn.execute("SAVEPOINT " + name if nested else "BEGIN IMMEDIATE")
    try:
        yield
        if nested:
            conn.execute("RELEASE SAVEPOINT " + name)
        else:
            conn.commit()
    except BaseException:
        if nested:
            conn.execute("ROLLBACK TO SAVEPOINT " + name)
            conn.execute("RELEASE SAVEPOINT " + name)
        else:
            conn.rollback()
        raise


def _uri(source, uri, external_id, api):
    if not isinstance(source, str) or not (source.endswith("_de") or source.startswith("dealer:")):
        raise ValueError("non_DE_source")
    if not isinstance(uri, str) or not isinstance(external_id, str) or not external_id:
        raise ValueError("invalid_publication_identity")
    parsed = urlsplit(uri)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.path in {"", "/"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443 if parsed.scheme == "https" else 80)
        or parsed.fragment
        or api.source_for_url(uri) != source
        or api.canonical_de_id(source, uri) != external_id
    ):
        raise ValueError("publication_URI_source_or_ID_mismatch")


def _reference(raw, api):
    reference = raw.get("publication_reference") if isinstance(raw, dict) else None
    if not isinstance(reference, dict) or reference.get("kind") != "explicit_own_publication_link":
        raise ValueError("missing_explicit_own_publication_reference")
    fields = {key: reference.get(key) for key in ("source", "url", "external_id", "kind")}
    _uri(fields["source"], fields["url"], fields["external_id"], api)
    return fields


class _HeadCanonical(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_head = False
        self.uris = []

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.in_head = True
        if self.in_head and tag == "link":
            attrs = dict(attrs)
            if "canonical" in (attrs.get("rel") or "").lower().split():
                self.uris.append(attrs.get("href"))

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False


def _own(db, listing, api, *, fresh_clock=None):
    """Only the latest observation fetch proves the current reference."""
    conn = db.conn
    row = conn.execute(
        "SELECT o.details_json,o.fetch_id,o.observed_at,f.* FROM latest_observations o "
        "LEFT JOIN fetches f ON f.id=o.fetch_id WHERE o.listing_id=?",
        (listing["id"],),
    ).fetchone()
    if row is None or row["status"] != 200 or row["error"]:
        raise ValueError("missing_successful_own_fetch")
    if fresh_clock is not None:
        if listing["availability"] != "observed":
            raise ValueError("AS_stock_publication_not_active")
        cutoff = fresh_clock - timedelta(hours=13)
        for field, value in (
            ("fetched_at", row["fetched_at"]),
            ("observed_at", row["observed_at"]),
            ("last_seen", listing["last_seen"]),
        ):
            instant = timestamp(value)
            if instant is None or not cutoff <= instant <= fresh_clock:
                raise ValueError("AS_stock_not_current_13h:" + field)
    source, identity = listing["source"], listing["external_id"]
    for uri in (listing["url"], row["url"], row["final_url"]):
        _uri(source, uri, identity, api)
    if row["source"] != source:
        raise ValueError("own_fetch_source_mismatch")
    if not isinstance(row["raw_path"], str):
        raise TypeError("missing_retained_raw")
    relative = pathlib.Path(row["raw_path"])
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("raw",):
        raise ValueError("unsafe_raw_path")
    try:
        root = pathlib.Path(db.raw).resolve()
        file = root.joinpath(*relative.parts[1:]).resolve()
    except (OSError, RuntimeError) as error:
        raise ValueError("raw_path_escape_or_missing") from error
    if (
        not root.is_relative_to(pathlib.Path(db.path).parent.resolve())
        or not file.is_relative_to(root)
        or not file.is_file()
    ):
        raise ValueError("raw_path_escape_or_missing")
    try:
        body = gzip.decompress(file.read_bytes())
        if hashlib.sha256(body).hexdigest() != row["content_hash"]:
            raise ValueError("retained_raw_hash_mismatch")
        text = body.decode("utf-8")
    except (OSError, EOFError, UnicodeError, zlib.error) as error:
        raise ValueError("unreadable_retained_raw") from error
    head = _HeadCanonical()
    head.feed(text)
    if len(head.uris) > 1:
        raise ValueError("ambiguous_head_canonical")
    for uri in head.uris:
        if not uri:
            raise ValueError("empty_head_canonical")
        _uri(source, urljoin(row["final_url"], uri), identity, api)
    page = api.parse_germany_page(source, text, row["final_url"])
    if len(page.listings) != 1 or page.total not in (None, 1):
        raise ValueError("not_exactly_one_own_publication")
    item = page.listings[0]
    if (item.source, item.external_id) != (source, identity):
        raise ValueError("own_payload_registry_ID_mismatch")
    _uri(source, item.url, identity, api)
    if not isinstance(item.raw, dict) or item.raw.get("market_country") != "DE":
        raise ValueError("own_payload_country_not_DE")
    country_filter = page.search_filters.get("country")
    if country_filter is not None and country_filter not in {"DE", "D"}:
        raise ValueError("own_payload_country_filter_conflict")
    if item.kenteken is not None:
        raise ValueError("DE_payload_has_kenteken")
    if item.scope not in {"target", "needs_confirmation", "model_conflict"}:
        raise ValueError("own_payload_model_out_of_scope")
    if item.vin is not None and normalized_vin(item.vin) != item.vin:
        raise ValueError("invalid_own_VIN")
    evidence = {
        "listing_id": listing["id"],
        "source": source,
        "external_id": identity,
        "fetch_id": row["fetch_id"],
        "status": row["status"],
        "requested_url": row["url"],
        "final_url": row["final_url"],
        "fetched_at": row["fetched_at"],
        "observed_at": row["observed_at"],
        "last_seen": listing["last_seen"],
        "availability": listing["availability"],
        "raw_path": row["raw_path"],
        "content_hash": row["content_hash"],
        "canonical_uri": head.uris[0] if head.uris else None,
        "market_country": "DE",
        "scope": item.scope,
        "vin": item.vin,
    }
    return item, evidence


def _root(conn, identity):
    row = conn.execute("SELECT * FROM vehicles WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise ValueError("missing_vehicle_root")
    if conn.execute("SELECT 1 FROM vehicle_aliases WHERE alias_id=?", (identity,)).fetchone():
        raise ValueError("conflicting_root_alias")
    return row


def _move(db, a, b, own_a, own_b, proof, api):
    """Transfer registry links without adding facts to observations."""
    conn = db.conn
    roots = [_root(conn, listing["vehicle_id"]) for listing in (a, b)]
    for root in roots:
        if root["kenteken"] is not None:
            raise ValueError("DE_root_has_kenteken")
        if root["vin"] is not None and normalized_vin(root["vin"]) != root["vin"]:
            raise ValueError("invalid_registry_VIN")
    vins = {value for value in [*(root["vin"] for root in roots), own_a.vin, own_b.vin] if value}
    if len(vins) > 1:
        raise ValueError("conflicting_VIN_identities")
    members = [
        dict(row)
        for row in conn.execute(
            "SELECT l.*,o.details_json FROM listings l LEFT JOIN latest_observations o "
            "ON o.listing_id=l.id WHERE l.vehicle_id IN (?,?)",
            (roots[0]["id"], roots[1]["id"]),
        )
    ]
    for member in members:
        raw = json.loads(member["details_json"] or "{}").get("raw", {})
        _uri(member["source"], member["url"], member["external_id"], api)
        if not isinstance(raw, dict) or raw.get("market_country") != "DE":
            raise ValueError("root_member_country_not_DE")
    if roots[0]["id"] == roots[1]["id"]:
        return False
    established = [root for root in roots if root["vin"]]
    winner = min(
        established or roots,
        key=lambda root: (
            timestamp(root["created_at"]) is None,
            timestamp(root["created_at"]),
            root["id"],
        ),
    )
    loser = next(root for root in roots if root["id"] != winner["id"])
    winner_id, loser_id = winner["id"], loser["id"]
    candidates = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM identity_candidates WHERE vehicle_a=? OR vehicle_b=?",
            (loser_id, loser_id),
        )
    ]
    quarantine = [
        dict(row)
        for row in conn.execute("SELECT * FROM identity_quarantine WHERE vehicle_id=?", (loser_id,))
    ]
    audit = {
        **proof,
        "winner_at_merge": winner_id,
        "retired_vehicle": dict(loser),
        "members": [
            {key: value for key, value in row.items() if key != "details_json"} for row in members
        ],
        "previous_candidates": candidates,
        "previous_quarantine": quarantine,
    }
    for table in ("listings", "vehicle_checks", "vehicle_aliases"):
        conn.execute(f"UPDATE {table} SET vehicle_id=? WHERE vehicle_id=?", (winner_id, loser_id))
    conn.execute(
        "INSERT OR IGNORE INTO identity_quarantine SELECT ?,field,value,quarantined_at,reason,evidence_json "
        "FROM identity_quarantine WHERE vehicle_id=?",
        (winner_id, loser_id),
    )
    conn.execute("DELETE FROM identity_quarantine WHERE vehicle_id=?", (loser_id,))
    for candidate in candidates:
        other = (
            candidate["vehicle_b"] if candidate["vehicle_a"] == loser_id else candidate["vehicle_a"]
        )
        if other != winner_id:
            first, second = sorted((winner_id, other))
            conn.execute(
                "INSERT OR IGNORE INTO identity_candidates VALUES (?,?,?,?,?)",
                (first, second, candidate["reason"], candidate["created_at"], candidate["status"]),
            )
    conn.execute(
        "DELETE FROM identity_candidates WHERE vehicle_a=? OR vehicle_b=?", (loser_id, loser_id)
    )
    conn.execute("DELETE FROM vehicles WHERE id=?", (loser_id,))
    conn.execute(
        "INSERT INTO vehicle_aliases VALUES (?,?,?,?,?)",
        (
            loser_id,
            winner_id,
            now(),
            "exact_own_AS_dealer_stock"
            if proof["kind"] == "exact_own_AS_dealer_stock"
            else "explicit_DE_publication_reference",
            json.dumps(audit, ensure_ascii=False),
        ),
    )
    if any(member["scope"] == "model_conflict" for member in members) or any(
        item.scope == "model_conflict" for item in (own_a, own_b)
    ):
        conn.execute("UPDATE listings SET scope='model_conflict' WHERE vehicle_id=?", (winner_id,))
    return True


def _as_stock_fields(item):
    """Key only from a typed native normal detail or transformed SMYLE detail."""
    raw = item.raw
    origin = raw.get("origin", {}).get("own_stock", {})
    if (
        not isinstance(origin, dict)
        or origin.get("kind") != "publisher_listing"
        or origin.get("external_id") != item.external_id
    ):
        raise ValueError("AS_stock_missing_own_detail_origin")
    if urlsplit(item.url).path.startswith("/smyle/details/"):
        own = raw.get("own_smyle_record")
        if (
            not isinstance(own, dict)
            or own.get("id") != item.external_id
            or origin.get("envelope") != "properData.carDetails"
            or origin.get("path") != "pageProps.properData.carDetails"
        ):
            raise ValueError("AS_stock_not_own_SMYLE_detail")
        ocs = own.get("ocsInfo", {})
        if not isinstance(ocs, dict) or ocs.get("status") != "ACTIVE":
            raise ValueError("AS_stock_SMYLE_not_active")
        seller, identifier, location = ocs.get("seller"), own.get("identifier"), ocs.get("location")
        path = "pageProps.properData.carDetails"
        paths = {
            "seller_id": path + ".ocsInfo.seller.id",
            "stock": path + ".identifier.offerReference",
            "location": path + ".ocsInfo.location",
        }
        if any(
            raw.get(field) != value
            for field, value in (
                ("seller", seller),
                ("identifier", identifier),
                ("location", location),
            )
        ):
            raise ValueError("AS_stock_SMYLE_transformation_mismatch")
        variant = "smyle"
    else:
        if (
            raw.get("id") != item.external_id
            or raw.get("status") != "Active"
            or origin.get("path") != "pageProps.listingDetails"
            or origin.get("envelope") not in {"__NEXT_DATA__", "next_json"}
        ):
            raise ValueError("AS_stock_not_own_normal_active_detail")
        seller, identifier, location = raw.get("seller"), raw.get("identifier"), raw.get("location")
        path = "pageProps.listingDetails"
        paths = {
            "seller_id": path + ".seller.id",
            "stock": path + ".identifier.offerReference",
            "location": path + ".location",
        }
        variant = "normal"
    if not all(isinstance(value, dict) for value in (seller, identifier, location)):
        raise ValueError("AS_stock_missing_typed_seller_identifier_location")
    if seller.get("type") != "Dealer" or seller.get("isDealer") is False:
        raise ValueError("AS_stock_seller_not_a_dealer")
    account = seller.get("id")
    if isinstance(account, int) and not isinstance(account, bool) and account > 0:
        account = str(account)
    if not isinstance(account, str) or not re.fullmatch(r"[1-9]\d*", account):
        raise ValueError("AS_stock_invalid_account_ID")
    stock = identifier.get("offerReference")
    if not isinstance(stock, str) or not stock or stock.strip() != stock:
        raise ValueError("AS_stock_missing_exact_offerReference")
    address = {field: location.get(field) for field in ("countryCode", "zip", "street", "city")}
    if (
        address["countryCode"] != "DE"
        or not isinstance(address["zip"], str)
        or not re.fullmatch(r"\d{5}", address["zip"])
    ):
        raise ValueError("AS_stock_missing_full_DE_location")
    vehicle = own.get("vehicle", {}) if variant == "smyle" else raw.get("vehicle", {})
    if not isinstance(vehicle, dict):
        raise TypeError("AS_stock_missing_own_vehicle")
    declared_fuels = [
        vehicle.get("fuel"),
        *[
            value.get("formatted")
            for value in (vehicle.get("primaryFuel"), vehicle.get("fuelCategory"))
            if isinstance(value, dict)
        ],
    ]
    known_fuels = {value for term in declared_fuels if (value := fuel(term)) is not None}
    known_gears = {
        value
        for term in (vehicle.get("transmission"), vehicle.get("transmissionType"))
        if (value := gearbox(term)) is not None
    }
    if known_fuels and known_fuels != {item.fuel}:
        raise ValueError("AS_stock_own_field_conflict:fuel")
    if known_gears and known_gears != {item.transmission}:
        raise ValueError("AS_stock_own_field_conflict:transmission")
    if any(
        not isinstance(address[field], str) or not address[field].strip()
        for field in ("street", "city")
    ) or not re.search(r"\d", address["street"]):
        raise ValueError("AS_stock_missing_full_DE_location")
    return {
        "account_id": account,
        "stock_reference": stock,
        "location": address,
        "variant": variant,
        "field_paths": paths,
    }


def _bridge_as_stock(db, api, result, seen):
    clock = timestamp(now())
    if clock is None:
        raise ValueError("invalid_AS_stock_bridge_clock")
    groups = {}
    rows = db.conn.execute(
        "SELECT l.*,o.details_json FROM listings l JOIN latest_observations o ON o.listing_id=l.id "
        "WHERE l.source='autoscout24_de' ORDER BY l.id"
    ).fetchall()
    for row in rows:
        try:
            detail = json.loads(row["details_json"])
            raw = detail.get("raw", {})
            seller, identifier = raw.get("seller", {}), raw.get("identifier", {})
            account = seller.get("id") if isinstance(seller, dict) else None
            stock = identifier.get("offerReference") if isinstance(identifier, dict) else None
            if isinstance(account, int) and not isinstance(account, bool) and account > 0:
                account = str(account)
            if (
                isinstance(account, str)
                and re.fullmatch(r"[1-9]\d*", account)
                and isinstance(stock, str)
                and stock
                and stock.strip() == stock
            ):
                groups.setdefault((account, stock), []).append(row["id"])
        except (ValueError, TypeError, AttributeError):
            continue
    for (account, stock), members in groups.items():
        if len(members) < 2:
            continue
        pair = tuple(members[:2])
        if len(members) == 2 and pair in seen:
            continue
        proof = {
            "kind": "exact_own_AS_dealer_stock",
            "source": "autoscout24_de",
            "account_id": account,
            "stock_reference": stock,
            "listing_ids": members,
            "as_of": clock.isoformat(),
            "freshness_hours": 13,
        }
        try:
            with _atomic(db.conn):
                if len(members) != 2:
                    raise ValueError("ambiguous_AS_dealer_stock_publications")
                a, b = [
                    db.conn.execute("SELECT * FROM listings WHERE id=?", (identity,)).fetchone()
                    for identity in members
                ]
                own_a, evidence_a = _own(db, a, api, fresh_clock=clock)
                own_b, evidence_b = _own(db, b, api, fresh_clock=clock)
                fields_a, fields_b = _as_stock_fields(own_a), _as_stock_fields(own_b)
                proof.update(a={**evidence_a, **fields_a}, b={**evidence_b, **fields_b})
                if any(
                    (fields["account_id"], fields["stock_reference"]) != (account, stock)
                    for fields in (fields_a, fields_b)
                ):
                    raise ValueError("AS_stock_stored_key_not_proven_by_own_payload")
                if fields_a["location"] != fields_b["location"]:
                    raise ValueError("AS_stock_branch_location_conflict")
                proof["location"] = fields_a["location"]
                if own_a.scope != "target" or own_b.scope != "target":
                    raise ValueError("AS_stock_model_scope_not_confirmed")
                if db.conn.execute(
                    "SELECT 1 FROM listings WHERE vehicle_id IN (?,?) AND scope='model_conflict' LIMIT 1",
                    (a["vehicle_id"], b["vehicle_id"]),
                ).fetchone():
                    raise ValueError("AS_stock_existing_model_conflict")
                common = {}
                for field in ("year", "fuel", "transmission"):
                    first, second = getattr(own_a, field), getattr(own_b, field)
                    valid = (
                        (
                            isinstance(first, int)
                            and not isinstance(first, bool)
                            and 1886 <= first <= 2100
                        )
                        if field == "year"
                        else first in ({"petrol"} if field == "fuel" else {"manual", "automatic"})
                    )
                    if not valid or first != second:
                        raise ValueError("AS_stock_common_field_conflict:" + field)
                    if field in own_a.raw.get("field_conflicts", {}) or field in own_b.raw.get(
                        "field_conflicts", {}
                    ):
                        raise ValueError("AS_stock_own_field_conflict:" + field)
                    common[field] = first
                proof["common_fields"] = {**common, "scope": "target"}
                result["verified_pairs"] += 1
                seen.add(pair)
                result["merged"] += int(_move(db, a, b, own_a, own_b, proof, api))
        except (ValueError, TypeError, AttributeError, sqlite3.IntegrityError) as error:
            result["conflicts"].append(
                {"listing_a": pair[0], "listing_b": pair[1], "error": str(error), "evidence": proof}
            )


def bridge_de_publications(db):
    """Return counts and rejected pairs while preserving the caller transaction.

    Retained raw.publication_reference selects candidates only. Reparse both
    publication details inside the write transaction.
    Verify already linked publications without counting a new merge.
    A parser reference to the same publication URI is not a cross-platform pair.
    AS normal/SMYLE are also linked by exact native dealer account/stock,
    full DE address and shared year/fuel/transmission/target scope. Both
    detail fetches and current observations must be active and no older than 13h.
    Result: integer verified_pairs/merged and a conflicts list with evidence.
    """
    from . import germany as api

    result = {"verified_pairs": 0, "merged": 0, "conflicts": []}
    with _atomic(db.conn):
        candidates = db.conn.execute(
            "SELECT l.id,o.details_json FROM listings l JOIN latest_observations o ON o.listing_id=l.id "
            "WHERE substr(l.source,-3)='_de' OR l.source LIKE 'dealer:%' ORDER BY l.id"
        ).fetchall()
        seen = set()
        for candidate in candidates:
            target = None
            proof = None
            try:
                with _atomic(db.conn):
                    stored = json.loads(candidate["details_json"])
                    raw = stored.get("raw") if isinstance(stored, dict) else None
                    if not isinstance(raw, dict) or "publication_reference" not in raw:
                        continue
                    reference = _reference(raw, api)
                    a = db.conn.execute(
                        "SELECT * FROM listings WHERE id=?", (candidate["id"],)
                    ).fetchone()
                    target = db.conn.execute(
                        "SELECT * FROM listings WHERE source=? AND external_id=?",
                        (reference["source"], reference["external_id"]),
                    ).fetchone()
                    if target is None:
                        raise ValueError("referenced_publication_not_in_registry")
                    if a["id"] == target["id"]:
                        continue
                    key = tuple(sorted((a["id"], target["id"])))
                    if key in seen:
                        continue
                    own_a, evidence_a = _own(db, a, api)
                    own_b, evidence_b = _own(db, target, api)
                    if _reference(own_a.raw, api) != reference:
                        raise ValueError("stored_reference_not_proven_by_own_payload")
                    proof = {
                        "kind": "explicit_own_DE_publication_link",
                        "reference": reference,
                        "a": evidence_a,
                        "b": evidence_b,
                    }
                    result["verified_pairs"] += 1
                    seen.add(key)
                    moved = _move(db, a, target, own_a, own_b, proof, api)
                    result["merged"] += int(moved)
            except (ValueError, TypeError, AttributeError, sqlite3.IntegrityError) as error:
                result["conflicts"].append(
                    {
                        "listing_a": candidate["id"],
                        "listing_b": target["id"] if target is not None else None,
                        "error": str(error),
                        "evidence": proof,
                    }
                )
        _bridge_as_stock(db, api, result, seen)
    return result
