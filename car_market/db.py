import copy
import csv
import dataclasses
import gzip
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing, contextmanager
from datetime import UTC, datetime

from .provenance import timestamp as evidence_time
from .text_encoding import json_text, scalar_text

_FINISHED_AT_DEFAULT = object()


def now():
    return datetime.now(UTC).isoformat()


def publish_export_files(stage, destination, names):
    """Restore previous outputs if publishing prepared files fails.

    Recovery links share the existing file data rather than copying the SQLite
    export. Readers can still see a transition between files; this does not
    make the complete export atomic or recover from a terminated process.
    """
    source = pathlib.Path(stage)
    target = pathlib.Path(destination)
    names = tuple(names)
    recovery = pathlib.Path(
        tempfile.mkdtemp(prefix=".market-export-recovery-", dir=target)
    )
    previous = set()
    published = []
    retain_recovery = False
    try:
        for name in names:
            try:
                os.link(target / name, recovery / name, follow_symlinks=False)
            except FileNotFoundError:
                if os.path.lexists(target / name):
                    raise
            else:
                previous.add(name)
        try:
            for name in names:
                (source / name).replace(target / name)
                published.append(name)
        except BaseException as failure:
            rollback_errors = []
            for name in reversed(published):
                try:
                    if name in previous:
                        (recovery / name).replace(target / name)
                    else:
                        (target / name).unlink()
                except BaseException as rollback_error:  # noqa: BLE001 — retain every rollback error and recovery file
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                retain_recovery = True
                raise BaseExceptionGroup(
                    f"Export rollback incomplete; recovery files retained in {recovery}",
                    [failure, *rollback_errors],
                ) from failure
            raise
    finally:
        if not retain_recovery:
            shutil.rmtree(recovery)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS runs(
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 kind TEXT NOT NULL, status TEXT NOT NULL, summary TEXT);
CREATE TABLE IF NOT EXISTS source_runs(
 id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id), source TEXT NOT NULL,
 query_key TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
 status TEXT NOT NULL, expected_count INTEGER, received_count INTEGER DEFAULT 0,
 target_count INTEGER DEFAULT 0, pages INTEGER DEFAULT 0, error TEXT,
 UNIQUE(run_id,source,query_key));
CREATE INDEX IF NOT EXISTS source_query_actual_time ON source_runs(
 source,query_key,julianday(COALESCE(finished_at,started_at)) DESC,id DESC);
