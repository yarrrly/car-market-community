"""Explicit shared AS/AT publications; read-only, without merging or field freshness checks."""

import gzip
import hashlib
import pathlib
import re
import zlib
from collections import defaultdict
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .parse import json_documents

_UUID = re.compile(r"(?:/|-)([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?$", re.IGNORECASE)
_SOURCES = {"autoscout24", "autotrader"}


class _HeadReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonical = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "canonical" in (attrs.get("rel") or "").lower().split():
            self.canonical.append(attrs.get("href"))


def _route_uuid(uri, source):
    if source not in _SOURCES or not isinstance(uri, str):
        return None
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    prefix = "/aanbod/" if source == "autoscout24" else "/auto/voertuig/"
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.netloc.lower() not in {source + ".nl", "www." + source + ".nl"}
        or not parsed.path.startswith(prefix)
    ):
        return None
    match = _UUID.search(parsed.path)
    return match.group(1).lower() if match else None


def reference_from_payload(source, payload, own_uri):
    """Verify the publication listingDetails and return its AS publisher URI."""
    identity = _route_uuid(own_uri, source)
    if (
        identity is None
        or not isinstance(payload, dict)
        or str(payload.get("id", "")).lower() != identity
        or not isinstance(payload.get("vehicle"), dict)
    ):
        return None
    reference = payload.get("webPage")
    if _route_uuid(reference, "autoscout24") != identity:
        return None
    parsed_reference = urlsplit(reference)
    if parsed_reference.query or parsed_reference.fragment:
        return None
    return {"publication_uuid": identity, "publisher_uri": reference, "own_uri": own_uri}


def _verified_fetch(db, listing, fetch):
    if (
        fetch["source"] != listing["source"]
        or _route_uuid(fetch["final_url"], listing["source"]) != listing["external_id"].lower()
    ):
        return None
    relative = pathlib.Path(fetch["raw_path"])
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("raw",):
        return None
    try:
        raw_root = pathlib.Path(db.raw).resolve()
        if not raw_root.is_relative_to(pathlib.Path(db.path).parent.resolve()):
            return None
        raw_file = raw_root.joinpath(*relative.parts[1:]).resolve()
    except (OSError, RuntimeError):
        return None
    if not raw_file.is_relative_to(raw_root) or not raw_file.is_file():
        return None
    try:
        body = gzip.decompress(raw_file.read_bytes())
        if hashlib.sha256(body).hexdigest() != fetch["content_hash"]:
            return None
        text = body.decode("utf-8")
    except (OSError, EOFError, UnicodeError, zlib.error):
        return None
    next_scripts = [
        '<script type="application/json">' + body + "</script>"
        for attrs, body in re.findall(
            r"<script([^>]*)>(.*?)</script>", text, re.IGNORECASE | re.DOTALL
        )
        if re.search(r'\bid\s*=\s*["\x27]__NEXT_DATA__["\x27]', attrs, re.IGNORECASE)
        and re.search(r'\btype\s*=\s*["\x27]application/json["\x27]', attrs, re.IGNORECASE)
    ]
    if len(next_scripts) != 1:
        return None
    details = []
    for document in json_documents(next_scripts[0]):
        props = document.get("props") if isinstance(document, dict) else None
        page = props.get("pageProps") if isinstance(props, dict) else None
        detail = page.get("listingDetails") if isinstance(page, dict) else None
        if isinstance(detail, dict):
            details.append(detail)
    if len(details) != 1:
        return None
    reference = reference_from_payload(listing["source"], details[0], fetch["final_url"])
    if reference is None:
        return None
    head = _HeadReferences()
    head.feed(re.split(r"</head\s*>", text, maxsplit=1, flags=re.IGNORECASE)[0])
    if len(head.canonical) > 1 or any(
        _route_uuid(uri, listing["source"]) != listing["external_id"].lower()
        for uri in head.canonical
    ):
        return None
    return {
        **reference,
        "listing_id": listing["id"],
        "fetch_id": fetch["id"],
        "fetched_at": fetch["fetched_at"],
        "content_hash": fetch["content_hash"],
        "raw_path": fetch["raw_path"],
        "requested_uri": fetch["url"],
        "final_uri": fetch["final_url"],
        "own_uuid": details[0]["id"],
        "reference_path": "props.pageProps.listingDetails.webPage",
        "canonical_uri": head.canonical[0] if head.canonical else None,
    }


def _references(db, listing):
    rows = db.conn.execute(
        "SELECT DISTINCT f.id,f.source,f.url,f.final_url,f.fetched_at,f.content_hash,f.raw_path "
        "FROM observations o JOIN fetches f ON f.id=o.fetch_id "
        "WHERE o.listing_id=? AND f.status=200 AND f.raw_path IS NOT NULL "
        "AND f.final_url LIKE ? ORDER BY f.fetched_at DESC,f.id DESC LIMIT 4",
        (listing["id"], "%" + listing["external_id"] + "%"),
    ).fetchall()
    evidence = []
    for fetch in rows:
        verified = _verified_fetch(db, listing, fetch)
        if verified:
            evidence.append({**verified, "retrieval": "observation_fetch_join"})
    if not evidence:
        # Only exact URLs with existing indexes; no database-wide UUID search.
        rows = db.conn.execute(
            "SELECT id,source,url,final_url,fetched_at,content_hash,raw_path FROM fetches "
            "WHERE (url=? OR final_url=?) AND status=200 AND raw_path IS NOT NULL "
            "ORDER BY fetched_at DESC,id DESC LIMIT 4",
            (listing["url"], listing["url"]),
        ).fetchall()
        for fetch in rows:
            verified = _verified_fetch(db, listing, fetch)
            if verified:
                evidence.append({**verified, "retrieval": "indexed_exact_url_fallback"})
    return evidence


