"""Post-collection outcomes; these clocks never renew source evidence."""

import copy
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .export_formats import (
    target_directory,
    valid_directory,
    valid_receipt,
    validate_mode,
)
from .provenance import timestamp as _timestamp

KEY = "publication_state"
PHASES = ("signals", "export", "last_run")
STATUSES = {"success", "failed", "inprogress", "disabled", "unresolved"}


def timestamp(value):
    try:
        return _timestamp(value)
    except OverflowError:
        return None


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _now():
    return datetime.now(UTC).isoformat()


def _database_key(path):
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()


def _file_json(path, maximum=65536):
    try:
        if path.is_symlink() or path.stat().st_size > maximum:
            return None
        with path.open(encoding="utf-8") as stream:
            body = stream.read(maximum + 1)
        return json.loads(body) if len(body) <= maximum else None
    except (OSError, ValueError, UnicodeError):
        return None


def _valid_state(conn, state, clock, path=None):
    if not isinstance(state, dict) or set(state) != {
        "attempt_id",
        "run_id",
        "mode",
        "collection_status",
        "collection_finished_at",
        "started_at",
        "phases",
    }:
        return False
    if type(state["run_id"]) is not int or state["run_id"] <= 0:
        return False
    if not isinstance(state["attempt_id"], str) or len(state["attempt_id"]) != 32:
        return False
    if not isinstance(state["mode"], str) or state["mode"] not in {
        "full",
        "skipped_fast",
    }:
        return False
    row = conn.execute(
        "SELECT kind,status,finished_at FROM runs WHERE id=?", (state["run_id"],)
    ).fetchone()
    if (
        not row
        or row["status"] != state["collection_status"]
        or row["finished_at"] != state["collection_finished_at"]
    ):
        return False
    if (row["kind"] == "fast") != (state["mode"] == "skipped_fast"):
        return False
    if state["mode"] == "full" and row["kind"] not in {"live", "full"}:
        return False
    collected = timestamp(state["collection_finished_at"])
    begun = timestamp(state["started_at"])
    for key in ("collection_finished_at", "started_at"):
        at = timestamp(state[key])
        if at is None or at > clock:
            return False
    if begun < collected:
        return False
    phases = state["phases"]
    if not isinstance(phases, dict) or set(phases) != set(PHASES):
        return False
    for name, phase in phases.items():
        keys = {
            "status",
            "started_at",
            "finished_at",
            "error",
        }
        contextual = isinstance(phase, dict) and "format" in phase
        if contextual:
            keys |= {"format", "directory", "receipt"}
        if not isinstance(phase, dict) or set(phase) != keys:
            return False
        if not isinstance(phase["status"], str) or phase["status"] not in STATUSES:
            return False
        start, finish = timestamp(phase["started_at"]), timestamp(phase["finished_at"])
        if phase["started_at"] is not None and (start is None or start > clock):
            return False
        if phase["finished_at"] is not None and (finish is None or finish > clock):
            return False
        if finish and (not start or finish < start):
            return False
        if start and start < begun:
            return False
        if contextual:
            if (
                name != "export"
                or not isinstance(phase["format"], str)
                or phase["format"] not in {"full", "compact"}
                or not valid_directory(phase["directory"], phase["format"])
            ):
                return False
            receipt = phase["receipt"]
            if phase["format"] == "compact" and phase["status"] == "success":
                if path is None:
                    path = conn.execute("PRAGMA database_list").fetchone()[2]
                if not valid_receipt(
                    conn,
                    path,
                    receipt,
                    phase["directory"],
                    state["run_id"],
                    start,
                    finish,
                ):
                    return False
            elif receipt is not None:
                return False
        if phase["status"] in {"success", "failed"} and (not start or not finish):
            return False
        error = phase["error"]
        if error is not None:
            if not isinstance(error, dict) or set(error) != {"type", "errno"}:
                return False
            if not isinstance(error["type"], str) or error["type"] not in {
                "OSError",
                "sqlite_error",
                "cancelled",
                "exception",
            }:
                return False
            if error["errno"] is not None and (
                type(error["errno"]) is not int or not 0 <= error["errno"] <= 4095
            ):
                return False
    return True


