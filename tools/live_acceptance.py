"""Opt-in Docker acceptance on fresh volumes, without accounts or imported data.

This measures exact source queries. It cannot measure a country's whole market.
Run sequentially and stop a source after an access denial. Retain volumes for
inspection; the printed cleanup command names only this run's volumes.
"""

import argparse
import json
import re
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

MODELS = {
    "corolla": ("Toyota", "Corolla", "toyota/corolla", []),
    "golf": ("Volkswagen", "Golf", "volkswagen/golf", ["--make-alias", "VW"]),
    "model-y": ("Tesla", "Model Y", "tesla/model-y", []),
    "bmw-218i": ("BMW", "218i", "bmw/218/ve_active-tourer", [
        "--model-alias", "218 i", "--require", "Active Tourer", "--exclude", "Gran Tourer",
    ]),
}
SOURCES = ("autoscout24", "autoscout24_de", "marktplaats", "gaspedaal", "autotrack")


def access_denied(errors, status_counts=None):
    """A result count containing 403/429 is not an HTTP access refusal."""
    statuses = status_counts or {}
    return any(statuses.get(str(code), 0) for code in (403, 429)) or bool(
        re.search(r"\bhttp(?:_|[ :]*)(?:403|429)\b|\b(?:captcha|challenge)\b",
                  errors, re.IGNORECASE)
    )


DATABASE_METRICS = """
import json,sys
from car_market.db import Database
db=Database.read_only('/workspace/data/market.sqlite3')
try:
    print(json.dumps({
        'quick_check': db.conn.execute('PRAGMA quick_check').fetchone()[0],
        'fetch_status_counts': {str(r[0]):r[1] for r in db.conn.execute(
            'SELECT status,COUNT(*) FROM fetches GROUP BY status')},
        'pending_country_records': db.conn.execute(
            "SELECT COUNT(*) FROM monitor_state WHERE key >= 'pending:' AND key < 'pending;'").fetchone()[0],
        'observed_publications': db.conn.execute('SELECT COUNT(*) FROM listings').fetchone()[0],
    }))
finally:
    db.conn.close()
"""


def search_url(source, key):
    make, model, route, _ = MODELS[key]
    if source.startswith("autoscout24"):
        country = "DE" if source.endswith("_de") else "NL"
        return f"https://www.autoscout24.{country.lower()}/lst/{route}?" + urlencode({
            "cy": "D" if country == "DE" else "NL", "atype": "C", "sort": "age",
            "desc": "1", "damaged_listing": "include", "ocs_listing": "include",
        })
    if source == "marktplaats":
        query = f"{make} {model}" + (" Active Tourer" if key == "bmw-218i" else "")
        return "https://www.marktplaats.nl/lrp/api/search?" + urlencode({
            "l1CategoryId": 91, "limit": 30, "offset": 0,
            "sortBy": "SORT_INDEX", "sortOrder": "INCREASING",
            "viewOptions": "list-view", "query": query,
            "searchInTitleAndDescription": "false",
        })
    brand, family = route.split("/")[:2]
    if key == "bmw-218i":
        family = "2-serie-active-tourer"
    if source == "gaspedaal":
        return f"https://www.gaspedaal.nl/{brand}/{family}" + (
            "/218i" if key == "bmw-218i" else ""
        )
    params = {
        "data.merkModel.filter.0.slug": brand,
        "data.merkModel.filter.0.models.0.slug": family,
        "pageSize": 30, "pageNumber": 1,
    }
    if key == "bmw-218i":
        params["data.trefwoord.filter.0.name"] = "218i"
    return "https://www.autotrack.nl/aanbod?" + urlencode(params)


