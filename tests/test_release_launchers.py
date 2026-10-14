"""Actual Bash entrypoint controls with a Docker boundary that cannot contact a daemon."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def launcher(tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("Bash is not installed on this host")
    root = tmp_path / "download with spaces"
    root.mkdir()
    source = Path(__file__).resolve().parents[1]
    shutil.copy2(source / "start.sh", root / "start.sh")
    shutil.copy2(source / "compose.yaml", root / "compose.yaml")
    commands = tmp_path / "docker.jsonl"
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(
        "#!" + sys.executable + "\n"
        "import json,os,pathlib,sys\n"
        "args=sys.argv[1:]\n"
        "with open(os.environ['DOCKER_PROOF'],'a') as f: f.write(json.dumps(args)+'\\n')\n"
        "if args[:2]==['compose','version']: sys.exit(0)\n"
        "if args[:2]==['volume','inspect']: sys.exit(1 if os.environ.get('DOCKER_VOLUME_MISSING') else 0)\n"
        "if args[0]=='cp':\n"
        " if os.environ.get('DOCKER_COPY_FAIL'): sys.exit(17)\n"
        " pathlib.Path(args[2],'report.pdf').write_bytes(b'copied report')\n"
        "sys.exit(int(os.environ.get('DOCKER_RUN_EXIT','0')) if 'run' in args and '--name' not in args else 0)\n"
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "DOCKER_PROOF": str(commands),
        "CAR_MARKET_PROJECT": "corolla-nl",
    }
    for name in ("DOCKER_RUN_EXIT", "DOCKER_COPY_FAIL", "DOCKER_VOLUME_MISSING"):
        env.pop(name, None)

    def run(*args, extra=None):
        result = subprocess.run(
            [bash, str(root / "start.sh"), *args],
            cwd=tmp_path,
            env={**env, **(extra or {})},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        calls = [json.loads(line) for line in commands.read_text().splitlines()]
        return result, calls

    return run, root


def test_no_arguments_show_help_and_cannot_start_monitor(launcher):
    run, root = launcher
    result, calls = run()
    assert result.returncode == 0
    assert calls[-1] == [
        "compose",
        "--project-name",
        "corolla-nl",
        "--file",
        str(root / "compose.yaml"),
        "run",
        "--rm",
        "--no-deps",
        "market",
        "--help",
    ]
    assert not any("daemon" in call or "up" in call for call in calls)


def test_partial_research_keeps_exact_arguments_and_exit_code(launcher):
    run, root = launcher
    argv = [
        "research",
        "--make",
        "Land Rover",
        "--model",
        "Range Rover",
        "--country",
        "NL",
        "--url",
        "https://example.invalid/search?q=a%20b&sort=age",
        "--no-details",
    ]
    result, calls = run(*argv, extra={"DOCKER_RUN_EXIT": "3"})
    assert result.returncode == 3
    assert calls[-1][-len(argv) :] == argv
    assert calls[-1][4] == str(root / "compose.yaml")
    assert len(calls) == 2


def test_reports_copy_uses_only_owned_stopped_container(launcher, tmp_path):
    run, _ = launcher
    output = tmp_path / "my reports"
    result, calls = run("reports", str(output))
    assert result.returncode == 0
    assert (output / "report.pdf").read_bytes() == b"copied report"
    create = calls[1]
    assert create[-4:] == ["--no-deps", "--entrypoint", "/bin/true", "market"]
    container = create[create.index("--name") + 1]
    assert container.startswith("car-market-report-copy-")
    assert calls[2] == ["cp", container + ":/workspace/reports/.", str(output)]
    assert calls[3] == ["rm", container]
    assert not any(
        "down" in call or "volume" in call or "--volumes" in call for call in calls
    )


def test_existing_output_is_preserved_without_copy_or_container(launcher, tmp_path):
    run, _ = launcher
    output = tmp_path / "existing reports"
    output.mkdir()
    original = output / "my-report.pdf"
    original.write_bytes(b"keep")
    result, calls = run("reports", str(output))
    assert result.returncode == 2
    assert original.read_bytes() == b"keep"
    assert calls == [["compose", "version"]]


def test_copy_error_is_not_success_and_owned_container_is_removed(launcher, tmp_path):
    run, _ = launcher
    result, calls = run(
        "reports", str(tmp_path / "failed copy"), extra={"DOCKER_COPY_FAIL": "1"}
    )
    assert result.returncode == 17
    assert "Report copy failed" in result.stderr
    assert calls[-1][0] == "rm"
    assert calls[-1][1] == calls[-2][1].split(":", 1)[0]


def test_compare_mounts_the_other_search_and_builds_one_report(launcher):
    run, _ = launcher
    result, calls = run("compare", "corolla-de", "--title", "Toyota Corolla")
    assert result.returncode == 0, result.stderr
    assert ["volume", "inspect", "corolla-de_market-data"] in calls
    run = calls[-1]
    assert run[run.index("--project-name") + 1] == "corolla-nl"
    assert run[run.index("--volume") + 1] == "corolla-de_market-data:/compare"
    assert run[-5:] == ["report", "--compare-workspace", "/compare", "--title", "Toyota Corolla"]
    assert "daemon" not in run


@pytest.mark.parametrize(
    "args,extra,message",
    [
        (["compare"], None, "Usage"),
        (["compare", "corolla-nl"], None, "must differ"),
        (["compare", "missing"], {"DOCKER_VOLUME_MISSING": "1"}, "No saved search named missing"),
    ],
)
def test_compare_explains_mistakes_without_running_a_container(launcher, args, extra, message):
    run, _ = launcher
    result, calls = run(*args, extra=extra)
    assert result.returncode == 2
    assert message in result.stderr
    assert not any("run" in call for call in calls)


def test_compare_uses_compose_lower_case_volume_names(launcher):
    run, _ = launcher
    result, calls = run("compare", "Corolla-DE")
    assert result.returncode == 0, result.stderr
    assert ["volume", "inspect", "corolla-de_market-data"] in calls
