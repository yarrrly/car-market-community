"""Exercise the installed candidate without accounts, network or market facts."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    volume = "car-market-release-smoke-" + uuid4().hex
    creation_attempted = False
    failure = None
    result = None
    commands = []
    expected_files = {
        str(path.relative_to("car_market")): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in Path("car_market").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert expected_files

    def command(argv, name):
        result = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=180
        )
        (out / (name + ".stdout")).write_text(result.stdout)
        (out / (name + ".stderr")).write_text(result.stderr)
        commands.append({"step": name, "exit_code": result.returncode})
        (out / "PARTIAL.json").write_text(json.dumps(commands, indent=2) + "\n")
        if result.returncode:
            raise RuntimeError(f"{name} failed with exit {result.returncode}")
        return result.stdout

    try:
        inspect = json.loads(
            command(["docker", "image", "inspect", args.image], "image")
        )[0]
        assert inspect["Architecture"] == args.architecture
        # Even a create whose response/receipt fails may have created this volume.
        creation_attempted = True
        command(
            [
                "docker",
                "volume",
                "create",
                "--label",
                "car-market.release-smoke=owned",
                volume,
            ],
            "volume",
        )
        run = [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--shm-size=1g",
            "-v",
            volume + ":/workspace",
        ]
        runtime = json.loads(
            command(
                run
                + [
                    "--entrypoint",
                    "python",
                    args.image,
                    "-c",
                    (
                        "import hashlib,json,os,pathlib,platform,subprocess; "
                        "from importlib.metadata import version,distributions; import car_market; "
                        "root=pathlib.Path(car_market.__file__).parent; "
                        "print(json.dumps({'uid':os.getuid(),'machine':platform.machine(),"
                        "'version':version('car-market'),'module':car_market.__file__,"
                        "'python':platform.python_version(),'uv':subprocess.check_output(['uv','--version'],text=True).strip(),"
                        "'packages':sorted((d.metadata['Name'],d.version) for d in distributions()),"
                        "'files':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() "
                        "for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}}))"
                    ),
                ],
                "runtime",
            )
        )
        assert runtime["uid"] == 10001
        assert runtime["version"] == args.expected_version
        assert runtime["files"] == expected_files
        assert runtime["machine"] in (
            {"x86_64", "AMD64"}
            if args.architecture == "amd64"
            else {"aarch64", "arm64"}
        )
        command(
            run
            + [
                "--entrypoint",
                "python",
                args.image,
                "-c",
                (
                    "import hashlib,json,pathlib; from playwright.sync_api import sync_playwright; "
                    "p=sync_playwright().start(); b=p.chromium.launch(); page=b.new_page(); "
                    "page.set_content('<title>release-ready</title>'); assert page.title()=='release-ready'; "
                    "exe=pathlib.Path(p.chromium.executable_path); "
                    "print(json.dumps({'version':b.version,'executable':str(exe),"
                    "'executable_sha256':hashlib.sha256(exe.read_bytes()).hexdigest()})); "
                    "b.close(); p.stop()"
                ),
            ],
            "browser",
        )
        cli = run + [args.image]
        command(cli + ["--help"], "help")
        command(
            cli
            + [
                "init",
                "--make",
                "Toyota",
                "--model",
                "Corolla",
                "--country",
                "NL",
                "--max-pages",
                "1",
                "--detail-limit",
                "0",
            ],
            "init",
        )
        command(cli + ["validate"], "validate")
        command(
            run
            + [
                "--entrypoint",
                "python",
                args.image,
                "-c",
                (
                    "from pathlib import Path; from car_market.db import Database; "
                    "from car_market.configuration import load,bind; "
                    "root=Path('/workspace'); db=Database(root/'data/market.sqlite3'); "
                    "bind(db,load(root/'search.json')); "
                    "assert db.conn.execute('SELECT COUNT(*) FROM listings').fetchone()[0]==0; "
                    "db.conn.close()"
                ),
            ],
            "empty-database",
        )
        command(cli + ["export"], "full-export")
        before = command(
            run
            + [
                "--entrypoint",
                "python",
                args.image,
                "-c",
                (
                    "import hashlib,json,pathlib,sqlite3; root=pathlib.Path('/workspace'); "
                    "db=sqlite3.connect(root/'data/market.sqlite3'); "
                    "print(json.dumps({'sql':list(db.iterdump()),'full':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() "
                    "for p in (root/'exports').iterdir() if p.is_file()}})); db.close()"
                ),
            ],
            "before-compact",
        )
        assert len(json.loads(before)["full"]) == 12
        receipt = json.loads(
            command(cli + ["export", "--export-mode", "compact"], "compact-export")
        )
        assert receipt["export_artifacts"]["mode"] == "compact"
        after = json.loads(
            command(
                run
                + [
                    "--entrypoint",
                    "python",
                    args.image,
                    "-c",
                    (
                        "import hashlib,json,pathlib,sqlite3; root=pathlib.Path('/workspace'); "
                        "db=sqlite3.connect(root/'data/market.sqlite3'); "
                        "compact=root/'exports/compact'; "
                        "artifacts=sorted(p.name for p in compact.iterdir() if p.is_file()); "
                        "snap=sqlite3.connect(compact.joinpath('market.sqlite3').as_uri()+'?mode=ro&immutable=1',uri=True); "
                        "print(json.dumps({'sql':list(db.iterdump()),'snapshot':list(snap.iterdump()),"
                        "'full':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'exports').iterdir() if p.is_file()},"
                        "'compact':artifacts})); db.close(); snap.close()"
                    ),
                ],
                "after-compact",
            )
        )
        prior = json.loads(before)
        assert after["sql"] == prior["sql"] == after["snapshot"]
        assert after["full"] == prior["full"]
        assert len(after["compact"]) == 11
        assert (
            "price-history.csv" not in after["compact"]
            and "vehicles.json" not in after["compact"]
        )
        command(
            cli
            + [
                "report",
                "--db",
                "/workspace/exports/compact/market.sqlite3",
                "--country",
                "NL",
                "--output",
                "/workspace/reports",
            ],
            "report",
        )
        reports = json.loads(
            command(
                run
                + [
                    "--entrypoint",
                    "python",
                    args.image,
                    "-c",
                    (
                        "import json,pathlib; root=pathlib.Path('/workspace/reports'); "
                        "files=[p for p in root.rglob('*') if p.is_file()]; "
                        "print(json.dumps({str(p.relative_to(root)):p.stat().st_size for p in files}))"
                    ),
                ],
                "report-files",
            )
        )
        assert any(n.endswith(".pdf") and size > 1000 for n, size in reports.items())
        assert any(n.endswith(".png") and size > 1000 for n, size in reports.items())
        command(cli + ["validate"], "persistent-volume-validate")
        result = {
            "status": "PASS",
            "image_id": inspect["Id"],
            "architecture": args.architecture,
            "runtime": runtime,
            "commands": commands,
            "reports": reports,
            "provider_HTTP": 0,
            "production_mounts": 0,
            "data_fixture": "empty initialized search",
            "limits": [
                "Installed-image browser/export/report/persistence smoke only.",
                "No fresh listings, national coverage, or desktop Docker host certificate.",
                "No collection or production upgrade was performed.",
            ],
        }
    except BaseException as error:
        failure = error
        raise
    finally:
        if creation_attempted:
            try:
                inspected = subprocess.run(
                    ["docker", "volume", "inspect", volume],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if inspected.returncode:
                    raise RuntimeError(
                        "Cannot confirm disposable volume ownership/removal: "
                        + inspected.stderr
                    )
                labels = json.loads(inspected.stdout)[0]["Labels"]
                assert (
                    volume.startswith("car-market-release-smoke-")
                    and labels.get("car-market.release-smoke") == "owned"
                )
                subprocess.run(
                    ["docker", "volume", "rm", volume], check=True, timeout=30
                )
            except BaseException as cleanup_error:
                if failure is None:
                    raise
                print(
                    "Disposable volume cleanup also failed: " + str(cleanup_error),
                    file=sys.stderr,
                )
    (out / "RESULT.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