def invoke(command, path, timeout, allowed=(0,)):
    started = time.monotonic()
    try:
        process = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=timeout)
        output, code = process.stdout, process.returncode
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        output += "\nAcceptance command exceeded its time budget.\n"
        code = 124
    path.write_text(output, encoding="utf-8")
    return {"exit_code": code, "seconds": round(time.monotonic() - started, 3),
            "expected_exit": code in allowed}


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="car-market:local")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default=uuid.uuid4().hex[:12])
    parser.add_argument("--sources", nargs="+", choices=SOURCES, default=list(SOURCES))
    parser.add_argument("--models", nargs="+", choices=list(MODELS), default=list(MODELS))
    parser.add_argument("--max-pages", type=int, choices=range(1, 201), default=3)
    parser.add_argument("--detail-limit", type=int, choices=range(0, 1001), default=8)
    parser.add_argument("--no-details", action="store_true")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--command-timeout", type=int, default=900)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", args.run_id):
        parser.error("run-id must contain lowercase letters, digits or hyphens (max 41)")
    if args.command_timeout < 30:
        parser.error("command-timeout must be at least 30 seconds")
    args.output.mkdir(parents=True, exist_ok=False)
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.image], text=True
    ).strip()
    rows, volumes, denied = [], [], set()
    failed = False
    for source in dict.fromkeys(args.sources):
        for key in dict.fromkeys(args.models):
            country = "DE" if source == "autoscout24_de" else "NL"
            row = {"source": source, "model": key, "country": country,
                   "started_at": datetime.now(UTC).isoformat(),
                   "image_id": image_id, "whole_market_coverage_percent": None}
            if source in denied:
                row["skipped"] = "source_access_denied_earlier_in_this_run"
                rows.append(row)
                continue
            volume = f"cm-accept-{args.run_id}-{source.replace('_', '-')}-{key}"
            # Never reuse an old dataset, even when a caller repeats the run-id.
            existing = subprocess.run(["docker", "volume", "inspect", volume],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if existing.returncode == 0:
                raise RuntimeError("Acceptance volume already exists: " + volume)
            subprocess.run(["docker", "volume", "create", "--label",
                            "car-market.acceptance=" + args.run_id, volume],
                           check=True, stdout=subprocess.DEVNULL)
            volumes.append(volume)
            folder = args.output / f"{source}-{key}"
            folder.mkdir()
            row.update(volume=volume, url=search_url(source, key))
            name = "cm-accept-" + args.run_id + "-command"
            prefix = ["docker", "run", "--rm", "--init", "--name", name,
                      "--shm-size", "1g", "-v", volume + ":/workspace", args.image]

            def command(label, options, allowed=(0,)):
                outcome = invoke(prefix + options, folder / (label + ".log"),
                                 args.command_timeout, allowed)
                if outcome["exit_code"] == 124:
                    # Stop the specific timed-out container; no Docker daemon prune.
                    subprocess.run(["docker", "stop", "--time", "30", name],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=45)
                row[label] = outcome
                return outcome["expected_exit"]

            make, model, _, extra = MODELS[key]
            ready = command("init", ["init", "--make", make, "--model", model,
                "--country", country, "--url", row["url"], "--max-pages",
                str(args.max_pages), "--detail-limit", str(args.detail_limit), *extra])
            if ready:
                ready = command("validate", ["validate"])
            if ready:
                options = ["collect"]
                if args.browser:
                    options.append("--browser")
                if args.no_details:
                    options.append("--no-details")
                ready = command("collect", options, (0, 3))
                for label in ("status", "coverage", "quality", "doctor"):
                    ready = command(label, [label]) and ready
                coverage = read_json(folder / "coverage.log")
                if coverage:
                    row["queries"] = coverage.get("queries", [])
                    errors = " ".join(str(q.get("error") or "") for q in row["queries"])
                    if access_denied(errors):
                        denied.add(source)
                quality = read_json(folder / "quality.log")
                if quality:
                    row["quality"] = [{k: v for k, v in item.items() if k not in
                        {"examples_missing_core_fields", "last_query_runs"}}
                        for item in quality.get("sources", [])]
                metrics = invoke(["docker", "run", "--rm", "--network", "none",
                    "--entrypoint", "python", "-v", volume + ":/workspace:ro",
                    args.image, "-c", DATABASE_METRICS], folder / "database.log", 60)
                row["database"] = read_json(folder / "database.log")
                ready = metrics["expected_exit"] and ready
                if row["database"]:
                    ready = row["database"]["quick_check"] == "ok" and ready
                    if access_denied("", row["database"]["fetch_status_counts"]):
                        denied.add(source)
            row.update(finished_at=datetime.now(UTC).isoformat(),
                       acceptance_commands_passed=ready)
            failed = failed or not ready
            rows.append(row)
            summary = {"image": args.image, "image_id": image_id,
                "run_id": args.run_id, "settings": {"max_pages": args.max_pages,
                "detail_limit": args.detail_limit, "details_enabled": not args.no_details,
                "browser": args.browser}, "cases": rows, "volumes": volumes,
                "basis": "Exact source queries; publication IDs, not unique vehicles or national market coverage."}
            (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            print(json.dumps(row), flush=True)
    # Include trailing skipped cases, even when the last source was denied.
    if rows:
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("Retained inspection volumes. Cleanup after saving required evidence:")
    print("docker volume rm " + " ".join(volumes))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
