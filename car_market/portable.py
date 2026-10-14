"""Portable operator interface. Every search owns its workspace and database."""

import argparse
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from contextlib import closing, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .configuration import ADAPTERS, load, preflight
from .db import Database, now
from .export_formats import run_export, target_directory, validate_mode
from .full_schedule import record_attempt, remaining
from .infra_support import FileLock
from .publication import Publication, atomic_json
from .publication import report as publication_report
from .search_profile import GENERIC_PROFILE
from .text_encoding import json_text

EXIT_PARTIAL = 3
EXIT_BUSY = 4
EXIT_UNHEALTHY = 5


def package_version():
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("car-market")
    except PackageNotFoundError:
        return "unknown"


def emit(value):
    print(json_text(value, indent=2), flush=True)


def show(args, kind, value):
    """Readable summary in an interactive terminal; JSON for --json, pipes and files."""
    if getattr(args, "json", False) or not sys.stdout.isatty():
        emit(value)
    else:
        from . import human

        print(getattr(human, kind)(value), flush=True)


def initialize(args, *, announce=True):
    from .configuration import validate
    from .search_profile import normalized

    fallback = None
    if args.url:
        urls = args.url
    else:
        # The model route narrows the search to this model. AutoScout24 answers
        # an unknown model slug with 404; the make route is the fallback.
        def route(*parts):
            return (
                "https://www.autoscout24."
                + args.country.lower()
                + "/lst/"
                + "/".join(normalized(p).replace(" ", "-") for p in parts)
                + "?"
                + urlencode(
                    {
                        "cy": "D" if args.country == "DE" else "NL",
                        "atype": "C",
                        "sort": "age",
                        "desc": "1",
                        "damaged_listing": "include",
                        "ocs_listing": "include",
                    }
                )
            )

        urls = [route(args.make, args.model)]
        fallback = route(args.make)
    selector = {
        "make": args.make,
        "make_aliases": args.make_alias,
        "model_terms": [[args.model, *args.model_alias], *[[v] for v in args.require]],
        "exclude_terms": args.exclude,
        "generation_terms": args.generation,
        "fuels": args.fuel,
        "transmissions": args.transmission,
    }
    for key in ["year_from", "year_to"]:
        if getattr(args, key) is not None:
            selector[key] = getattr(args, key)
    queries = []
    for index, url in enumerate(urls):
        source = next(
            (s for s, a in ADAPTERS.items() if urlsplit(url).hostname in a["hosts"]),
            None,
        )
        if source is None:
            raise ValueError("No supported adapter for search URL: " + url)
        if source in {"autoscout24", "autoscout24_de"}:
            parsed = urlsplit(url)
            params = parse_qs(parsed.query, keep_blank_values=True)
            if "cy" not in params:
                params["cy"] = ["D" if args.country == "DE" else "NL"]
            params.pop("page", None)
            url = urlunsplit(parsed._replace(query=urlencode(params, doseq=True)))
        if source == "marktplaats" and urlsplit(url).path != "/lrp/api/search":
            parsed = urlsplit(url)
            path = re.sub(r"/p/\d+/?$", "/", parsed.path)
            url = urlunsplit(parsed._replace(path=path))
        pagination = ADAPTERS[source]["pagination"]
        if source == "marktplaats" and urlsplit(url).path == "/lrp/api/search":
            parsed = urlsplit(url)
            params = parse_qs(parsed.query, keep_blank_values=True)
            params["offset"] = ["0"]
            url = urlunsplit(parsed._replace(query=urlencode(params, doseq=True)))
            pagination = "marktplaats_api"
        query = {
            "source": source,
            "key": "search_" + str(index + 1),
            "url": url,
            "pagination": pagination,
            "coverage": "enumerate",
            "infer_missing": False,
        }
        if fallback:
            query["fallback_url"] = fallback
        queries.append(query)
    config = validate(
        {
            "schema_version": 1,
            "scope": {
                "model_profile": GENERIC_PROFILE,
                "country": args.country,
                "selector": selector,
            },
            "queries": queries,
            "channels": [],
            "workers": 2,
            "max_pages": args.max_pages,
            "detail_limit": args.detail_limit,
            "detail_batch_limit": args.detail_limit,
            "fast_detail_limit": min(20, args.detail_limit),
            "detail_cache_hours": 24,
            "detail_error_backoff_hours": 6,
            "request_delay": 1.5,
            "request_timeout": 25,
            "interval_hours": 6,
            "fast_interval_minutes": 30,
            "fast_sources": list(dict.fromkeys(q["source"] for q in queries)),
            "rdw_limit": 0,
        }
    )
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    path = Path(args.config).resolve() if args.config else workspace / "search.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: rerunning init cannot silently overwrite a friend's existing search.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if announce:
        emit(
            {
                "status": "initialized",
                "config": str(path),
                "workspace": str(workspace),
                "search_urls": [query["url"] for query in queries],
                "note": "Review search URLs and aliases before collecting. Page budget may produce a partial result.",
            }
        )
    return 0