def _documents(db):
    raw = []
    if db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='monitor_state'"
    ).fetchone():
        row = db.conn.execute(
            "SELECT value FROM monitor_state WHERE key=?", (KEY,)
        ).fetchone()
        if row and isinstance(row[0], str) and len(row[0]) <= 65536:
            try:
                raw.append(json.loads(row[0]))
            except ValueError:
                pass
    raw.append(_file_json(db.path.parent / "publication-attempt.json"))
    clock = datetime.now(UTC)
    valid = []
    for doc in raw:
        required = {
            "schema_version",
            "database_key",
            "updated_at",
            "revision",
            "latest",
            "last_full",
            "last_export",
        }
        if (
            not isinstance(doc, dict)
            or not required <= set(doc)
            or set(doc) - required - {"last_full_format_export"}
        ):
            continue
        if (
            type(doc["schema_version"]) is not int
            or doc["schema_version"] != 1
            or doc["database_key"] != _database_key(db.path)
        ):
            continue
        if type(doc["revision"]) is not int or doc["revision"] <= 0:
            continue
        stamp = timestamp(doc["updated_at"])
        if stamp is None or stamp > clock:
            continue
        if not _valid_state(db.conn, doc["latest"], clock, db.path):
            continue
        if any(
            doc[key] is not None and not _valid_state(db.conn, doc[key], clock, db.path)
            for key in ("last_full", "last_export", "last_full_format_export")
            if key in doc
        ):
            continue
        states = [
            doc[key]
            for key in ("latest", "last_full", "last_export", "last_full_format_export")
            if key in doc and doc[key] is not None
        ]
        if any(
            timestamp(state["started_at"]) > stamp
            or any(
                timestamp(phase[key]) is not None and timestamp(phase[key]) > stamp
                for phase in state["phases"].values()
                for key in ("started_at", "finished_at")
            )
            for state in states
        ):
            continue
        valid.append(doc)
    return sorted(
        valid,
        key=lambda doc: (timestamp(doc["updated_at"]), doc["revision"]),
        reverse=True,
    )


def _status(state):
    if state is None:
        return "unresolved"
    values = [p["status"] for p in state["phases"].values()]
    for status in ("failed", "inprogress", "unresolved"):
        if status in values:
            return status
    return "success"


def _safe_error(error):
    # A rollback group lists its original publication error first.
    for _ in range(4):
        if not isinstance(error, BaseExceptionGroup) or not error.exceptions:
            break
        error = error.exceptions[0]
    number = getattr(error, "errno", None)
    kind = (
        "OSError"
        if isinstance(error, OSError)
        else "sqlite_error"
        if isinstance(error, sqlite3.Error)
        else "cancelled"
        if isinstance(error, (KeyboardInterrupt, SystemExit))
        else "exception"
    )
    return {
        "type": kind,
        "errno": number if type(number) is int and 0 <= number <= 4095 else None,
    }