def shared_publication_pairs(db):
    """Return explicit pairs of distinct current roots, preserving the caller transaction."""
    owns_snapshot = not db.conn.in_transaction
    if owns_snapshot:
        db.conn.execute("BEGIN")
    try:
        rows = db.conn.execute(
            "SELECT l.id,l.source,l.external_id,l.vehicle_id,l.url,l.scope, "
            "COALESCE(v.id,av.id) AS resolved_root, "
            "CASE WHEN v.id IS NOT NULL THEN v.kenteken ELSE av.kenteken END AS kenteken, "
            "CASE WHEN v.id IS NOT NULL THEN v.vin ELSE av.vin END AS vin "
            "FROM listings l LEFT JOIN vehicles v ON v.id=l.vehicle_id "
            "LEFT JOIN vehicle_aliases a ON a.alias_id=l.vehicle_id "
            "LEFT JOIN vehicles av ON av.id=a.vehicle_id "
            "WHERE l.source IN ('autoscout24','autotrader')"
        ).fetchall()
        grouped = defaultdict(dict)
        for row in rows:
            if (
                row["resolved_root"]
                and _route_uuid(row["url"], row["source"]) == row["external_id"].lower()
            ):
                grouped[row["external_id"].lower()][row["source"]] = row
        pairs = []
        for identity, publishers in sorted(grouped.items()):
            if set(publishers) != _SOURCES:
                continue
            a, b = publishers["autoscout24"], publishers["autotrader"]
            if a["resolved_root"] == b["resolved_root"]:
                continue
            if any(a[field] and b[field] and a[field] != b[field] for field in ("kenteken", "vin")):
                continue
            as_refs, at_refs = _references(db, a), _references(db, b)
            proofs = []
            for as_ref in as_refs:
                for at_ref in at_refs:
                    if as_ref["publisher_uri"] == at_ref["publisher_uri"]:
                        proofs.append(
                            {
                                "kind": "shared_autoscout_publication",
                                "publication_uuid": identity,
                                "reference_url": as_ref["publisher_uri"],
                                "a_fetch_id": as_ref["fetch_id"],
                                "b_fetch_id": at_ref["fetch_id"],
                                "publisher_uri": as_ref["publisher_uri"],
                                "autoscout": as_ref,
                                "autotrader": at_ref,
                            }
                        )
            if proofs:
                pairs.append(
                    {
                        "listing_a": a["id"],
                        "listing_b": b["id"],
                        "vehicle_a": a["resolved_root"],
                        "vehicle_b": b["resolved_root"],
                        "scope_a": a["scope"],
                        "scope_b": b["scope"],
                        "warnings": ["existing_model_conflict"]
                        if "model_conflict" in {a["scope"], b["scope"]}
                        else [],
                        "evidence": [proofs[0]],
                    }
                )
        return pairs
    finally:
        if owns_snapshot:
            db.conn.rollback()


def validate_shared_pair(db, listing_a, listing_b, evidence):
    """Recheck both retained details before merging; a UUID/supplied JSON is insufficient."""
    owns_snapshot = not db.conn.in_transaction
    if owns_snapshot:
        db.conn.execute("BEGIN")
    try:
        if isinstance(evidence, list) and len(evidence) == 1:
            evidence = evidence[0]
        if not isinstance(evidence, dict) or evidence.get("kind") != "shared_autoscout_publication":
            raise ValueError("Missing explicit publisher reference evidence")
        rows = db.conn.execute(
            "SELECT id,source,external_id,url FROM listings WHERE id IN (?,?)",
            (listing_a, listing_b),
        ).fetchall()
        if len(rows) != 2 or {row["source"] for row in rows} != _SOURCES:
            raise ValueError("Publisher reference needs both source publications")
        publications = {row["source"]: row for row in rows}
        a, b = publications["autoscout24"], publications["autotrader"]
        identity = a["external_id"].lower()
        if b["external_id"].lower() != identity or evidence.get("publication_uuid") != identity:
            raise ValueError("Publisher reference UUIDs disagree")
        for source, listing, fetch_key in (
            ("autoscout", a, "a_fetch_id"),
            ("autotrader", b, "b_fetch_id"),
        ):
            fetch_id = evidence.get(fetch_key)
            if not isinstance(fetch_id, int) or isinstance(fetch_id, bool):
                raise ValueError("Publisher reference needs retained fetch IDs")  # noqa: TRY004 — invalid audit evidence
            fetch = db.conn.execute(
                "SELECT id,source,url,final_url,fetched_at,content_hash,raw_path,status FROM fetches WHERE id=?",
                (fetch_id,),
            ).fetchone()
            if fetch is None or fetch["status"] != 200 or not fetch["raw_path"]:
                raise ValueError("Publisher reference has no retained own HTTP 200")
            verified = _verified_fetch(db, listing, fetch)
            supplied = evidence.get(source)
            if (
                verified is None
                or verified["publisher_uri"] != evidence.get("reference_url")
                or verified["publisher_uri"] != evidence.get("publisher_uri")
                or not isinstance(supplied, dict)
                or any(supplied.get(key) != value for key, value in verified.items())
            ):
                raise ValueError("Publisher reference does not match verified own payloads")
        return True
    finally:
        if owns_snapshot:
            db.conn.rollback()
