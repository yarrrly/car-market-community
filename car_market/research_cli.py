"""Human-facing research wrapper; collection and evidence policies stay unchanged."""

import json
import os
import shlex
import sys
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from .db import Database
from .export_formats import target_directory
from .human import explain
from .publication import atomic_json

SEARCH_OPTIONS = (
    "make",
    "model",
    "country",
    "make_alias",
    "model_alias",
    "require",
    "exclude",
    "generation",
    "url",
    "fuel",
    "transmission",
    "year_from",
    "year_to",
    "max_pages",
    "detail_limit",
)


class Progress:
    """Render existing line-oriented collector progress without buffering a whole pass."""

    def __init__(self, stream):
        self.stream = stream
        self.pending = ""

    def write(self, text):
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            try:
                value = json.loads(line)
            except ValueError:
                self.stream.write(line + "\n")
                continue
            if isinstance(value, dict) and value.get("source") and value.get("status"):
                self.stream.write(f"  {value['source']}: {value['status']}")
                if value.get("received") is not None:
                    self.stream.write(f", {value['received']} listings read")
                self.stream.write("\n")
                errors = value.get("errors") or value.get("error")
                if isinstance(errors, str):
                    errors = [errors]
                for error in errors or []:
                    for note in explain(str(error)):
                        self.stream.write("    - " + note + "\n")
            elif isinstance(value, dict) and "details_total" in value:
                self.stream.write(
                    f"  listing details: {value.get('details_completed', 0)} of "
                    f"{value['details_total']} read"
                    + (f", {value['errors']} errors" if value.get("errors") else "")
                    + "\n"
                )
            else:
                self.stream.write(line + "\n")
        if len(self.pending) > 65536:
            self.stream.write(self.pending)
            self.pending = ""
        return len(text)

    def flush(self):
        self.stream.flush()

    def finish(self):
        if self.pending:
            self.stream.write(self.pending)
            self.pending = ""
        self.flush()


def workspace(args):
    root = Path(args.workspace).expanduser().absolute()
    if args.config:
        raise ValueError(
            "research uses WORKSPACE/search.json; use the existing collect/report commands for --config"
        )
    if root.is_symlink():
        raise ValueError("Research workspace must be a real directory, not a symlink")
    for name in [
        "search.json",
        "data",
        "data/market.sqlite3",
        "data/research-receipts",
        "exports",
        "reports",
    ]:
        if (root / name).is_symlink():
            raise ValueError("Research path must not be a symlink: " + str(root / name))
    if args.resume:
        if any(
            getattr(args, name) is not None and getattr(args, name) != []
            for name in SEARCH_OPTIONS
        ):
            raise ValueError(
                "--resume uses the saved selector and limits; omit search options or choose a new workspace"
            )
        if not (root / "search.json").is_file():
            raise ValueError(
                "No saved search to resume; provide --make, --model and --country for a new workspace"
            )
    else:
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise ValueError(
                "This search already exists. Repeat it with: research --resume. "
                + (
                    "For a different search, choose a new project name first, "
                    "e.g. CAR_MARKET_PROJECT=yaris-nl ./start.sh research ..."
                    if Path("/.dockerenv").exists()
                    else "For a different search, use a new --workspace directory."
                )
            )
        if not all([args.make, args.model, args.country]):
            raise ValueError(
                "A new research search needs --make, --model and --country NL or DE"
            )
        args.max_pages = 15 if args.max_pages is None else args.max_pages
        args.detail_limit = 50 if args.detail_limit is None else args.detail_limit
    args.workspace = str(root.resolve())
    return Path(args.workspace)


def outcomes(path, run):
    db = Database.read_only(path)
    try:
        db.conn.execute("BEGIN")
        counts = dict.fromkeys(["target", "needs_confirmation", "excluded"], 0)
        counts.update(
            dict(
                db.conn.execute(
                    "SELECT l.scope,COUNT(*) FROM listings l WHERE l.id IN "
                    "(SELECT listing_id FROM query_membership WHERE last_run_id=?) GROUP BY l.scope",
                    (run,),
                )
            )
        )
        counts["pending_discoveries_seen_this_run"] = db.conn.execute(
            "SELECT COUNT(*) FROM monitor_state m JOIN fetches f "
            "ON f.id=json_extract(m.value,'$.fetch_id') "
            "WHERE m.key LIKE 'pending:%' AND f.run_id=?",
            (run,),
        ).fetchone()[0]
        errors = [
            dict(row)
            for row in db.conn.execute(
                "SELECT source,status,error,COUNT(*) AS count FROM fetches WHERE run_id=? "
                "AND (status IN (403,429) OR error IS NOT NULL) GROUP BY source,status,error",
                (run,),
            )
        ]
        return counts, errors
    finally:
        db.conn.close()