class Publication:
    def __init__(
        self,
        db,
        run_id,
        *,
        fast=False,
        signals=False,
        writer=atomic_json,
        export_mode="full",
        export_dir=None,
    ):
        validate_mode(export_mode)
        if export_mode == "compact" and export_dir is None:
            raise ValueError("Compact publication requires an export directory")
        self.db, self.writer = db, writer
        row = db.conn.execute(
            "SELECT status,finished_at FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        previous = _documents(db)
        self.document = (
            copy.deepcopy(previous[0])
            if previous
            else {
                "schema_version": 1,
                "database_key": _database_key(db.path),
                "updated_at": _now(),
                "revision": 0,
                "latest": None,
                "last_full": None,
                "last_export": None,
            }
        )
        if "last_full_format_export" not in self.document:
            prior = self.document["last_export"]
            self.document["last_full_format_export"] = copy.deepcopy(
                prior
                if prior and prior["phases"]["export"].get("format", "full") == "full"
                else None
            )
        self.state = {
            "attempt_id": uuid.uuid4().hex,
            "run_id": run_id,
            "mode": "skipped_fast" if fast else "full",
            "collection_status": row["status"],
            "collection_finished_at": row["finished_at"],
            "started_at": _now(),
            "phases": {
                name: {
                    "status": "unresolved",
                    "started_at": None,
                    "finished_at": None,
                    "error": None,
                }
                for name in PHASES
            },
        }
        if not signals:
            self.state["phases"]["signals"]["status"] = "disabled"
        if fast:
            self.state["phases"]["export"]["status"] = "disabled"
        if export_dir is not None:
            self.state["phases"]["export"].update(
                format=export_mode,
                directory=str(target_directory(export_dir, export_mode).resolve()),
                receipt=None,
            )
        self._persist()

    def _persist(self, *, failing=False):
        self.document["latest"] = copy.deepcopy(self.state)
        if self.state["mode"] == "full":
            self.document["last_full"] = copy.deepcopy(self.state)
        self.document["updated_at"] = _now()
        self.document["revision"] += 1
        exceptions = BaseException if failing else Exception
        try:
            if not self.db.conn.in_transaction:
                with self.db.conn:
                    self.db.conn.execute(
                        "INSERT OR REPLACE INTO monitor_state VALUES (?,?)",
                        (KEY, json.dumps(self.document)),
                    )
        except exceptions:
            pass
        try:
            self.writer(self.db.path.parent / "publication-attempt.json", self.document)
        except exceptions:
            pass

    def phase(self, name, callback):
        phase = self.state["phases"][name]
        phase.update(
            status="inprogress", started_at=_now(), finished_at=None, error=None
        )
        try:
            self._persist()
            value = callback()
            if name == "export" and phase.get("format") == "compact":
                receipt = (
                    value.get("export_artifacts") if isinstance(value, dict) else None
                )
                if not valid_receipt(
                    self.db.conn,
                    self.db.path,
                    receipt,
                    phase["directory"],
                    self.state["run_id"],
                    timestamp(phase["started_at"]),
                    timestamp(_now()),
                ):
                    raise ValueError(
                        "Compact export returned an invalid artifact receipt"
                    )
                phase["receipt"] = copy.deepcopy(receipt)
        except BaseException as error:
            phase.update(
                status="failed",
                finished_at=_now(),
                error=_safe_error(error),
            )
            try:
                self._persist(failing=True)
            except BaseException:  # noqa: BLE001,S110 - Never mask the original phase failure.
                pass
            raise
        disabled = (
            name == "signals"
            and isinstance(value, dict)
            and value.get("status") == "not_configured_for_model_profile"
        )
        phase.update(status="disabled" if disabled else "success", finished_at=_now())
        if name == "export":
            self.document["last_export"] = copy.deepcopy(self.state)
            if phase.get("format", "full") == "full":
                self.document["last_full_format_export"] = copy.deepcopy(self.state)
        self._persist()
        return value


def report(db):
    """Read only operator metadata. Success here is not source freshness/coverage."""
    docs = _documents(db)
    latest = db.conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    full = db.conn.execute(
        "SELECT id FROM runs WHERE kind IN ('live','full') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    attempt = next(
        (
            doc["latest"]
            for doc in docs
            if latest and doc["latest"]["run_id"] == latest["id"]
        ),
        None,
    )
    full_attempt = next(
        (
            doc["last_full"]
            for doc in docs
            if full and doc["last_full"] and doc["last_full"]["run_id"] == full["id"]
        ),
        None,
    )
    export = next(
        (
            doc["last_export"]
            for doc in docs
            if doc["last_export"]
            and doc["last_export"]["mode"] == "full"
            and doc["last_export"]["phases"]["export"]["status"] == "success"
        ),
        None,
    )
    flat_export = next(
        (
            state
            for doc in docs
            for state in [doc.get("last_full_format_export", doc["last_export"])]
            if state
            and state["mode"] == "full"
            and state["phases"]["export"]["status"] == "success"
            and state["phases"]["export"].get("format", "full") == "full"
        ),
        None,
    )
    status = _status(attempt)
    if full and _status(full_attempt) != "success":
        status = (
            _status(full_attempt) if _status(full_attempt) == "failed" else "unresolved"
        )
    if (
        status == "success"
        and full
        and (export is None or export["run_id"] != full["id"])
    ):
        status = "unresolved"
    last_run = _file_json(db.path.parent / "last-run.json", maximum=1048576)
    last_id = last_run.get("run_id") if isinstance(last_run, dict) else None
    if (
        latest
        and last_id == latest["id"]
        and (attempt is None or attempt["phases"]["last_run"]["status"] != "success")
    ):
        # A visible current file alone cannot certify a completed callback.
        last_id = None
    return {
        "status": status,
        "latest_attempt": attempt,
        "latest_full_attempt": full_attempt,
        "last_successful_export": export,
        "last_successful_full_export": flat_export,
        "export_format": export["phases"]["export"].get("format", "full")
        if export
        else None,
        "export_directory": export["phases"]["export"].get("directory")
        if export
        else None,
        "export_snapshot_at": (export["phases"]["export"].get("receipt") or {}).get(
            "snapshot_captured_at"
        )
        if export
        else None,
        "flat_full_export_matches_latest_full_attempt": bool(
            full and flat_export and flat_export["run_id"] == full["id"]
        ),
        "matches_latest_full_attempt": bool(
            full and export and export["run_id"] == full["id"]
        ),
        "last_successful_invocation_run_id": last_id
        if type(last_id) is int and last_id > 0
        else None,
        "note": "Collection mode full is distinct from export format. last_successful_full_export refers only to the full file format. Phase outcomes describe operator calls, not current source facts or an atomic generation; absent path/snapshot metadata remains unknown.",
    }