def paths(args):
    root = Path(args.workspace).resolve()
    config_path = Path(args.config).resolve() if args.config else root / "search.json"
    config = load(config_path)
    db_path = root / "data" / "market.sqlite3"
    preflight(db_path, config)
    return root, config, db_path


def heartbeat(root, state, **extra):
    atomic_json(
        root / "data" / "heartbeat.json",
        {"at": now(), "pid": os.getpid(), "state": state, **extra},
    )


def collect(args, root, config, db_path, *, fast=False, stopped=None, automatic=False):
    from .configured_collect import ConfiguredCollector

    export_mode = validate_mode(getattr(args, "export_mode", "full"))
    db = None
    with FileLock(Path(str(db_path) + ".lock"), blocking=False):
        preflight(db_path, config)
        try:
            db = Database(db_path)
            db.conn.execute(
                "UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'",
                (now(),),
            )
            db.conn.commit()
            if automatic:
                fast = remaining(db.conn, config) > 0
            if not fast:
                record_attempt(db.conn, config, now())
            collector = ConfiguredCollector(
                db, config, browser=args.browser, stopped=stopped
            )
            result = collector.run(
                args.sources or (config.get("fast_sources") if fast else None),
                not args.no_details,
                False,
                fast=fast,
            )
            publication = Publication(
                db,
                result["run_id"],
                fast=fast,
                writer=atomic_json,
                export_mode=export_mode,
                export_dir=root / "exports",
            )
            if not fast:
                publication.phase(
                    "export", lambda: run_export(db, root / "exports", export_mode)
                )
            result.update(completed_at=now(), kind="fast" if fast else "full")
            publication.phase(
                "last_run", lambda: atomic_json(root / "data" / "last-run.json", result)
            )
            result["publication"] = publication.state
            return result
        finally:
            if db is not None:
                db.conn.close()


def daemon(args, root, config, db_path):
    stopped = threading.Event()
    old_handlers = {}

    def stop(_signum, _frame):
        stopped.set()

    for sig in [signal.SIGINT, signal.SIGTERM]:
        old_handlers[sig] = signal.signal(sig, stop)
    root.joinpath("data").mkdir(parents=True, exist_ok=True)
    # A second daemon must exit rather than repeatedly wake on a busy database.
    with FileLock(root / "data" / "daemon.lock", blocking=False):
        reader = (
            closing(Database.read_only(db_path).conn)
            if db_path.exists()
            else nullcontext(None)
        )
        with reader as state:
            next_full = time.monotonic() + remaining(state, config)
        while not stopped.is_set():
            started = time.monotonic()
            config = load(Path(args.config) if args.config else root / "search.json")
            preflight(db_path, config)
            reader = (
                closing(Database.read_only(db_path).conn)
                if db_path.exists()
                else nullcontext(None)
            )
            with reader as state:
                delay = remaining(state, config)
            next_full = time.monotonic() + delay
            fast = delay > 0
            heartbeat(root, "collecting", kind="fast" if fast else "full")
            ticking = threading.Event()

            def tick():
                while not ticking.wait(20):
                    heartbeat(root, "collecting", kind="fast" if fast else "full")

            thread = threading.Thread(target=tick, daemon=True)
            thread.start()
            failure = None
            try:
                result = collect(
                    args,
                    root,
                    config,
                    db_path,
                    fast=fast,
                    stopped=stopped,
                    automatic=True,
                )
                fast = result.get("kind") == "fast"
                emit(result)
                if not fast:
                    next_full = started + config["interval_hours"] * 3600
            except BlockingIOError:
                emit({"status": "already_running"})
            except KeyboardInterrupt:
                if not stopped.is_set():
                    raise
            except Exception as error:
                # Network/source errors cannot erase committed history or kill scheduling.
                failure = str(error)
                if not fast:
                    next_full = started + config["interval_hours"] * 3600
                emit({"status": "error", "error": str(error), "at": now()})
            finally:
                ticking.set()
                thread.join(timeout=2)
            wait = max(
                30, config["fast_interval_minutes"] * 60 - (time.monotonic() - started)
            )
            deadline = time.monotonic() + wait
            while not stopped.is_set() and time.monotonic() < deadline:
                heartbeat(
                    root,
                    "error" if failure else "waiting",
                    next_full_seconds=max(0, next_full - time.monotonic()),
                    **({"last_error": failure} if failure else {}),
                )
                stopped.wait(min(20, max(0, deadline - time.monotonic())))
        heartbeat(root, "stopped")
    for sig, handler in old_handlers.items():
        signal.signal(sig, handler)
    return 0


