import argparse
import json
import pathlib
import sqlite3
import time
from datetime import UTC, datetime, timedelta

from .collect import Collector, load_config
from .db import Database, now
from .export_formats import run_export
from .full_schedule import record_attempt, remaining
from .infra_support import FileLock
from .parse import parse_page
from .publication import Publication, atomic_json

ROOT = pathlib.Path(__file__).resolve().parent.parent


def import_audit(db, directory):
    if db._bound_country() == "DE":
        raise ValueError("This importer has no German country proof")
    path = pathlib.Path(directory)
    manifests = {}
    for name in [
        "probe-results.json",
        "followup-results.json",
        "pagination-results.json",
        "corrected-routes.json",
        "all-market-probe.json",
    ]:
        if (path / name).exists():
            for record in json.loads((path / name).read_text()):
                manifests[record["source"]] = record
    run = db.start_run("audit_import")
    imported = 0
    source_map = {
        "gaspedaal": "gaspedaal",
        "autoscout": "autoscout24",
        "marktplaats": "marktplaats",
        "autotrack": "autotrack",
        "autouncle": "autouncle",
        "autotrader": "autotrader",
        "viabovag": "viabovag",
        "autohero": "autohero",
    }
    for name, record in manifests.items():
        source = next(
            (value for key, value in source_map.items() if name.startswith(key)), None
        )
        file = path / (name + ".html")
        if not source or not file.exists():
            continue
        body = file.read_text()
        timestamp = record["checked_at"]
        fetch_id = db.save_fetch(
            run,
            source,
            record["url"],
            record.get("final_url", record["url"]),
            record.get("status"),
            body,
            timestamp,
            "audit_import",
        )
        page = parse_page(source, body, record.get("final_url", record["url"]))
        for item in page.listings:
            if item.scope != "excluded":
                db.observe(run, item, timestamp, fetch_id)
                imported += 1
    db.candidates()
    db.finish_run(run, "imported", {"parsed_observations": imported})
    print(json.dumps(db.summary(), ensure_ascii=False, indent=2))


def collect_once(args, config):
    export_mode = getattr(args, "export_mode", "full")
    if export_mode not in ("full", "compact"):
        raise ValueError("Export mode must be full or compact")
    path = pathlib.Path(args.db).resolve()
    lock = FileLock(pathlib.Path(str(path) + ".lock"), blocking=False)
    try:
        lock.acquire()
    except BlockingIOError:
        print(json.dumps({"status": "already_running"}))
        return {"status": "already_running"}
    db = None
    try:
        from .germany import validate_market_path

        validate_market_path(path, config.get("scope", {}).get("country", "NL"))
        from .model_profiles import config_profile, validate_profile_path

        validate_profile_path(
            path, config.get("scope", {}).get("country", "NL"), config_profile(config)
        )
        db = Database(path)
        # The process lock is held: any prior unfinished writer has terminated.
        db.conn.execute(
            "UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'",
            (now(),),
        )
        db.conn.commit()
        from .mercedes_collect import MercedesCollector
        from .model_profiles import MERCEDES_PROFILE

        if getattr(args, "command", None) == "daemon":
            args.fast = remaining(db.conn, config) > 0
            args.sources = (
                config.get("fast_sources") if args.fast else args.full_sources
            )
        if not getattr(args, "fast", False):
            record_attempt(db.conn, config, now())
        collector_type = (
            MercedesCollector
            if config_profile(config) == MERCEDES_PROFILE
            else Collector
        )
        result = collector_type(db, config, browser=args.browser).run(
            args.sources,
            not args.no_details,
            not args.no_rdw,
            fast=getattr(args, "fast", False),
        )
        from .opportunities import publish

        publication = Publication(
            db,
            result["run_id"],
            fast=getattr(args, "fast", False),
            signals=True,
            writer=atomic_json,
            export_mode=export_mode,
            export_dir=args.export_dir,
        )
        publication.phase(
            "signals", lambda: publish(db, db.path.parent / "signals", config=config)
        )
        if not getattr(args, "fast", False):
            publication.phase(
                "export",
                lambda: (
                    db.export(args.export_dir, mode="compact")
                    if export_mode == "compact"
                    else db.export(args.export_dir)
                ),
            )
        publication.phase(
            "last_run", lambda: atomic_json(db.path.parent / "last-run.json", result)
        )
        result["publication"] = publication.state
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    finally:
        if db is not None:
            db.conn.close()
        lock.release()


