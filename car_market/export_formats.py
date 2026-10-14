"""Explicit export formats and bounded operator receipts; never source facts."""

import hashlib
from pathlib import Path

from .model_profiles import BMW_PROFILE, bound_scope, validate_profile_path
from .provenance import timestamp

OMITTED = ("price-history.csv", "vehicles.json")
COMPACT_FILES = (
    "market-current.csv",
    "source-health.csv",
    "events.csv",
    "purchase-candidates.csv",
    "identity-candidates.csv",
    "rdw-checks.csv",
    "vehicle-id-aliases.csv",
    "field-verifications.csv",
    "vehicles.csv",
    "market.sqlite3",
)
MANIFEST = "export-manifest.json"
FULL_FILES = (*COMPACT_FILES, *OMITTED)


def validate_mode(mode):
    if type(mode) is not str or mode not in {"full", "compact"}:
        raise ValueError("Export mode must be full or compact")
    return mode


def target_directory(destination, mode):
    root = Path(destination)
    return root / "compact" if validate_mode(mode) == "compact" else root


def run_export(db, destination, mode="full"):
    # Preserve the existing positional call and result for default callers.
    return (
        db.export(destination)
        if validate_mode(mode) == "full"
        else db.export(destination, mode=mode)
    )


def validate_compact_namespace(full_target, target):
    root = Path(full_target)
    compact = Path(target)
    if compact.resolve() != root.resolve() / "compact" or (
        root.exists() and compact.exists() and compact.samefile(root)
    ):
        raise ValueError("Compact export must be separate from the full directory")
    for name in (*COMPACT_FILES, MANIFEST):
        selected = compact / name
        for retained in FULL_FILES:
            old = root / retained
            if selected.resolve() == old.resolve() or (
                selected.exists() and old.exists() and selected.samefile(old)
            ):
                raise ValueError("Compact output must not alias a retained full output")


def validate_compact_backup(db, target, full_target):
    validate_compact_namespace(full_target, target)
    if not valid_directory(str(Path(target).resolve()), "compact"):
        raise ValueError("Compact export must resolve to a compact directory")
    backup = Path(target) / "market.sqlite3"
    scope = bound_scope(db.conn)
    country = db._bound_country()
    if scope:
        validate_profile_path(backup, country, scope.get("model_profile", BMW_PROFILE))
    if country:
        from .germany import validate_market_path

        validate_market_path(backup, country)
    if backup.resolve() == db.path or (backup.exists() and backup.samefile(db.path)):
        raise ValueError("Export backup and working database must be different files")


def database_key(path):
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()


def compact_manifest(db, stage, target, names, as_of):
    latest = db.conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "schema_version": 1,
        "mode": "compact",
        "database_key": database_key(db.path),
        "scope": bound_scope(db.conn),
        "directory": str(Path(target).resolve()),
        "snapshot_latest_run_id": latest[0] if latest else None,
        "snapshot_captured_at": as_of,
        "derived_as_of": as_of,
        "omitted": list(OMITTED),
        "files": [
            {
                "name": name,
                "mode": "sqlite" if name == "market.sqlite3" else "csv",
                "bytes": (Path(stage) / name).stat().st_size,
            }
            for name in names
        ],
    }


def valid_directory(value, mode):
    if not isinstance(value, str) or not value:
        return False
    try:
        path = Path(value)
        return (
            path.is_absolute()
            and str(path.resolve()) == value
            and (mode != "compact" or path.name == "compact")
        )
    except (OSError, ValueError, RuntimeError):
        return False


def valid_receipt(conn, path, receipt, directory, run_id, start, finish):
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "mode",
        "database_key",
        "scope",
        "directory",
        "snapshot_latest_run_id",
        "snapshot_captured_at",
        "derived_as_of",
        "omitted",
        "files",
    }:
        return False
    if (
        type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["mode"] != "compact"
        or receipt["database_key"] != database_key(path)
        or receipt["scope"] != bound_scope(conn)
        or receipt["directory"] != directory
        or not valid_directory(directory, "compact")
        or type(receipt["snapshot_latest_run_id"]) is not int
        or receipt["snapshot_latest_run_id"] != run_id
        or receipt["omitted"] != list(OMITTED)
        or not isinstance(receipt["files"], list)
        or len(receipt["files"]) != len(COMPACT_FILES)
    ):
        return False
    try:
        captured = timestamp(receipt["snapshot_captured_at"])
    except OverflowError:
        return False
    if (
        captured is None
        or start is None
        or finish is None
        or not start <= captured <= finish
        or receipt["derived_as_of"] != receipt["snapshot_captured_at"]
    ):
        return False
    names = []
    for file in receipt["files"]:
        if not isinstance(file, dict) or set(file) != {"name", "mode", "bytes"}:
            return False
        name = file["name"]
        if (
            not isinstance(name, str)
            or name not in COMPACT_FILES
            or file["mode"] != ("sqlite" if name == "market.sqlite3" else "csv")
            or type(file["bytes"]) is not int
            or file["bytes"] < 0
        ):
            return False
        names.append(name)
    return len(set(names)) == len(COMPACT_FILES)