def run(args, *, initialize, paths, collect, partial_code, busy_code):
    receipt = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "collection": {"status": "not_attempted"},
        "report": {"status": "not_attempted"},
    }
    root = None
    ready = False
    code = 2
    stage = "Search setup"
    try:
        root = workspace(args)
        if not args.resume:
            initialize(args, announce=False)
        root, config, db_path = paths(args)
        if args.sources and set(args.sources) - {
            q["source"] for q in config["queries"] if q.get("enabled", True)
        }:
            raise ValueError("--sources contains a source absent from the saved search")
        ready = True
        selector = config["scope"]["selector"]
        print(
            f"Research: {selector['make']} / {' | '.join(selector['model_terms'][0])} / {config['scope']['country']}"
        )
        print("Workspace: " + str(root))
        print(
            f"Limits: {config.get('max_pages', 'default')} search pages per query, "
            f"{config.get('detail_limit', 'default')} listing detail pages per pass "
            "(saved in search.json)."
        )
        print(
            "Collecting search pages"
            + (
                " only…"
                if args.no_details or config.get("detail_limit") == 0
                else " and listing details…"
            )
        )
        stage = "Collection/export"
        receipt["collection"] = {
            "status": "outcome_unavailable",
            "attempted": True,
            "note": "Collection/export was invoked but has not returned an outcome. Inspect saved status before recollecting.",
        }
        progress = Progress(sys.stdout)
        try:
            with redirect_stdout(progress):
                result = collect(args, root, config, db_path)
        finally:
            progress.finish()
        receipt.update(
            collection=result,
            collection_exit_code=0 if result["status"] == "complete" else partial_code,
        )
        code = receipt["collection_exit_code"]
        stage = "Summary"
        counts, errors = outcomes(db_path, result["run_id"])
        print(
            "Collection status: "
            + result["status"]
            + (
                " (normal: a pass stops at its page and detail limits; the saved sample is usable)."
                if result["status"] == "partial" and sum(counts.values())
                else " (nothing was saved; see the notes above)."
                if not sum(counts.values())
                else "."
            )
        )
        print("This is a sample of the listings, not a count of the whole national market.")
        for error in result.get("collector_errors", []):
            for note in explain(str(error)):
                print("  - " + note)
        receipt.update(counts=counts, fetch_outcomes=errors)
        print(
            f"Listings seen: {counts['target']} match your search; "
            f"{counts['needs_confirmation']} need confirmation (model, country or details not yet "
            f"checked); {counts['excluded']} excluded."
        )
        if counts["pending_discoveries_seen_this_run"]:
            print(
                f"Discoveries waiting for a check: {counts['pending_discoveries_seen_this_run']}."
            )
        print("Counts are listings, not unique cars: one car can appear more than once.")
        for error in errors:
            label = "Access refusal" if error["status"] in (403, 429) else "Fetch error"
            print(
                f"{label}: {error['source']} HTTP {error['status']} ({error['count']} captures); {error['error'] or 'source refused access'}"
            )
        if not sum(counts.values()):
            print(
                "No stored publications were confirmed by this pass; this is not proof that the market is empty."
            )
        export_dir = target_directory(root / "exports", args.export_mode)
        receipt.update(
            workspace=str(root),
            config=str(root / "search.json"),
            database=str(db_path),
            exports={
                "mode": args.export_mode,
                "directory": str(export_dir),
                "publication": result["publication"],
            },
        )
        print("Exports: " + str(export_dir))
        stage = "Report"
        try:
            from .analytics import generate

            report = generate(
                db_path, country=config["scope"]["country"], output=root / "reports"
            )
            receipt["report"] = report
            print("English report directory: " + report["directory"])
            print("PDF: " + report["summary_pdf"])
        except Exception as error:  # noqa: BLE001 — preserve collection status when report publication fails
            receipt["report"] = {
                "status": "failed",
                "error": str(error),
                "exit_code": 2,
            }
            print("Report failed: " + str(error), file=sys.stderr)
            print(
                "Collection status and saved exports are retained. Retry the existing report command after fixing the error.",
                file=sys.stderr,
            )
            if code == 0:
                code = 2
    except KeyboardInterrupt:
        code = 130
        receipt["error"] = {"phase": stage, "message": "Interrupted"}
        print(stage + " interrupted; saved evidence is retained.", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 — terminal boundary records and displays the original phase error
        code = (
            busy_code
            if isinstance(error, BlockingIOError)
            else partial_code
            if receipt.get("collection_exit_code") == partial_code
            else 2
        )
        receipt["error"] = {"phase": stage, "message": str(error)}
        print(stage + " failed: " + str(error), file=sys.stderr)
    receipt.update(exit_code=code, finished_at=datetime.now(UTC).isoformat())
    receipt_written = False
    if ready:
        try:
            # A new directory for every attempt: no silent replacement of older receipts.
            directory = (
                root
                / "data/research-receipts"
                / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            )
            directory.mkdir(parents=True, exist_ok=False)
            path = directory / "receipt.json"
            receipt["receipt"] = str(path)
            atomic_json(path, receipt)
            receipt_written = True
            print("Receipt: " + str(path))
        except OSError as error:
            print("Receipt could not be written: " + str(error), file=sys.stderr)
            if code == 0:
                code = 2
    print(
        f"Exit code: {code}"
        + (
            " (partial collection; data and report are saved)."
            if code == partial_code and receipt_written
            else " (partial collection; receipt unavailable, inspect status/coverage and the errors above)."
            if code == partial_code
            else "."
        )
    )
    if ready and code in (0, partial_code):
        print(next_steps(root))
    return code


def next_steps(root=None):
    """Commands for the next step, in the form the user is most likely running."""
    if Path("/.dockerenv").exists():
        run = "./start.sh"
        note = "  (Windows: .\\start.ps1)"
    else:
        run, note = "car-market", ""
        default = Path(os.environ.get("CAR_MARKET_WORKSPACE", "./workspace")).absolute()
        if root is not None and Path(root).absolute() != default:
            run += " --workspace " + shlex.quote(str(root))
    return "\n".join(
        [
            "",
            "Next:" + ("  (the paths above are inside the container)" if note else ""),
            f"  copy the report to this computer:   {run} reports{note}" if note else "  open the PDF above",
            f"  see the matching listings:          {run} list",
            f"  what was and was not read:          {run} status",
            f"  collect again with the same search: {run} research --resume",
        ]
    )