def main():
    parser = argparse.ArgumentParser(
        description="BMW 218i Active Tourer country market observations"
    )
    parser.add_argument("--db", default=str(ROOT / "data" / "market.sqlite3"))
    parser.add_argument(
        "--config", default=str(ROOT / "tests/fixtures/legacy-config/sources.json")
    )
    parser.add_argument("--export-dir", default=str(ROOT / "exports"))
    parser.add_argument(
        "--export-mode",
        choices=("full", "compact"),
        default="full",
        help="Export format: compact omits history CSV and rich vehicle JSON; complete SQLite is preserved (default: full)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["collect", "daemon", "enrich"]:
        command = sub.add_parser(name)
        command.add_argument("--sources", nargs="+")
        command.add_argument("--no-details", action="store_true")
        command.add_argument("--no-rdw", action="store_true")
        command.add_argument("--browser", action="store_true")
    sub.add_parser("status")
    sub.add_parser("coverage")
    sub.add_parser("quality")
    sub.add_parser("vehicles")
    vehicle = sub.add_parser("vehicle")
    vehicle.add_argument("id", help="Canonical vehicle UUID or a preserved former ID")
    sub.add_parser("export")
    sub.add_parser("opportunities")
    sub.add_parser("reconcile-identities")
    importer = sub.add_parser("import-audit")
    importer.add_argument("directory")
    facebook = sub.add_parser("import-facebook")
    facebook.add_argument(
        "file", help="JSON observations supplied from normal Facebook access"
    )
    sub.add_parser("refresh-facebook")
    merger = sub.add_parser("import-db")
    merger.add_argument("snapshot")
    merger.add_argument(
        "--historical",
        action="store_true",
        help="Keep old evidence out of current purchase candidates",
    )
    query = sub.add_parser("query")
    query.add_argument("sql")
    args = parser.parse_args()
    if args.command in {
        "collect",
        "daemon",
        "enrich",
        "export",
        "import-db",
        "import-audit",
        "import-facebook",
        "refresh-facebook",
        "reconcile-identities",
    }:
        from .germany import validate_market_path
        from .model_profiles import config_profile, validate_profile_path

        scoped_config = load_config(args.config)
        profile = config_profile(scoped_config)
        country = scoped_config.get("scope", {}).get("country", "NL")
        validate_profile_path(args.db, country, profile)
        validate_profile_path(
            pathlib.Path(args.export_dir) / "market.sqlite3", country, profile
        )
        if args.command == "import-db":
            validate_profile_path(args.snapshot, country, profile)
        from .model_profiles import MERCEDES_PROFILE

        if profile == MERCEDES_PROFILE and args.command in {
            "import-audit",
            "import-facebook",
            "refresh-facebook",
        }:
            raise ValueError("This importer has no Mercedes model proof")
        country = load_config(args.config).get("scope", {}).get("country", "NL")
        validate_market_path(args.db, country)
        validate_market_path(pathlib.Path(args.export_dir) / "market.sqlite3", country)
        if args.command == "import-db":
            validate_market_path(args.snapshot, country)
        if country == "DE" and args.command in {
            "import-db",
            "import-facebook",
            "refresh-facebook",
            "import-audit",
        }:
            raise ValueError("This importer has no German country proof")
    if args.command in {"vehicles", "vehicle"}:
        from .vehicle_catalog import catalog, get_vehicle

        with sqlite3.connect(
            pathlib.Path(args.db).resolve().as_uri() + "?mode=ro", uri=True
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            config = load_config(args.config)
            result = (
                catalog(conn, config)
                if args.command == "vehicles"
                else get_vehicle(conn, args.id, config)
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "refresh-facebook":
        from .facebook_browser import refresh_known

        config = load_config(args.config)["facebook_refresh"]
        with FileLock(pathlib.Path(str(args.db) + ".lock"), blocking=False):
            db = Database(args.db)
            try:
                run = db.start_run("facebook_refresh")
                result = refresh_known(db, run, config)
                db.candidates()
                db.finish_run(run, result["status"], result)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            finally:
                db.conn.close()
        return
    if args.command == "import-facebook":
        from .facebook import import_observations

        document = json.loads(pathlib.Path(args.file).read_text())
        with FileLock(pathlib.Path(str(args.db) + ".lock"), blocking=False):
            db = Database(args.db)
            try:
                print(
                    json.dumps(
                        import_observations(db, document), ensure_ascii=False, indent=2
                    )
                )
            finally:
                db.conn.close()
        return
    if args.command == "import-db":
        import gzip
        import hashlib

        from .parse import Listing
        from .snapshot_identity import SnapshotIdentity

        path = pathlib.Path(args.snapshot).resolve()
        destination = pathlib.Path(args.db).resolve()
        if path == destination or (destination.exists() and path.samefile(destination)):
            raise ValueError("Snapshot and destination must be different databases")
        lock = FileLock(pathlib.Path(str(destination) + ".lock"), blocking=False)
        lock.acquire()
        db = None
        source = None
        try:
            historical_cutoff = (
                datetime.now(UTC)
                - timedelta(
                    hours=load_config(args.config).get("interval_hours", 6) * 2 + 1
                )
                if args.historical
                else None
            )
            db = Database(destination)
            if profile == MERCEDES_PROFILE:
                from .model_profiles import bind_profile

                bind_profile(db, scoped_config)
            source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            source.row_factory = sqlite3.Row
            # One snapshot for all tables; WAL writer verified in test_cli_snapshot_import_reads_one_source_version.
            source.execute("BEGIN")
            with db.transaction():
                identity = SnapshotIdentity(db, source, path)
                run = db.start_run("adapter_import")
                fetches = {}
                for row in source.execute("SELECT * FROM fetches"):
                    body = None
                    if row["raw_path"]:
                        relative = pathlib.Path(row["raw_path"])
                        if relative.is_absolute() or ".." in relative.parts:
                            raise ValueError(f"Unsafe raw path for fetch {row['id']}")
                        raw_file = (path.parent / relative).resolve()
                        if not raw_file.is_relative_to(path.parent):
                            raise ValueError(f"Unsafe raw path for fetch {row['id']}")
                        if raw_file.is_file():
                            with gzip.open(raw_file, "rb") as stream:
                                raw = stream.read()
                            if hashlib.sha256(raw).hexdigest() != row["content_hash"]:
                                raise ValueError(
                                    f"Raw content hash mismatch for fetch {row['id']}"
                                )
                            body = raw.decode("utf-8")
                    fetches[row["id"]] = db.save_fetch(
                        run,
                        row["source"],
                        row["url"],
                        row["final_url"],
                        row["status"],
                        body,
                        row["fetched_at"],
                        row["transport"],
                        row["error"],
                    )
                    if body is None and row["content_hash"]:
                        db.conn.execute(
                            "UPDATE fetches SET content_hash=? WHERE id=?",
                            (row["content_hash"], fetches[row["id"]]),
                        )
                identity.checks(source, fetches)
                imported_runs = {}

                def imported_run(original_id, timestamp, missing_key):
                    key = original_id if original_id is not None else missing_key
                    if key not in imported_runs:
                        original = source.execute(
                            "SELECT started_at FROM runs WHERE id=?", (original_id,)
                        ).fetchone()
                        imported_runs[key] = db.start_run(
                            "adapter_import",
                            original["started_at"] if original else timestamp,
                        )
                    return imported_runs[key]

                def comparable(payload):
                    # Local fetch IDs differ between databases; lineage is not a market change.
                    data = dict(payload)
                    data["raw"] = dict(data.get("raw", {}))
                    data["raw"].pop("field_evidence", None)
                    return data

                for row in source.execute(
                    "SELECT * FROM observations ORDER BY observed_at,id"
                ):
                    payload = json.loads(row["details_json"])
                    _, vehicle_id = identity.listing(row, payload)
                    for evidence in (
                        payload.get("raw", {}).get("field_evidence", {}).values()
                    ):
                        if (
                            isinstance(evidence, dict)
                            and evidence.get("fetch_id") is not None
                        ):
                            evidence["fetch_id"] = fetches.get(evidence["fetch_id"])
                    item = Listing(**payload)
                    existing = db.conn.execute(
                        """SELECT o.details_json FROM observations o JOIN listings l ON l.id=o.listing_id
                        WHERE l.source=? AND l.external_id=? AND o.observed_at=?""",
                        (item.source, item.external_id, row["observed_at"]),
                    ).fetchall()
                    if any(
                        comparable(json.loads(observation[0])) == comparable(payload)
                        for observation in existing
                    ):
                        continue
                    original_run = row["run_id"]
                    mapped_run = imported_run(
                        original_run, row["observed_at"], ("observation", row["id"])
                    )
                    listing_id = db.observe(
                        mapped_run,
                        item,
                        row["observed_at"],
                        fetches.get(row["fetch_id"]),
                        query_key=None
                        if args.historical
                        else (
                            "active_all" if item.source == "viabovag" else "218i_all"
                        ),
                        vehicle_id_hint=vehicle_id,
                        preserve_observation=True,
                    )
                    if historical_cutoff and listing_id:
                        current = db.conn.execute(
                            "SELECT last_seen,availability FROM listings WHERE id=?",
                            (listing_id,),
                        ).fetchone()
                        if (
                            current["availability"] == "observed"
                            and datetime.fromisoformat(current["last_seen"])
                            < historical_cutoff
                        ):
                            db.conn.execute(
                                "UPDATE listings SET availability='historical_only' WHERE id=?",
                                (listing_id,),
                            )
                identity.verifications(source, fetches)
                identity.quarantine(source, fetches)
                for row in source.execute(
                    "SELECT * FROM source_runs WHERE query_key<>'channel_status' ORDER BY started_at,id"
                ):
                    fields = (
                        "source",
                        "query_key",
                        "started_at",
                        "finished_at",
                        "status",
                        "expected_count",
                        "received_count",
                        "target_count",
                        "pages",
                        "error",
                    )
                    if db.conn.execute(
                        "SELECT 1 FROM source_runs WHERE "
                        + " AND ".join(field + " IS ?" for field in fields),
                        tuple(row[field] for field in fields),
                    ).fetchone():
                        continue
                    db.finish_source(
                        imported_run(
                            row["run_id"], row["started_at"], ("source_run", row["id"])
                        ),
                        row["source"],
                        row["query_key"],
                        row["started_at"],
                        row["status"],
                        row["expected_count"],
                        row["received_count"],
                        row["target_count"],
                        row["pages"],
                        row["error"],
                        infer_missing=False,
                        finished_at=row["finished_at"],
                    )
                for mapped_run in imported_runs.values():
                    db.finish_run(mapped_run, "imported", {"snapshot": str(path)})
                db.candidates()
                db.finish_run(run, "imported", db.summary())
            source.rollback()
            run_export(db, args.export_dir, getattr(args, "export_mode", "full"))
            print(json.dumps(db.summary(), ensure_ascii=False, indent=2))
        finally:
            if source is not None:
                source.close()
            if db is not None:
                db.conn.close()
            lock.release()
        return
    if args.command == "enrich":
        config = load_config(args.config)
        path = pathlib.Path(args.db).resolve()
        lock = FileLock(pathlib.Path(str(path) + ".lock"), blocking=False)
        lock.acquire()
        db = None
        try:
            db = Database(path)
            from .mercedes_collect import MercedesCollector
            from .model_profiles import MERCEDES_PROFILE, config_profile

            collector_type = (
                MercedesCollector
                if config_profile(config) == MERCEDES_PROFILE
                else Collector
            )
            collector = collector_type(db, config, browser=args.browser)
            run = db.start_run("enrichment")
            from .parse import Listing

            for record in db.conn.execute(
                "SELECT l.id,o.details_json FROM listings l JOIN latest_observations o ON o.listing_id=l.id"
            ):
                item = Listing(**json.loads(record["details_json"]))
                collector.queue_details(item, record["id"])
            if not args.no_details:
                collector.details(run)
            if not args.no_rdw:
                collector.rdw(run)
            publisher_bridges = None
            if collector.country == "DE":
                from .germany_identity import bridge_de_publications

                publisher_bridges = bridge_de_publications(db)
            db.candidates()
            summary = db.summary()
            if publisher_bridges is not None:
                summary["publisher_bridges"] = publisher_bridges
            status = (
                "partial"
                if collector.failures
                or (publisher_bridges is not None and publisher_bridges["conflicts"])
                or any(
                    row["run_id"] == run
                    and row["status"] not in ("complete", "zero_results")
                    for row in summary["latest_sources"]
                )
                else "complete"
            )
            db.finish_run(run, status, summary)
            run_export(db, args.export_dir, getattr(args, "export_mode", "full"))
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        finally:
            if db is not None:
                db.conn.close()
            lock.release()
        return
    if args.command in ["collect", "daemon"]:
        config = load_config(args.config)
        if args.command == "collect":
            collect_once(args, config)
        else:
            sources = args.sources
            args.full_sources = sources
            while True:
                config = load_config(args.config)
                started = time.monotonic()
                state = (
                    Database.read_only(args.db)
                    if pathlib.Path(args.db).exists()
                    else None
                )
                try:
                    delay = remaining(
                        state.conn if state else None,
                        config,
                    )
                finally:
                    if state:
                        state.conn.close()
                args.fast = delay > 0
                args.sources = config.get("fast_sources") if args.fast else sources
                try:
                    result = collect_once(args, config)
                except Exception as error:  # noqa: BLE001 — log the source error and continue with the next source
                    print(
                        json.dumps({"daemon_error": str(error), "at": now()}),
                        flush=True,
                    )
                wait = max(
                    30,
                    config.get("fast_interval_minutes", 5) * 60
                    - (time.monotonic() - started),
                )
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    time.sleep(min(10, deadline - time.monotonic()))
    elif args.command == "import-audit":
        import_audit(Database(args.db), args.directory)
    elif args.command == "opportunities":
        from .opportunities import evaluate

        db = Database.read_only(args.db)
        try:
            result = evaluate(db.conn, load_config(args.config))
        finally:
            db.conn.close()
        destination = pathlib.Path(args.export_dir) / "analysis"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "opportunities.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(
            json.dumps(
                {k: v for k, v in result.items() if k != "cars"},
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command in ["coverage", "quality"]:
        if args.command == "quality":
            from .quality import report
        else:
            from .coverage import report

        with sqlite3.connect(
            pathlib.Path(args.db).resolve().as_uri() + "?mode=ro", uri=True
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            print(
                json.dumps(
                    report(conn, load_config(args.config)), ensure_ascii=False, indent=2
                )
            )
    elif args.command == "status":
        db = Database.read_only(args.db)
        try:
            print(json.dumps(db.summary(), ensure_ascii=False, indent=2))
        finally:
            db.conn.close()
    elif args.command == "export":
        path = pathlib.Path(args.db).resolve()
        with FileLock(pathlib.Path(str(path) + ".lock"), blocking=False):
            db = Database(path)
            try:
                print(
                    json.dumps(
                        run_export(
                            db, args.export_dir, getattr(args, "export_mode", "full")
                        ),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            finally:
                db.conn.close()
    elif args.command == "reconcile-identities":
        from .identity_merge import bridge_shared_publications

        path = pathlib.Path(args.db).resolve()
        with FileLock(pathlib.Path(str(path) + ".lock"), blocking=False):
            db = Database(path)
            try:
                run = db.start_run("identity_reconciliation")
                if country == "DE":
                    from .germany_identity import bridge_de_publications

                    result = bridge_de_publications(db)
                else:
                    result = bridge_shared_publications(db)
                db.candidates()
                db.finish_run(
                    run, "partial" if result["conflicts"] else "complete", result
                )
                run_export(db, args.export_dir, getattr(args, "export_mode", "full"))
                print(json.dumps(result, ensure_ascii=False, indent=2))
            finally:
                db.conn.close()
    else:
        conn = sqlite3.connect(
            "file:" + str(pathlib.Path(args.db).resolve()) + "?mode=ro", uri=True
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        print(
            json.dumps(
                [dict(x) for x in conn.execute(args.sql)], ensure_ascii=False, indent=2
            )
        )


if __name__ == "__main__":
    main()