CREATE TABLE IF NOT EXISTS fetches(
 id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id), source TEXT NOT NULL,
 url TEXT NOT NULL, final_url TEXT, fetched_at TEXT NOT NULL, status INTEGER,
 content_hash TEXT, raw_path TEXT, transport TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS fetch_url_time ON fetches(url,fetched_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS fetch_final_url_time ON fetches(final_url,fetched_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS fetch_source_blocked_actual_time ON fetches(
 source,julianday(fetched_at) DESC,id DESC) WHERE status IN (403,429);
CREATE INDEX IF NOT EXISTS fetch_blocked_actual_time ON fetches(
 julianday(fetched_at) DESC,id DESC) WHERE status IN (403,429);
CREATE TABLE IF NOT EXISTS vehicles(
 id TEXT PRIMARY KEY, kenteken TEXT UNIQUE, vin TEXT UNIQUE,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vehicle_aliases(
 alias_id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL REFERENCES vehicles(id),
 merged_at TEXT NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL,
 CHECK(alias_id<>vehicle_id));
CREATE INDEX IF NOT EXISTS aliases_vehicle ON vehicle_aliases(vehicle_id);
CREATE TABLE IF NOT EXISTS listings(
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
 vehicle_id TEXT REFERENCES vehicles(id), url TEXT NOT NULL, first_seen TEXT NOT NULL,
 last_seen TEXT NOT NULL, availability TEXT NOT NULL DEFAULT 'observed',
 scope TEXT NOT NULL, UNIQUE(source,external_id));
CREATE TABLE IF NOT EXISTS query_membership(
 source TEXT NOT NULL, query_key TEXT NOT NULL, listing_id INTEGER REFERENCES listings(id),
 last_run_id INTEGER REFERENCES runs(id), missed_complete_runs INTEGER DEFAULT 0,
 PRIMARY KEY(source,query_key,listing_id));
CREATE TABLE IF NOT EXISTS observations(
 id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id), listing_id INTEGER REFERENCES listings(id),
 observed_at TEXT NOT NULL, content_hash TEXT NOT NULL, title TEXT NOT NULL,
 price_eur REAL, price_type TEXT, mileage_km INTEGER, year INTEGER, transmission TEXT,
 fuel TEXT, seller TEXT, city TEXT, kenteken TEXT, vin TEXT, scope TEXT,
 details_json TEXT NOT NULL, fetch_id INTEGER REFERENCES fetches(id),
 UNIQUE(run_id,listing_id));
CREATE INDEX IF NOT EXISTS obs_listing_time ON observations(listing_id,observed_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS obs_fetch ON observations(fetch_id);
CREATE TABLE IF NOT EXISTS field_verifications(
 listing_id INTEGER NOT NULL REFERENCES listings(id),field TEXT NOT NULL,
 verified_at TEXT NOT NULL,fetch_id INTEGER REFERENCES fetches(id),
 value_hash TEXT NOT NULL,evidence_json TEXT NOT NULL,PRIMARY KEY(listing_id,field));
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id), listing_id INTEGER REFERENCES listings(id),
 observed_at TEXT NOT NULL, kind TEXT NOT NULL, previous_value TEXT, current_value TEXT);
CREATE TABLE IF NOT EXISTS identity_candidates(
 vehicle_a TEXT REFERENCES vehicles(id), vehicle_b TEXT REFERENCES vehicles(id),
 reason TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unconfirmed',
 PRIMARY KEY(vehicle_a,vehicle_b));
CREATE TABLE IF NOT EXISTS vehicle_checks(
 id INTEGER PRIMARY KEY, vehicle_id TEXT REFERENCES vehicles(id), checked_at TEXT NOT NULL,
 provider TEXT NOT NULL, data_json TEXT NOT NULL, fetch_id INTEGER REFERENCES fetches(id));
CREATE TABLE IF NOT EXISTS identity_quarantine(
 vehicle_id TEXT NOT NULL REFERENCES vehicles(id),field TEXT NOT NULL,value TEXT NOT NULL,
 quarantined_at TEXT NOT NULL,reason TEXT NOT NULL,evidence_json TEXT NOT NULL,
 PRIMARY KEY(vehicle_id,field,value));
DROP VIEW IF EXISTS purchase_candidates;
DROP VIEW IF EXISTS market_current;
DROP VIEW IF EXISTS latest_observations;
DROP VIEW IF EXISTS source_health;
CREATE VIEW latest_observations AS
 SELECT * FROM observations o WHERE o.id=(
  SELECT n.id FROM observations n WHERE n.listing_id=o.listing_id ORDER BY julianday(n.observed_at) DESC,n.id DESC LIMIT 1);
CREATE VIEW market_current AS
 SELECT l.id AS listing_id,l.source,l.external_id,l.vehicle_id,l.url,l.first_seen,l.last_seen,l.availability,
 v.kenteken AS confirmed_kenteken,o.title,o.price_eur,o.price_type,o.mileage_km,o.year,
 o.transmission,o.fuel,o.seller,o.city,l.scope,o.observed_at
 FROM listings l JOIN latest_observations o ON o.listing_id=l.id JOIN vehicles v ON v.id=l.vehicle_id;
CREATE VIEW purchase_candidates AS
 SELECT * FROM market_current WHERE scope='target' AND transmission='manual'
 AND price_eur>0 AND price_eur<=10000 AND price_type IN ('advertised_price','FIXED')
 AND availability='observed';
CREATE VIEW source_health AS
 SELECT s.* FROM source_runs s WHERE s.id=(
  SELECT n.id FROM source_runs n WHERE n.source=s.source AND n.query_key=s.query_key
  ORDER BY julianday(COALESCE(n.finished_at,n.started_at)) DESC,n.id DESC LIMIT 1);
"""


class Database:
    @classmethod
    def read_only(cls, path):
        db = cls.__new__(cls)
        db._in_transaction = False
        db.path = pathlib.Path(path).resolve()
        db.raw = db.path.parent / "raw"
        wal = pathlib.Path(str(db.path) + "-wal")
        mode = (
            "mode=ro" if wal.exists() and wal.stat().st_size else "mode=ro&immutable=1"
        )
        db.conn = sqlite3.connect(db.path.as_uri() + "?" + mode, uri=True, timeout=30)
        db.conn.row_factory = sqlite3.Row
        db.conn.execute("PRAGMA query_only=ON")
        return db

    def __init__(self, path):
        self._in_transaction = False
        self.path = pathlib.Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.raw = self.path.parent / "raw"
        self.raw.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            schema = SCHEMA.replace("PRAGMA journal_mode=WAL;", "").replace(
                "PRAGMA foreign_keys=ON;", ""
            )
            self.conn.executescript(
                "BEGIN IMMEDIATE;\n" + schema + "\nPRAGMA user_version=1;\nCOMMIT;"
            )
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            self.conn.executescript(
                "BEGIN IMMEDIATE;"
                "CREATE INDEX IF NOT EXISTS fetch_url_time ON fetches(url,fetched_at DESC,id DESC);"
                "CREATE INDEX IF NOT EXISTS fetch_final_url_time ON fetches(final_url,fetched_at DESC,id DESC);"
                "PRAGMA user_version=2; COMMIT;"
            )

        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 3:
            self.conn.executescript(
                "BEGIN IMMEDIATE;"
                "CREATE TABLE IF NOT EXISTS vehicle_aliases("
                "alias_id TEXT PRIMARY KEY,vehicle_id TEXT NOT NULL REFERENCES vehicles(id),"
                "merged_at TEXT NOT NULL,reason TEXT NOT NULL,evidence_json TEXT NOT NULL,"
                "CHECK(alias_id<>vehicle_id));"
                "CREATE INDEX IF NOT EXISTS aliases_vehicle ON vehicle_aliases(vehicle_id);"
                "PRAGMA user_version=3; COMMIT;"
            )

        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 4:
            self.conn.executescript(
                "BEGIN IMMEDIATE;"
                "CREATE INDEX IF NOT EXISTS obs_fetch ON observations(fetch_id);"
                "CREATE TABLE IF NOT EXISTS field_verifications("
                "listing_id INTEGER NOT NULL REFERENCES listings(id),field TEXT NOT NULL,"
                "verified_at TEXT NOT NULL,fetch_id INTEGER REFERENCES fetches(id),"
                "value_hash TEXT NOT NULL,evidence_json TEXT NOT NULL,PRIMARY KEY(listing_id,field));"
                "CREATE TABLE IF NOT EXISTS identity_quarantine("
                "vehicle_id TEXT NOT NULL REFERENCES vehicles(id),field TEXT NOT NULL,value TEXT NOT NULL,"
                "quarantined_at TEXT NOT NULL,reason TEXT NOT NULL,evidence_json TEXT NOT NULL,"
                "PRIMARY KEY(vehicle_id,field,value));"
                "DROP VIEW IF EXISTS source_health;"
                "CREATE VIEW source_health AS SELECT s.* FROM source_runs s WHERE s.id=("
                "SELECT n.id FROM source_runs n WHERE n.source=s.source AND n.query_key=s.query_key "
                "ORDER BY julianday(COALESCE(n.finished_at,n.started_at)) DESC,n.id DESC LIMIT 1);"
                "PRAGMA user_version=4; COMMIT;"
            )
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 5:
            self.conn.executescript(
                "BEGIN IMMEDIATE;"
                "CREATE INDEX IF NOT EXISTS obs_listing_actual_time ON observations("
                "listing_id,julianday(observed_at) DESC,id DESC);"
                "DROP VIEW IF EXISTS latest_observations;"
                "CREATE VIEW latest_observations AS SELECT * FROM observations o WHERE o.id=("
                "SELECT n.id FROM observations n WHERE n.listing_id=o.listing_id "
                "ORDER BY julianday(n.observed_at) DESC,n.id DESC LIMIT 1);"
                "PRAGMA user_version=5; COMMIT;"
            )
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 6:
            self.conn.executescript(
                "BEGIN IMMEDIATE;"
                "CREATE INDEX IF NOT EXISTS source_query_actual_time ON source_runs("
                "source,query_key,julianday(COALESCE(finished_at,started_at)) DESC,id DESC);"
                "CREATE INDEX IF NOT EXISTS fetch_source_blocked_actual_time ON fetches("
                "source,julianday(fetched_at) DESC,id DESC) WHERE status IN (403,429);"
                "PRAGMA user_version=6; COMMIT;"
            )
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 7:
            try:
                self.conn.executescript(
                    "BEGIN IMMEDIATE;"
                    "CREATE INDEX IF NOT EXISTS fetch_blocked_actual_time ON fetches("
                    "julianday(fetched_at) DESC,id DESC) WHERE status IN (403,429);"
                    "PRAGMA user_version=7; COMMIT;"
                )
            except BaseException:
                self.conn.rollback()
                self.conn.close()
                raise
        self._quarantine_legacy_registrations()

    def _quarantine_legacy_registrations(self):
        from .identity import unsupported_legacy_registrations

        proofs = unsupported_legacy_registrations(self.conn)
        for proof in proofs:
            original = proof["original_vehicle"]
            identity, registration = original["id"], original["kenteken"]
            self.conn.execute(
                "INSERT OR IGNORE INTO identity_quarantine VALUES (?,?,?,?,?,?)",
                (
                    identity,
                    "kenteken",
                    registration,
                    now(),
                    "audited_legacy_trovit_suffix_without_own_evidence",
                    json_text(proof),
                ),
            )
            self.conn.execute(
                "UPDATE vehicles SET kenteken=NULL WHERE id=? AND kenteken=?",
                (identity, registration),
            )
        if proofs:
            self._commit()

    @contextmanager
    def transaction(self):
        """Commit an import as a whole; regular passes commit each observation."""
        if self._in_transaction or self.conn.in_transaction:
            raise RuntimeError("A database transaction is already active")
        self.conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            self._in_transaction = False

    def _commit(self):
        if not self._in_transaction:
            self.conn.commit()

    def _bound_country(self):
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_state'"
        ).fetchone():
            return None
        row = self.conn.execute(
            "SELECT value FROM monitor_state WHERE key='market_scope'"
        ).fetchone()
        if row is None:
            return None
        country = json.loads(row[0]).get("country")
        if country not in {"NL", "DE"}:
            raise ValueError("Invalid database market binding")
        return country

    def start_run(self, kind="live", timestamp=None):
        cursor = self.conn.execute(
            "INSERT INTO runs(started_at,kind,status) VALUES (?,?,?)",
            (timestamp or now(), kind, "running"),
        )
        self._commit()
        return cursor.lastrowid

    def save_fetch(
        self,
        run,
        source,
        url,
        final_url,
        status,
        body,
        timestamp,
        transport="http",
        error=None,
        retain_raw=True,
    ):
        digest, raw_path = None, None
        if body is not None:
            raw = body.encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            if retain_raw:
                raw_path = str(pathlib.Path("raw") / (digest + ".html.gz"))
                target = self.path.parent / raw_path
                if target.exists():
                    with gzip.open(target, "rb") as f:
                        if hashlib.sha256(f.read()).hexdigest() != digest:
                            raise ValueError(
                                f"Existing raw content hash mismatch: {raw_path}"
                            )
                else:
                    # A failed write must never claim the final content-addressed
                    # name. Publish a closed, flushed archive without replacing
                    # an existing capture, including one created concurrently.
                    descriptor, temporary = tempfile.mkstemp(
                        prefix=".raw-", suffix=".tmp", dir=self.raw
                    )
                    temporary = pathlib.Path(temporary)
                    try:
                        with os.fdopen(descriptor, "wb") as archive:
                            with gzip.GzipFile(
                                filename="", mode="wb", fileobj=archive, mtime=0
                            ) as stream:
                                stream.write(raw)
                            archive.flush()
                            os.fsync(archive.fileno())
                        try:
                            os.link(temporary, target)
                        except FileExistsError:
                            with gzip.open(target, "rb") as existing:
                                if (
                                    hashlib.sha256(existing.read()).hexdigest()
                                    != digest
                                ):
                                    raise ValueError(
                                        f"Existing raw content hash mismatch: {raw_path}"
                                    )
                    finally:
                        temporary.unlink(missing_ok=True)
        cursor = self.conn.execute(
            "INSERT INTO fetches(run_id,source,url,final_url,fetched_at,status,content_hash,raw_path,transport,error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run,
                source,
                url,
                final_url,
                timestamp,
                status,
                digest,
                raw_path,
                transport,
                error,
            ),
        )
        self._commit()
        return cursor.lastrowid

    def resolve_vehicle_id(self, identity):
        row = self.conn.execute(
            "SELECT id FROM vehicles WHERE id=?", (identity,)
        ).fetchone()
        if row:
            return row[0]
        alias = self.conn.execute(
            "SELECT vehicle_id FROM vehicle_aliases WHERE alias_id=?", (identity,)
        ).fetchone()
        return alias[0] if alias else None

    def attach_identity(self, listing_id, kenteken=None, vin=None, evidence=None):
        if kenteken is not None and self._bound_country() == "DE":
            raise ValueError("German registration cannot be a stable vehicle identity")
        from .parse import plate

        if kenteken:
            kenteken = plate(kenteken)
            if kenteken is None:
                raise ValueError("Invalid registration; identity not attached")
        from .identity import normalized_vin

        vin = normalized_vin(vin)
        current = self.conn.execute(
            "SELECT v.* FROM listings l JOIN vehicles v ON v.id=l.vehicle_id WHERE l.id=?",
            (listing_id,),
        ).fetchone()
        if current is None:
            raise ValueError("Listing does not exist")
        known = self.conn.execute(
            "SELECT * FROM vehicles WHERE kenteken=? OR vin=?", (kenteken, vin)
        ).fetchall()
        records = {r["id"]: r for r in [current, *known]}
        for field, incoming in [("kenteken", kenteken), ("vin", vin)]:
            values = {r[field] for r in records.values() if r[field]}
            if incoming:
                values.add(incoming)
            if len(values) > 1:
                raise ValueError(
                    "Conflicting registration/VIN identities; manual review required"
                )
        registration = next(
            (r["kenteken"] for r in records.values() if r["kenteken"]), kenteken
        )
        chassis = next((r["vin"] for r in records.values() if r["vin"]), vin)
        # A published confirmed identity wins over a newly created provisional ID.
        established = [r for r in records.values() if r["kenteken"] or r["vin"]]
        winner = min(
            established or [current], key=lambda r: (r["created_at"], r["id"])
        )["id"]
        for loser, record in records.items():
            if loser == winner:
                continue
            candidates = self.conn.execute(
                "SELECT * FROM identity_candidates WHERE vehicle_a=? OR vehicle_b=?",
                (loser, loser),
            ).fetchall()
            members = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT id,source,external_id FROM listings WHERE vehicle_id=?",
                    (loser,),
                )
            ]
            proof = {
                "retired_vehicle": dict(record),
                "winner_at_merge": winner,
                "trigger_listing_id": listing_id,
                "registration": registration,
                "vin": chassis,
                "members": members,
                "previous_candidates": [dict(r) for r in candidates],
                "reference_evidence": evidence,
            }
            reason = (
                "exact_registration_and_vin"
                if registration and chassis
                else "exact_registration"
                if registration
                else "exact_vin"
            )
            self.conn.execute(
                "UPDATE listings SET vehicle_id=? WHERE vehicle_id=?", (winner, loser)
            )
            self.conn.execute(
                "UPDATE vehicle_checks SET vehicle_id=? WHERE vehicle_id=?",
                (winner, loser),
            )
            self.conn.execute(
                "UPDATE vehicle_aliases SET vehicle_id=? WHERE vehicle_id=?",
                (winner, loser),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO identity_quarantine SELECT ?,field,value,quarantined_at,reason,evidence_json FROM identity_quarantine WHERE vehicle_id=?",
                (winner, loser),
            )
            self.conn.execute(
                "DELETE FROM identity_quarantine WHERE vehicle_id=?", (loser,)
            )
            self.conn.execute(
                "INSERT INTO vehicle_aliases VALUES (?,?,?,?,?)",
                (loser, winner, now(), reason, json_text(proof)),
            )
            for candidate in candidates:
                other = (
                    candidate["vehicle_b"]
                    if candidate["vehicle_a"] == loser
                    else candidate["vehicle_a"]
                )
                if other == winner:
                    continue
                a, b = sorted((winner, other))
                self.conn.execute(
                    "INSERT OR IGNORE INTO identity_candidates VALUES (?,?,?,?,?)",
                    (
                        a,
                        b,
                        candidate["reason"],
                        candidate["created_at"],
                        candidate["status"],
                    ),
                )
            self.conn.execute(
                "DELETE FROM identity_candidates WHERE vehicle_a=? OR vehicle_b=?",
                (loser, loser),
            )
            self.conn.execute("DELETE FROM vehicles WHERE id=?", (loser,))
        self.conn.execute(
            "UPDATE vehicles SET kenteken=COALESCE(kenteken,?),vin=COALESCE(vin,?) WHERE id=?",
            (registration, chassis, winner),
        )
        return winner

    def observe(
        self,
        run,
        item,
        timestamp,
        fetch_id=None,
        query_key=None,
        *,
        vehicle_id_hint=None,
        preserve_observation=False,
    ):
        from contextlib import nullcontext

        from .model_profiles import bound_scope
        from .search_profile import search_context

        scope = bound_scope(self.conn)
        context = (
            search_context(scope)
            if scope and scope.get("model_profile") == "configured_vehicle"
            else nullcontext()
        )
        protected = self._in_transaction
        if protected:
            self.conn.execute("SAVEPOINT observe_identity")
        try:
            with context:
                result = self._observe(
                    run,
                    item,
                    timestamp,
                    fetch_id,
                    query_key,
                    vehicle_id_hint=vehicle_id_hint,
                    preserve_observation=preserve_observation,
                )
        except BaseException:
            if protected:
                self.conn.execute("ROLLBACK TO SAVEPOINT observe_identity")
                self.conn.execute("RELEASE SAVEPOINT observe_identity")
            else:
                self.conn.rollback()
            raise
        if protected:
            self.conn.execute("RELEASE SAVEPOINT observe_identity")
        return result

    def _observe(
        self,
        run,
        item,
        timestamp,
        fetch_id=None,
        query_key=None,
        *,
        vehicle_id_hint=None,
        preserve_observation=False,
    ):
        from .model_profiles import (
            BMW_PROFILE,
            MERCEDES_PROFILE,
            bound_scope,
            qualify_mercedes,
        )

        model_scope = bound_scope(self.conn)
        model_profile = (
            model_scope.get("model_profile", BMW_PROFILE)
            if model_scope
            else BMW_PROFILE
        )
        incoming_profile = item.raw.get("model_profile", BMW_PROFILE)
        if model_profile != incoming_profile:
            raise ValueError("Publication model profile does not match database")
        existing = self.conn.execute(
            "SELECT * FROM listings WHERE source=? AND external_id=?",
            (item.source, item.external_id),
        ).fetchone()
        foreign_existing = False
        if model_profile == "configured_vehicle":
            from .search_profile import qualify

            qualify(item, model_scope)
            if item.raw.get("market_country") != model_scope["country"]:
                # A verified own detail can invalidate a known publication without
                # creating or attaching any foreign vehicle identity.
                foreign_existing = bool(
                    existing
                    and item.raw.get("market_country")
                    and item.scope == "excluded"
                )
                if not foreign_existing:
                    raise ValueError(
                        "Own market country proof is required before identity writes"
                    )
        if model_profile == MERCEDES_PROFILE:
            qualify_mercedes(item, model_scope["country"])
            if item.raw.get("market_country") != model_scope["country"]:
                raise ValueError(
                    "Own market country proof is required before identity writes"
                )
        country = self._bound_country()
        german_item = (
            item.source.endswith("_de")
            or item.raw.get("market_context_country") == "DE"
        )
        if country == "NL" and german_item and not foreign_existing:
            raise ValueError("German item cannot write a bound Dutch database")
        if (country == "DE" or german_item) and not foreign_existing:
            if item.raw.get("market_country") != "DE":
                raise ValueError(
                    "Own German country proof is required before identity writes"
                )
            if item.kenteken:
                raise ValueError(
                    "German registration cannot be a stable vehicle identity"
                )
        original_payload = dataclasses.asdict(item) if preserve_observation else None
        if preserve_observation:
            item = copy.deepcopy(item)
        legacy_nl = (
            model_profile == BMW_PROFILE and country in (None, "NL") and not german_item
        )
        if legacy_nl:
            from .nl_publication import qualify_publication

            qualify_publication(item)
            if item.raw.get("nl_publication_conflicts", {}).get("identity"):
                if existing is None:
                    return None
                # The incoming URI belongs to a different publication. Preserve
                # the registry URI and retain its claim as unresolved native proof.
                item.url = existing["url"]
                item.kenteken = item.vin = None
        hint = (
            self.resolve_vehicle_id(vehicle_id_hint)
            if vehicle_id_hint is not None
            else None
        )
        if vehicle_id_hint is not None and hint is None:
            raise ValueError("Imported vehicle identity does not exist")
        if (
            existing is not None
            and hint is not None
            and self.resolve_vehicle_id(existing["vehicle_id"]) != hint
        ):
            raise ValueError("Imported listing conflicts with its vehicle registry")
        negative_own_nl = False
        if (
            existing
            and item.scope == "excluded"
            and model_profile == BMW_PROFILE
            and country in (None, "NL")
            and not german_item
            and fetch_id is not None
        ):
            from .provenance import timestamp as proof_time

            capture = self.conn.execute(
                "SELECT * FROM fetches WHERE id=?", (fetch_id,)
            ).fetchone()
            negative_own_nl = bool(
                capture
                and capture["source"] == item.source
                and capture["status"] == 200
                and capture["error"] is None
                and proof_time(capture["fetched_at"]) is not None
                and proof_time(capture["fetched_at"]) == proof_time(timestamp)
            )
        if item.scope == "excluded" and not (
            existing
            and (
                item.raw.get("market_context_country") == "DE"
                or model_profile in {MERCEDES_PROFILE, "configured_vehicle"}
                or negative_own_nl
            )
        ):
            return None
        if existing is None:
            identity = hint or str(uuid.uuid4())
            if hint is None:
                self.conn.execute(
                    "INSERT INTO vehicles(id,created_at) VALUES (?,?)",
                    (identity, timestamp),
                )
            cursor = self.conn.execute(
                "INSERT INTO listings(source,external_id,vehicle_id,url,first_seen,last_seen,scope) VALUES (?,?,?,?,?,?,?)",
                (
                    item.source,
                    item.external_id,
                    identity,
                    item.url,
                    timestamp,
                    timestamp,
                    item.scope,
                ),
            )
            listing_id = cursor.lastrowid
            self.conn.execute(
                "INSERT INTO events(run_id,listing_id,observed_at,kind,current_value) VALUES (?,?,?,?,?)",
                (run, listing_id, timestamp, "first_observed", item.url),
            )
        else:
            listing_id = existing["id"]
            availability = (
                "removed"
                if query_key and existing["availability"] == "removed"
                else "observed"
            )
            if existing["availability"] != "observed":
                unavailable_at = existing["last_seen"]
                if existing["availability"] == "removed":
                    removed = self.conn.execute(
                        "SELECT observed_at FROM events WHERE listing_id=? AND kind='http_removed' ORDER BY julianday(observed_at) DESC,id DESC LIMIT 1",
                        (listing_id,),
                    ).fetchone()
                    removed = removed[0] if removed else unavailable_at
                    unavailable_at = max((unavailable_at, removed), key=evidence_time)
                if evidence_time(timestamp) <= evidence_time(unavailable_at):
                    availability = existing["availability"]
            self.conn.execute(
                "UPDATE listings SET url=?,first_seen=CASE WHEN julianday(?)<julianday(first_seen) THEN ? ELSE first_seen END,last_seen=CASE WHEN julianday(?)>julianday(last_seen) THEN ? ELSE last_seen END,availability=?,scope=? WHERE id=?",
                (
                    item.url,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    availability,
                    item.scope,
                    listing_id,
                ),
            )
        vehicle_id = (
            existing["vehicle_id"]
            if negative_own_nl
            or foreign_existing
            or (
                existing
                and model_profile == "configured_vehicle"
                and item.scope != "target"
            )
            else self.attach_identity(listing_id, item.kenteken, item.vin)
        )
        check = self.conn.execute(
            "SELECT data_json,fetch_id,checked_at FROM vehicle_checks WHERE vehicle_id=? AND provider='rdw' ORDER BY checked_at DESC,id DESC LIMIT 1",
            (vehicle_id,),
        ).fetchone()
        if check and model_profile == MERCEDES_PROFILE and item.kenteken:
            item.raw["rdw_model_evidence"] = {
                "kenteken": item.kenteken,
                "data": json.loads(check["data_json"]),
                "fetch_id": check["fetch_id"],
                "checked_at": check["checked_at"],
            }
        if check and model_profile == BMW_PROFILE:
            name = json.loads(check[0]).get("handelsbenaming", "").upper()
            if "GRAN TOURER" in name or "GRAN COUPE" in name or ("218D" in name):
                item.scope = "model_conflict"
                self.conn.execute(
                    "UPDATE listings SET scope=? WHERE id=?", (item.scope, listing_id)
                )
        previous = self.conn.execute(
            "SELECT * FROM latest_observations WHERE listing_id=?", (listing_id,)
        ).fetchone()
        from .evidence import retain_enrichment

        prior = (
            previous
            if previous
            and evidence_time(timestamp) >= evidence_time(previous["observed_at"])
            else None
        )
        old_details = retain_enrichment(
            item,
            None if preserve_observation else prior,
            timestamp,
            fetch_id,
            query_key,
            run,
        )
        if model_profile == "configured_vehicle":
            from .publication_state import retain_state

            retain_state(
                item,
                old_details,
                timestamp,
                fetch_id,
                previous_observed_at=prior["observed_at"] if prior else None,
                previous_fetch_id=prior["fetch_id"] if prior else None,
            )
            if not previous or prior is not None:
                from .publication_state import availability as own_availability

                state = item.raw.get("availability_evidence", {}).get("value")
                if state in {"sold", "reserved"}:
                    self.conn.execute(
                        "UPDATE listings SET availability=? WHERE id=?",
                        (
                            "seller_marked_sold"
                            if state == "sold"
                            else "seller_reserved",
                            listing_id,
                        ),
                    )
                elif own_availability(item.raw) == "available":
                    self.conn.execute(
                        "UPDATE listings SET availability='observed' WHERE id=?",
                        (listing_id,),
                    )
        from .sale_terms import classify

        classify(item)
        if model_profile == MERCEDES_PROFILE:
            if prior and not preserve_observation:
                from .model_profiles import generation_proof
                from .parse import Listing

                old_item = Listing(**old_details)
                old_generation, proof = generation_proof(old_item, True)
                if old_generation == "C205":
                    retained = old_item.raw.get("retained_generation_evidence")
                    item.raw["retained_generation_evidence"] = (
                        retained
                        if proof.get("kind") == "retained_own_generation"
                        else {
                            "value": "C205",
                            "source": item.source,
                            "external_id": item.external_id,
                            "observed_at": prior["observed_at"],
                            "fetch_id": prior["fetch_id"],
                            "proof": proof,
                        }
                    )
            qualify_mercedes(item, model_scope["country"])
        if legacy_nl:
            # Retained model text cannot replace unresolved own publication proof.
            qualify_publication(item)
        current_scope = item.scope if not previous or prior else previous["scope"]
        self.conn.execute(
            "UPDATE listings SET scope=? WHERE id=?", (current_scope, listing_id)
        )
        normalized_payload = dataclasses.asdict(item)
        payload = original_payload if preserve_observation else normalized_payload
        from .provenance import verify_fields

        if not (legacy_nl and item.raw.get("nl_publication_conflicts")):
            verify_fields(self.conn, listing_id, normalized_payload)
        if previous:
            fields = [
                "title",
                "price",
                "price_type",
                "mileage",
                "year",
                "transmission",
                "fuel",
                "seller",
                "city",
                "kenteken",
                "vin",
                "scope",
                "description",
                "images",
            ]
            metadata = (
                "detail_complete",
                "field_conflicts",
                "description_may_be_truncated",
                "description_truncated",
                "price_warning",
                "historical_only",
                "sold",
                "reserved",
                "nl_price_conditional",
            )
            if model_profile == MERCEDES_PROFILE:
                metadata += (
                    "model_profile",
                    "model_scope_evidence",
                    "rdw_model_evidence",
                    "country_evidence",
                )
            if item.raw.get("market_context_country") == "DE":
                fields.append("links")
                metadata += (
                    "publication_reference",
                    "own_publication_reference",
                    "publication_url_verified",
                    "own_metadata_fields",
                    "options",
                    "country_evidence",
                    "own_details",
                    "own_inventory_record",
                    "own_smyle_record",
                    "native_stock_record",
                    "model_brand",
                    "model_family",
                    "model_variant",
                    "offer_role",
                    "dealer_stock_number",
                    "dealer_stock_id",
                    "market_country",
                )
            same_publication = True
            if (
                model_profile == BMW_PROFILE
                and country in (None, "NL")
                and not german_item
            ):
                from .nl_publication import publication_facts

                same_publication = publication_facts(old_details) == publication_facts(
                    payload
                )
            if (
                not preserve_observation
                and same_publication
                and model_profile != "configured_vehicle"
                and all(old_details.get(k) == payload.get(k) for k in fields)
                and all(
                    old_details.get("raw", {}).get(k) == payload.get("raw", {}).get(k)
                    for k in metadata
                )
            ):
                # Presence/freshness are independent of a changed market observation.
                if query_key:
                    self.conn.execute(
                        "INSERT INTO query_membership(source,query_key,listing_id,last_run_id) VALUES (?,?,?,?) ON CONFLICT(source,query_key,listing_id) DO UPDATE SET last_run_id=excluded.last_run_id,missed_complete_runs=0",
                        (item.source, query_key, listing_id, run),
                    )
                self._commit()
                return listing_id
        # Preserve structured evidence but omit contact phone/e-mail fields from normalized columns.
        content = json_text(payload, sort_keys=True)
        digest = hashlib.sha256(content.encode()).hexdigest()
        if previous and evidence_time(timestamp) >= evidence_time(
            previous["observed_at"]
        ):
            for key, new in [("price_eur", item.price), ("mileage_km", item.mileage)]:
                if (
                    new is not None
                    and previous[key] is not None
                    and previous[key] != new
                ):
                    same_run = previous["run_id"] == run
                    if not same_run:
                        self.conn.execute(
                            "INSERT INTO events(run_id,listing_id,observed_at,kind,previous_value,current_value) VALUES (?,?,?,?,?,?)",
                            (
                                run,
                                listing_id,
                                timestamp,
                                key + "_changed",
                                str(previous[key]),
                                str(new),
                            ),
                        )
        self.conn.execute(
            """INSERT INTO observations(run_id,listing_id,observed_at,content_hash,title,price_eur,price_type,mileage_km,year,transmission,fuel,seller,city,kenteken,vin,scope,details_json,fetch_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id,listing_id) DO UPDATE SET observed_at=excluded.observed_at,content_hash=excluded.content_hash,
            title=excluded.title,price_eur=excluded.price_eur,price_type=excluded.price_type,
            mileage_km=excluded.mileage_km,year=excluded.year,
            transmission=excluded.transmission,fuel=excluded.fuel,
            seller=excluded.seller,city=excluded.city,
            kenteken=excluded.kenteken,vin=excluded.vin,
            scope=excluded.scope,details_json=excluded.details_json,fetch_id=excluded.fetch_id""",
            (
                run,
                listing_id,
                timestamp,
                digest,
                scalar_text(item.title),
                item.price,
                item.price_type,
                item.mileage,
                item.year,
                item.transmission,
                item.fuel,
                scalar_text(item.seller),
                scalar_text(item.city),
                item.kenteken,
                item.vin,
                item.scope,
                content,
                fetch_id,
            ),
        )
        if query_key:
            self.conn.execute(
                "INSERT INTO query_membership(source,query_key,listing_id,last_run_id) VALUES (?,?,?,?) ON CONFLICT(source,query_key,listing_id) DO UPDATE SET last_run_id=excluded.last_run_id,missed_complete_runs=0",
                (item.source, query_key, listing_id, run),
            )
        self._commit()
        return listing_id

    def finish_source(
        self,
        run,
        source,
        key,
        started,
        status,
        expected,
        received,
        targets,
        pages,
        error=None,
        *,
        infer_missing=True,
        finished_at=_FINISHED_AT_DEFAULT,
    ):
        self.conn.execute(
            "INSERT OR REPLACE INTO source_runs(run_id,source,query_key,started_at,finished_at,status,expected_count,received_count,target_count,pages,error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run,
                source,
                key,
                started,
                now() if finished_at is _FINISHED_AT_DEFAULT else finished_at,
                status,
                expected,
                received,
                targets,
                pages,
                error,
            ),
        )
        # Only fully enumerated stable queries can produce non-observation signals.
        if status == "complete" and infer_missing:
            missing = self.conn.execute(
                "SELECT listing_id FROM query_membership WHERE source=? AND query_key=? AND last_run_id<>?",
                (source, key, run),
            ).fetchall()
            for record in missing:
                self.conn.execute(
                    "UPDATE query_membership SET missed_complete_runs=missed_complete_runs+1 WHERE source=? AND query_key=? AND listing_id=?",
                    (source, key, record[0]),
                )
                self.conn.execute(
                    "INSERT INTO events(run_id,listing_id,observed_at,kind,current_value) VALUES (?,?,?,?,?)",
                    (run, record[0], now(), "not_seen_in_complete_query", key),
                )
            # Availability is intentionally not 'sold'; listings in other completed queries may persist.
        self._commit()

    def candidates(self):
        # Exact year+mileage+city is a candidate only; never silently merge by these fields.
        pairs = self.conn.execute("""SELECT DISTINCT a.vehicle_id AS a,b.vehicle_id AS b FROM market_current a JOIN market_current b
          ON a.year=b.year AND a.mileage_km=b.mileage_km AND LOWER(a.city)=LOWER(b.city)
          WHERE a.vehicle_id<b.vehicle_id AND a.source<>b.source AND a.mileage_km>0 AND LENGTH(a.city)>0""").fetchall()
        for p in pairs:
            self.conn.execute(
                "INSERT OR IGNORE INTO identity_candidates VALUES (?,?,?,?,?)",
                (p["a"], p["b"], "same_year_mileage_city", now(), "unconfirmed"),
            )
        self._commit()

    def finish_run(self, run, status, summary):
        self.conn.execute(
            "UPDATE runs SET finished_at=?,status=?,summary=? WHERE id=?",
            (now(), status, json_text(summary), run),
        )
        self._commit()

    def export(self, destination, *, mode="full"):
        from .export_formats import (
            MANIFEST,
            compact_manifest,
            validate_compact_backup,
            validate_mode,
        )

        validate_mode(mode)
        target = pathlib.Path(destination)
        backup_path = target / "market.sqlite3"
        from .model_profiles import BMW_PROFILE, bound_scope, validate_profile_path

        model_scope = bound_scope(self.conn)
        country = self._bound_country()
        if model_scope:
            validate_profile_path(
                backup_path, country, model_scope.get("model_profile", BMW_PROFILE)
            )
        if country:
            from .germany import validate_market_path

            validate_market_path(backup_path, country)
        if backup_path.resolve() == self.path or (
            backup_path.exists() and backup_path.samefile(self.path)
        ):
            raise ValueError(
                "Export backup and working database must be different files"
            )
        if mode == "compact":
            target = target / "compact"
            validate_compact_backup(self, target, pathlib.Path(destination))
            if self.conn.in_transaction:
                raise ValueError("Export requires committed observations")
        target.mkdir(parents=True, exist_ok=True)
        queries = {
            "market-current": "SELECT * FROM market_current ORDER BY price_eur",
            "price-history": "SELECT l.source,l.external_id,l.vehicle_id,l.url,o.* FROM observations o JOIN listings l ON l.id=o.listing_id ORDER BY o.observed_at",
            "source-health": "SELECT * FROM source_health ORDER BY source,query_key",
            "events": "SELECT * FROM events ORDER BY id",
            "purchase-candidates": "SELECT * FROM purchase_candidates ORDER BY price_eur",
            "identity-candidates": "SELECT * FROM identity_candidates",
            "rdw-checks": "SELECT * FROM vehicle_checks",
            "vehicle-id-aliases": "SELECT * FROM vehicle_aliases ORDER BY alias_id",
            "field-verifications": "SELECT * FROM field_verifications ORDER BY listing_id,field",
        }
        if mode == "compact":
            del queries["price-history"]
        if self.conn.in_transaction:
            raise ValueError("Export requires committed observations")
        self.conn.execute("BEGIN")
        try:
            if mode == "compact":
                self.conn.execute("SELECT id FROM runs LIMIT 1").fetchone()
                as_of = now()
            # Prepare all files before publishing; ENOSPC verified in test_cli_import_export_failure_preserves_previous_files_and_can_retry.
            with tempfile.TemporaryDirectory(
                prefix=".market-export-", dir=target
            ) as temporary:
                stage = pathlib.Path(temporary)
                for name, query in queries.items():
                    cursor = self.conn.execute(query)
                    with (stage / (name + ".csv")).open("w", newline="") as f:
                        writer = csv.writer(f)
                        writer.writerow([c[0] for c in cursor.description])
                        writer.writerows(cursor)
                from .vehicle_catalog import catalog

                cards = (
                    catalog(self.conn, {"as_of": as_of})
                    if mode == "compact"
                    else catalog(self.conn)
                )
                if mode == "full":
                    with (stage / "vehicles.json").open("w") as stream:
                        stream.write(json_text(cards, indent=2))
                columns = [
                    "id",
                    "identity_status",
                    "kenteken",
                    "vin",
                    "title",
                    "year",
                    "fuel",
                    "transmission",
                    "mileage_km",
                    "seller",
                    "city",
                    "is_live",
                    "price_min_eur",
                    "price_max_eur",
                    "listing_count",
                    "active_offer_count",
                ]
                with (stage / "vehicles.csv").open("w", newline="") as stream:
                    writer = csv.DictWriter(
                        stream, fieldnames=columns, extrasaction="ignore"
                    )
                    writer.writeheader()
                    writer.writerows(cards)
                with closing(sqlite3.connect(stage / "market.sqlite3")) as backup:
                    self.conn.backup(backup)
                summary = self.summary()
                names = [
                    *(name + ".csv" for name in queries),
                    "vehicles.csv",
                    *(["vehicles.json"] if mode == "full" else []),
                    "market.sqlite3",
                ]
                if mode == "compact":
                    receipt = compact_manifest(self, stage, target, names, as_of)
                    (stage / MANIFEST).write_text(
                        json_text(receipt, indent=2), encoding="utf-8"
                    )
                    names.append(MANIFEST)
                    summary = {**summary, "export_artifacts": receipt}
                    validate_compact_backup(self, target, pathlib.Path(destination))
                publish_export_files(
                    stage,
                    target,
                    names,
                )
        finally:
            self.conn.rollback()
        return summary

    def summary(self):
        return {
            "listings": self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[
                0
            ],
            "vehicle_identities": self.conn.execute(
                "SELECT COUNT(*) FROM vehicles"
            ).fetchone()[0],
            "vehicles_with_registration": self.conn.execute(
                "SELECT COUNT(*) FROM vehicles WHERE kenteken IS NOT NULL"
            ).fetchone()[0],
            "observations": self.conn.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0],
            "purchase_candidates": self.conn.execute(
                "SELECT COUNT(*) FROM purchase_candidates"
            ).fetchone()[0],
            "vehicle_id_aliases": self.conn.execute(
                "SELECT COUNT(*) FROM vehicle_aliases"
            ).fetchone()[0],
            "possible_duplicate_pairs": self.conn.execute(
                "SELECT COUNT(*) FROM identity_candidates"
            ).fetchone()[0],
            "latest_sources": [
                dict(x)
                for x in self.conn.execute(
                    "SELECT * FROM source_health ORDER BY source,query_key"
                )
            ],
        }