def diagnostic(root, config, db_path, *, require_daemon=False):
    result = {"config": "valid", "database": "not_created", "daemon": "not_running"}
    # A valid read-only SQLite file is insufficient for an operational collector.
    # Permission checks are read-only; never touch a restored database to probe it.
    paths = {"workspace": root, "data": db_path.parent}
    if db_path.exists():
        paths["database"] = db_path
    for name in (
        "raw",
        "heartbeat.json",
        "last-run.json",
        "daemon.lock",
        db_path.name + ".lock",
        db_path.name + "-wal",
        db_path.name + "-shm",
    ):
        path = db_path.parent / name
        if path.exists():
            paths[name] = path
    result["write_permissions"] = {
        name: os.access(path, os.W_OK | (os.X_OK if path.is_dir() else 0))
        for name, path in paths.items()
        if path.exists()
    }
    if db_path.exists():
        db = Database.read_only(db_path)
        try:
            result["database"] = db.conn.execute("PRAGMA quick_check").fetchone()[0]
            result["foreign_key_errors"] = len(
                db.conn.execute("PRAGMA foreign_key_check").fetchall()
            )
            result["summary"] = db.summary()
            result["pending_discoveries"] = db.conn.execute(
                "SELECT COUNT(*) FROM monitor_state WHERE key LIKE 'pending:%'"
            ).fetchone()[0]
            result["publication"] = publication_report(db)
            result["publication_ok"] = result["publication"]["status"] == "success"
        finally:
            db.conn.close()
    heartbeat_path = root / "data" / "heartbeat.json"
    if heartbeat_path.exists():
        beat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        age = (datetime.now(UTC) - datetime.fromisoformat(beat["at"])).total_seconds()
        result["heartbeat"] = beat
        result["daemon"] = (
            "alive"
            if 0 <= age <= 90 and beat["state"] in {"collecting", "waiting"}
            else "stale_or_stopped"
        )
    result["healthy"] = (
        result["database"] in {"ok", "not_created"}
        and not result.get("foreign_key_errors")
        and all(result["write_permissions"].values())
    )
    if not all(result["write_permissions"].values()):
        result["permission_error"] = (
            "The collector cannot write its workspace. After restoring Docker data, "
            "stop this search and restore ownership/permissions for UID 10001; see docs/operations.md."
        )
    if require_daemon:
        result["healthy"] = result["healthy"] and result["daemon"] == "alive"
    result["market_complete"] = False
    if "publication" not in result:
        result["publication"] = {
            "status": "unresolved",
            "note": "No publication evidence yet.",
        }
        result["publication_ok"] = False
    result["note"] = (
        "Database/process health, publication outcomes and market coverage are separate; "
        "inspect publication, latest_sources and coverage."
    )
    return result


BROWSER_HELP = "Use the bundled Chromium for sources that need a browser"
NO_DETAILS_HELP = "Read search pages only and skip listing detail pages"
SOURCES_HELP = "Limit this run to these source IDs (see the sources command)"
JSON_HELP = "Print JSON (the default when output is not a terminal)"


def search_arguments(command, *, research=False):
    command.add_argument("--make", required=not research, help="Car make, e.g. Toyota")
    command.add_argument("--model", required=not research, help="Model, e.g. Corolla")
    command.add_argument(
        "--country",
        choices=["NL", "DE"],
        default=None if research else "NL",
        help="Market to search (default NL)",
    )
    for name, text in [
        ("make-alias", "Another spelling of the make; repeatable"),
        ("model-alias", "Another spelling of the model; repeatable"),
        ("require", "Word the listing's model must also contain, e.g. Touring; repeatable"),
        ("exclude", "Word in the model that rules a listing out, e.g. GR; repeatable"),
        ("generation", "Generation the listing must state, e.g. E210; repeatable"),
        ("url", "Public search URL copied from a supported site; repeatable"),
    ]:
        command.add_argument("--" + name, action="append", default=[], help=text)
    command.add_argument(
        "--fuel",
        action="append",
        default=[],
        choices=["petrol", "diesel", "electric", "hybrid", "lpg", "hydrogen"],
        help="Keep only this fuel; repeatable",
    )
    command.add_argument(
        "--transmission",
        action="append",
        default=[],
        choices=["manual", "automatic"],
        help="Keep only this transmission; repeatable",
    )
    command.add_argument("--year-from", type=int, help="First registration year, inclusive")
    command.add_argument("--year-to", type=int, help="Last registration year, inclusive")
    command.add_argument(
        "--max-pages",
        type=int,
        default=None if research else 15,
        help="Search page budget (default 15)",
    )
    command.add_argument(
        "--detail-limit",
        type=int,
        default=None if research else 50,
        help="Own detail budget; 0 skips details (default 50)",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Configurable car market search and monitoring"
    )
    parser.add_argument(
        "--workspace", default=os.environ.get("CAR_MARKET_WORKSPACE", "./workspace")
    )
    parser.add_argument("--config", help="Defaults to WORKSPACE/search.json")
    parser.add_argument("--version", action="version", version="car-market " + package_version())
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser(
        "init", help="Create a search without overwriting existing files"
    )
    search_arguments(init)
    research = sub.add_parser(
        "research",
        help="Collect a bounded search, export it and make an English report",
    )
    search_arguments(research, research=True)
    research.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="Separate search directory (also accepted before the command)",
    )
    research.add_argument(
        "--resume",
        action="store_true",
        help="Use this workspace's saved search without changing its selector or limits",
    )
    research.add_argument("--browser", action="store_true", help=BROWSER_HELP)
    research.add_argument("--no-details", action="store_true", help=NO_DETAILS_HELP)
    research.add_argument("--sources", nargs="+", help=SOURCES_HELP)
    research.add_argument(
        "--export-mode",
        choices=("full", "compact"),
        default="full",
        help="Compact omits history CSV and rich vehicle JSON (default: full)",
    )
    for name, text in [
        ("collect", "Run one collection pass now"),
        ("daemon", "Keep collecting on a schedule (used by docker compose up)"),
    ]:
        command = sub.add_parser(name, help=text)
        command.add_argument("--browser", action="store_true", help=BROWSER_HELP)
        command.add_argument("--no-details", action="store_true", help=NO_DETAILS_HELP)
        command.add_argument("--sources", nargs="+", help=SOURCES_HELP)
        command.add_argument(
            "--export-mode",
            choices=("full", "compact"),
            default="full",
            help="Compact omits history CSV and rich vehicle JSON, retaining complete SQLite (default: full)",
        )
    for name, text in [
        ("validate", "Check the saved search configuration"),
        ("status", "Show saved runs, listings and the last result"),
        ("coverage", "Compare collected listings with each source's own count"),
        ("quality", "Show which fields are filled, confirmed or contradictory"),
        ("sources", "List the supported sources"),
        ("discoveries", "Show listings that still need a check before they count"),
    ]:
        sub.add_parser(name, help=text).add_argument("--json", action="store_true", help=JSON_HELP)
    export = sub.add_parser("export", help="Write CSV, JSON and a SQLite copy to exports/")
    export.add_argument("--json", action="store_true", help=JSON_HELP)
    export.add_argument(
        "--export-mode",
        choices=("full", "compact"),
        default="full",
        help="Compact writes exports/compact with complete SQLite (default: full)",
    )
    doctor = sub.add_parser("doctor", help="Check the database, files and daemon health")
    doctor.add_argument("--json", action="store_true", help=JSON_HELP)
    doctor.add_argument(
        "--require-daemon", action="store_true", help="Fail if the daemon is not running"
    )
    listing = sub.add_parser("list", help="Show matching listings with prices")
    listing.add_argument("--json", action="store_true", help=JSON_HELP)
    listing.add_argument("--max-price", type=float, help="Hide listings above this price")
    listing.add_argument(
        "--include-pending", action="store_true", help="Also show unconfirmed discoveries"
    )
    listing.add_argument(
        "--include-stale", action="store_true", help="Also show listings not seen recently"
    )
    vehicle = sub.add_parser("vehicle", help="Show one vehicle with its full history")
    vehicle.add_argument("id", help="Vehicle ID (or its first 8 characters) from the list command")
    vehicle.add_argument("--json", action="store_true", help=JSON_HELP)
    backup = sub.add_parser("backup", help="Write a consistent copy of the database")
    backup.add_argument("--output", required=True, help="Backup file path")
    report = sub.add_parser(
        "report",
        help="Generate English editorial PDF/PNG analytics from database snapshots",
    )
    report.add_argument(
        "--db", help="Explicit SQLite database; otherwise use this workspace"
    )
    report.add_argument("--country", choices=["NL", "DE"])
    report.add_argument("--compare-db", help="A second country's SQLite database")
    report.add_argument(
        "--compare-workspace", help="An accessible second search workspace"
    )
    report.add_argument("--compare-country", choices=["NL", "DE"])
    report.add_argument("--output", help="Directory for timestamped report generations")
    report.add_argument("--title", help="Optional English model label")
    report.add_argument("--as-of", help="Non-future ISO timestamp with timezone")
    report.add_argument("--freshness-hours", type=float, default=13)
    report.add_argument("--min-cohort-size", type=int, default=5)
    report.add_argument("--mileage-bin-km", type=int, default=50000)
    report.add_argument("--price-floor", type=float, default=1)
    report.add_argument("--price-ceiling", type=float)
    report.add_argument("--json", action="store_true", help=JSON_HELP)
    args = parser.parse_args(argv)
    try:
        if args.command == "research":
            from .research_cli import run

            return run(
                args,
                initialize=initialize,
                paths=paths,
                collect=collect,
                partial_code=EXIT_PARTIAL,
                busy_code=EXIT_BUSY,
            )
        if args.command == "init":
            return initialize(args)
        if args.command == "sources":
            show(args, "sources", {s: {**a, "hosts": sorted(a["hosts"])} for s, a in ADAPTERS.items()})
            return 0
        if args.command == "report":
            from .analytics import generate
            from .model_profiles import bound_scope

            root = Path(args.workspace).resolve()
            primary = (
                Path(args.db).resolve() if args.db else root / "data/market.sqlite3"
            )
            if args.compare_db and args.compare_workspace:
                raise ValueError("Use either --compare-db or --compare-workspace")
            secondary = (
                Path(args.compare_db).resolve()
                if args.compare_db
                else (
                    Path(args.compare_workspace).resolve() / "data/market.sqlite3"
                    if args.compare_workspace
                    else None
                )
            )

            def country_of(path, fallback):
                db = Database.read_only(path)
                try:
                    return (bound_scope(db.conn) or {}).get("country", fallback)
                finally:
                    db.conn.close()

            country = args.country or country_of(primary, "NL")
            second_country = args.compare_country or (
                country_of(secondary, "DE" if country == "NL" else "NL")
                if secondary
                else "DE"
            )
            show(
                args,
                "report",
                generate(
                    primary,
                    country=country,
                    secondary=secondary,
                    secondary_country=second_country,
                    output=args.output or root / "reports",
                    title=args.title,
                    as_of=args.as_of,
                    freshness_hours=args.freshness_hours,
                    minimum=args.min_cohort_size,
                    mileage_bin=args.mileage_bin_km,
                    price_floor=args.price_floor,
                    price_ceiling=args.price_ceiling,
                )
            )
            return 0
        root, config, db_path = paths(args)
        if args.command in {"collect", "daemon"} and args.sources:
            configured = {
                q["source"] for q in config["queries"] if q.get("enabled", True)
            }
            if set(args.sources) - configured:
                raise ValueError(
                    "--sources contains a source absent from the search configuration"
                )
        if args.command == "validate":
            emit(
                {
                    "status": "valid",
                    "scope": config["scope"],
                    "queries": len(config["queries"]),
                }
            )
            return 0
        if args.command == "daemon":
            return daemon(args, root, config, db_path)
        if args.command == "collect":
            result = collect(args, root, config, db_path)
            emit(result)
            return 0 if result["status"] == "complete" else EXIT_PARTIAL
        if args.command in {"status", "doctor"}:
            result = diagnostic(
                root,
                config,
                db_path,
                require_daemon=getattr(args, "require_daemon", False),
            )
            show(args, "status", result)
            return 0 if result["healthy"] else EXIT_UNHEALTHY
        if not db_path.exists():
            raise ValueError("No database yet. Run collect first")
        if args.command == "export":
            with FileLock(Path(str(db_path) + ".lock"), blocking=False):
                db = Database(db_path)
                try:
                    from .configuration import bind

                    bind(db, config)
                    result = run_export(db, root / "exports", args.export_mode)
                    if isinstance(result, dict):
                        result = {**result, "directory": str(target_directory(root / "exports", args.export_mode))}
                    show(args, "export", result)
                finally:
                    db.conn.close()
            return 0
        db = Database.read_only(db_path)
        try:
            db.conn.execute("BEGIN")
            if args.command == "backup":
                target = Path(args.output).resolve()
                if target.exists() or target == db_path.resolve():
                    raise ValueError("Backup destination must be a new file")
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(target.name + ".tmp-" + str(os.getpid()))
                try:
                    with closing(sqlite3.connect(temp)) as destination:
                        db.conn.backup(destination)
                        if (
                            destination.execute("PRAGMA integrity_check").fetchone()[0]
                            != "ok"
                        ):
                            raise ValueError("Backup integrity check failed")
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
                emit(
                    {
                        "status": "backed_up",
                        "path": str(target),
                        "note": "Also copy data/raw to preserve retained response proofs.",
                    }
                )
            elif args.command == "discoveries":
                show(
                    args,
                    "discoveries",
                    [
                        json.loads(row[0])
                        for row in db.conn.execute(
                            "SELECT value FROM monitor_state WHERE key LIKE 'pending:%' ORDER BY key"
                        )
                    ]
                )
            elif args.command in {"list", "vehicle"}:
                from .vehicle_catalog import get_vehicle

                if args.command == "vehicle":
                    vehicle_id = args.id
                    if not db.conn.execute(
                        "SELECT 1 FROM vehicles WHERE id=?", (vehicle_id,)
                    ).fetchone() and len(vehicle_id) >= 6:
                        # The list table shows the first eight characters.
                        found = db.conn.execute(
                            "SELECT id FROM vehicles WHERE id LIKE ? LIMIT 2",
                            (vehicle_id.replace("%", "").replace("_", "") + "%",),
                        ).fetchall()
                        if len(found) == 1:
                            vehicle_id = found[0][0]
                    show(args, "vehicle", get_vehicle(db.conn, vehicle_id, config))
                else:
                    rows = [
                        dict(r)
                        for r in db.conn.execute(
                            "SELECT * FROM market_current ORDER BY price_eur IS NULL, price_eur, listing_id"
                        )
                    ]
                    if not args.include_pending:
                        rows = [r for r in rows if r["scope"] == "target"]
                    before_stale = len(rows)
                    if not args.include_stale:
                        from .provenance import timestamp

                        clock = datetime.now(UTC)
                        hours = config.get(
                            "freshness_hours", config.get("interval_hours", 6) * 2 + 1
                        )
                        rows = [
                            r
                            for r in rows
                            if r["availability"] == "observed"
                            and timestamp(r["last_seen"]) is not None
                            and 0
                            <= (clock - timestamp(r["last_seen"])).total_seconds()
                            <= hours * 3600
                        ]
                    if args.max_price is not None:
                        rows = [
                            r
                            for r in rows
                            if r["price_eur"] is not None
                            and 0 < r["price_eur"] <= args.max_price
                            and r["price_type"] in {"advertised_price", "FIXED"}
                        ]
                    show(
                        args,
                        "listings",
                        {
                            "advertisements": rows,
                            "count": len(rows),
                            "hidden_not_seen_recently": 0
                            if args.include_stale
                            else before_stale - len(rows),
                            "note": "Publication counts are not unique physical car counts; vehicle <uuid> shows merged evidence.",
                        }
                    )
            else:
                if args.command == "quality":
                    from .quality import report
                else:
                    from .coverage import report
                show(args, args.command, report(db.conn, config))
        finally:
            db.conn.close()
        return 0
    except BlockingIOError:
        emit(
            {
                "status": "already_running",
                "message": (
                    "Another collector is writing this search right now, usually the background "
                    "collector started with 'docker compose up -d'. It updates exports and data "
                    "itself after each pass. Wait for it, or stop it first "
                    "('docker compose -p PROJECT stop'), then run this command again."
                ),
            }
        )
        return EXIT_BUSY
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130
    except ImportError as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "message": (
                        "Report dependencies unavailable. In a source checkout: uv sync --extra reports. "
                        "For a downloaded wheel, install that wheel with its reports extra in the same environment: "
                        "python -m pip install '/path/to/car_market-VERSION-py3-none-any.whl[reports]'. "
                        "See docs/reports.md."
                    ),
                    "detail": str(error),
                }
            ),
            file=sys.stderr,
        )
        return 2
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as error:
        print(
            json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
