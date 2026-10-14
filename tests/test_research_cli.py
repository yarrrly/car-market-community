"""Public research entrypoint: retained native evidence and explicit fault controls."""

import gzip
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from car_market import analytics, editorial_report, portable
from car_market.fetch import Fetcher, Response

FIXTURES = Path(__file__).parent / "fixtures/autoscout_query_binding"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())


def native(country="NL"):
    record = next(r for r in MANIFEST if r["case"] == country.lower() + "-corolla")
    wire = (FIXTURES / record["file"]).read_bytes()
    assert hashlib.sha256(wire).hexdigest() == record["fixture_sha256"]
    body = gzip.decompress(wire)
    assert hashlib.sha256(body).hexdigest() == record["decoded_sha256"]
    return record, body.decode()


@pytest.fixture
def offline(monkeypatch):
    calls = []
    state = {"country": "NL", "mode": "complete"}

    def get(_self, url, *args, **kwargs):
        calls.append(url)
        record, body = native(state["country"])
        if state["mode"] in {403, 429}:
            return Response(
                url, url, state["mode"], "Access refused", record["observed_at"]
            )
        if state["mode"] != "native":
            page = json.loads(body)
            props = page["props"]["pageProps"]
            props["listings"] = props["listings"][:1]
            if state["mode"] == "empty":
                props["listings"] = []
            if state["mode"] == "unknown":
                props["listings"][0]["location"] = {}
            props["numberOfResults"] = len(props["listings"])
            props["numberOfPages"] = 1
            body = json.dumps(page)
        return Response(
            url, url, 200, body, record["observed_at"], transport="offline_fixture"
        )

    def render(_analysis, stage):
        # Fault/control cases exercise real snapshot/analysis/manifest publication.
        # The native end-to-end case below restores the actual PDF/PNG renderer.
        for name in [
            "comparison-editorial.png",
            "comparison-editorial.pdf",
            "comparison-editorial-details.pdf",
        ]:
            (Path(stage) / name).write_bytes(b"offline renderer boundary")

    monkeypatch.setattr(Fetcher, "get", get)
    monkeypatch.setattr(
        Fetcher, "browser_get", lambda *_a, **_k: pytest.fail("Unexpected browser HTTP")
    )
    monkeypatch.setattr(editorial_report, "render", render)
    return state, calls


def arguments(root, country="NL", *extra, no_details=True):
    record, _ = native(country)
    return [
        "research",
        "--workspace",
        str(root),
        "--make",
        "Toyota",
        "--model",
        "Corolla",
        "--country",
        country,
        "--url",
        record["url"],
        "--max-pages",
        "1",
        "--detail-limit",
        "0",
        *(["--no-details"] if no_details else []),
        *extra,
    ]


def receipt(root):
    files = sorted((root / "data/research-receipts").glob("*/receipt.json"))
    assert files
    value = json.loads(files[-1].read_text())
    assert value["receipt"] == str(files[-1])
    return value


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_native_partial_keeps_ids_clocks_and_human_receipt(
    tmp_path, offline, capsys, country
):
    state, calls = offline
    state.update(country=country, mode="native")
    root = tmp_path / "new-search"
    assert portable.main(arguments(root, country)) == 3
    out = capsys.readouterr().out
    assert "Collection status: partial" in out
    assert "not a count of the whole national market" in out
    result = receipt(root)
    assert result["collection"]["status"] == "partial"
    assert result["collection_exit_code"] == result["exit_code"] == 3
    assert result["report"]["status"] == "generated"
    assert Path(result["report"]["directory"]).joinpath("manifest.json").exists()
    assert len(calls) == 1
    record, _ = native(country)
    with sqlite3.connect(root / "data/market.sqlite3") as conn:
        assert {r[0] for r in conn.execute("SELECT external_id FROM listings")} <= set(
            record["native_ids"]
        )
        assert conn.execute("SELECT DISTINCT fetched_at FROM fetches").fetchall() == [
            (record["observed_at"],)
        ]
        assert conn.execute(
            "SELECT DISTINCT observed_at FROM observations"
        ).fetchall() == [(record["observed_at"],)]
        assert conn.execute(
            "SELECT status FROM runs WHERE id=?", (result["collection"]["run_id"],)
        ).fetchone() == ("partial",)


def test_actual_english_pdf_entrypoint(tmp_path, offline, monkeypatch):
    offline[0]["mode"] = "native"
    # Retained facts keep their old clock; a newly made report must not freshen them.
    monkeypatch.undo()
    _state, calls = offline
    record, body = native()
    monkeypatch.setattr(
        Fetcher,
        "get",
        lambda _self, url, **_kw: (
            calls.append(url) or Response(url, url, 200, body, record["observed_at"])
        ),
    )
    root = tmp_path / "native-report"
    assert portable.main(arguments(root)) == 3
    result = receipt(root)
    report = Path(result["report"]["directory"])
    assert (report / "comparison-editorial.pdf").read_bytes().startswith(b"%PDF-")
    analysis = json.loads((report / "analysis.json").read_text())
    assert analysis["language"] == "en"
    assert (
        analysis["markets"]["NL"]["priced_ids"] == 0
    )  # Original Oct11 clock is stale.


@pytest.mark.parametrize(
    "mode,code", [("complete", 0), ("empty", 0), (403, 3), (429, 3), ("unknown", 3)]
)
def test_terminal_outcomes_are_not_promoted(tmp_path, offline, capsys, mode, code):
    offline[0]["mode"] = mode
    root = tmp_path / "outcome"
    assert portable.main(arguments(root)) == code
    text = capsys.readouterr().out
    result = receipt(root)
    assert result["exit_code"] == code
    if mode in {403, 429}:
        assert "refusal" in text.lower()
        assert result["counts"]["target"] == 0
        assert result["fetch_outcomes"][0]["status"] == mode
    if mode == "unknown":
        assert result["counts"]["target"] == 0
        assert result["counts"]["pending_discoveries_seen_this_run"] == 1
        assert "need confirmation" in text.lower()
    if mode == "empty":
        assert "not proof that the market is empty" in text


def test_resume_keeps_configuration_and_original_rows(tmp_path, offline):
    root = tmp_path / "resume"
    assert portable.main(arguments(root)) == 0
    original = (root / "search.json").read_bytes()
    with sqlite3.connect(root / "data/market.sqlite3") as conn:
        first = conn.execute(
            "SELECT id,observed_at,details_json FROM observations"
        ).fetchall()
    assert (
        portable.main(
            ["--workspace", str(root), "research", "--resume", "--no-details"]
        )
        == 0
    )
    assert (root / "search.json").read_bytes() == original
    with sqlite3.connect(root / "data/market.sqlite3") as conn:
        assert (
            conn.execute(
                "SELECT id,observed_at,details_json FROM observations WHERE id<=?",
                (first[-1][0],),
            ).fetchall()
            == first
        )
    assert len(list((root / "data/research-receipts").glob("*/receipt.json"))) == 2


def test_zero_details_budget_preserves_original_backlog_partial(tmp_path, offline):
    root = tmp_path / "cap-zero"
    assert portable.main(arguments(root, no_details=False)) == 3
    value = receipt(root)
    assert value["collection"]["collector_errors"] == ["detail_backlog:1"]
    assert value["collection"]["detail_sources"]["autoscout24"]["attempted"] == 0
    assert len(offline[1]) == 1


@pytest.mark.parametrize("extra", [[], ["--make", "Honda"], ["--max-pages", "2"]])
def test_existing_workspace_cannot_be_reconfigured_before_http(
    tmp_path, offline, extra
):
    root = tmp_path / "existing"
    assert portable.main(arguments(root)) == 0
    original = (root / "search.json").read_bytes()
    offline[1].clear()
    command = (
        arguments(root)
        if not extra
        else ["research", "--workspace", str(root), "--resume", *extra]
    )
    assert portable.main(command) == 2
    assert offline[1] == []
    assert (root / "search.json").read_bytes() == original


@pytest.mark.parametrize("code,mode", [(0, "complete"), (3, "native")])
def test_report_failure_is_separate_and_partial_code_preserved(
    tmp_path, offline, monkeypatch, capsys, code, mode
):
    offline[0]["mode"] = mode
    monkeypatch.setattr(
        analytics,
        "generate",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("report rename failed")),
    )
    root = tmp_path / "report-fault"
    assert portable.main(arguments(root)) == (2 if code == 0 else 3)
    value = receipt(root)
    assert value["collection_exit_code"] == code
    assert value["report"] == {
        "status": "failed",
        "error": "report rename failed",
        "exit_code": 2,
    }
    assert "Report failed: report rename failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "mode,stored_status,count", [("complete", "complete", 1), ("native", "partial", 18)]
)
def test_collection_failure_does_not_run_report(
    tmp_path, offline, monkeypatch, capsys, mode, stored_status, count
):
    offline[0]["mode"] = mode
    monkeypatch.setattr(
        portable,
        "run_export",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("export disk full")),
    )
    monkeypatch.setattr(
        analytics,
        "generate",
        lambda *_a, **_k: pytest.fail("Report after failed publication"),
    )
    root = tmp_path / "export-fault"
    assert portable.main(arguments(root)) == 2
    assert "export disk full" in capsys.readouterr().err
    value = receipt(root)
    assert value["report"]["status"] == "not_attempted"
    assert value["collection"]["status"] == "outcome_unavailable"
    assert value["collection"]["attempted"] is True
    assert "collection_exit_code" not in value
    assert value["error"] == {
        "phase": "Collection/export",
        "message": "export disk full",
    }
    record, _ = native()
    with sqlite3.connect(root / "data/market.sqlite3") as conn:
        assert conn.execute("SELECT status FROM runs").fetchall() == [(stored_status,)]
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone() == (count,)
        assert conn.execute(
            "SELECT DISTINCT observed_at FROM observations"
        ).fetchall() == [(record["observed_at"],)]


def test_invalid_limits_and_foreign_url_rejected_before_http(tmp_path, offline):
    root = tmp_path / "invalid"
    assert portable.main(arguments(root, "NL", "--max-pages", "0")) == 2
    assert not (root / "search.json").exists()
    assert offline[1] == []
    assert (
        portable.main(
            arguments(root, "NL", "--url", "https://www.autoscout24.de/lst/toyota?cy=D")
        )
        == 2
    )
    assert offline[1] == []


def test_existing_json_entrypoints_keep_their_format(tmp_path, offline, capsys):
    root = tmp_path / "legacy-cli"
    argv = arguments(root, no_details=False)[3:]  # Only init options.
    assert portable.main(["--workspace", str(root), "init", *argv]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "initialized"
    assert portable.main(["--workspace", str(root), "collect", "--no-details"]) == 0
    text = capsys.readouterr().out
    assert '"status": "complete"' in text and '"run_id"' in text


@pytest.mark.parametrize("code,mode", [(0, "complete"), (3, "native")])
def test_receipt_failure_is_visible_without_masking_collection(
    tmp_path, offline, monkeypatch, capsys, code, mode
):
    from car_market import research_cli

    offline[0]["mode"] = mode
    monkeypatch.setattr(
        research_cli,
        "atomic_json",
        lambda *_a: (_ for _ in ()).throw(OSError("receipt disk full")),
    )
    root = tmp_path / "receipt-fault"
    assert portable.main(arguments(root)) == (2 if code == 0 else 3)
    assert "Receipt could not be written: receipt disk full" in capsys.readouterr().err


def test_real_writer_contention_returns_busy_without_provider_http(tmp_path, offline):
    from car_market.infra_support import FileLock

    root = tmp_path / "busy"
    assert portable.main(arguments(root)) == 0
    offline[1].clear()
    with FileLock(root / "data/market.sqlite3.lock", blocking=False):
        assert portable.main(["research", "--workspace", str(root), "--resume"]) == 4
    assert offline[1] == []
    assert receipt(root)["report"]["status"] == "not_attempted"


def test_changed_db_binding_rejected_before_http(tmp_path, offline):
    root = tmp_path / "bound"
    assert portable.main(arguments(root)) == 0
    path = root / "search.json"
    changed = json.loads(path.read_text())
    changed["scope"]["selector"]["make"] = "Honda"
    path.write_text(json.dumps(changed))
    before = path.read_bytes()
    offline[1].clear()
    assert portable.main(["research", "--workspace", str(root), "--resume"]) == 2
    assert offline[1] == []
    assert path.read_bytes() == before


def test_workspace_symlink_and_unrelated_file_are_not_overwritten(tmp_path, offline):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert portable.main(arguments(alias)) == 2
    assert list(real.iterdir()) == []
    note = real / "keep.txt"
    note.write_bytes(b"unrelated existing evidence")
    assert portable.main(arguments(real)) == 2
    assert note.read_bytes() == b"unrelated existing evidence"
    assert offline[1] == []


def test_actual_module_entrypoint_partial_receipt_and_container_paths(tmp_path):
    root = tmp_path / "module-search"
    record, _ = native()
    script = """
import gzip,json,runpy,sys
from pathlib import Path
from car_market.fetch import Fetcher,Response
from car_market import editorial_report
record=json.loads(sys.argv[1])
body=gzip.decompress(Path(sys.argv[2]).read_bytes()).decode()
calls=[]
def get(self,url,*args,**kwargs):
    calls.append(url)
    return Response(url,url,200,body,record['observed_at'],transport='offline_fixture')
def render(analysis,stage):
    for name in ['comparison-editorial.png','comparison-editorial.pdf','comparison-editorial-details.pdf']:
        (Path(stage)/name).write_bytes(b'offline renderer boundary')
Fetcher.get=get
editorial_report.render=render
sys.argv=['car-market',*sys.argv[3:]]
try:
    runpy.run_module('car_market.portable',run_name='__main__')
finally:
    assert len(calls)==1
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            json.dumps(record),
            str(FIXTURES / record["file"]),
            *arguments(root),
        ],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3, result.stderr
    assert "Next:" in result.stdout and "research --resume" in result.stdout
    value = receipt(root)
    assert value["exit_code"] == 3
    assert Path(value["report"]["directory"]).is_dir()


def test_new_flow_saves_existing_default_limits_and_selector_choices(tmp_path, offline):
    root = tmp_path / "options"
    record, _ = native()
    argv = [
        "research",
        "--workspace",
        str(root),
        "--make",
        "Toyota",
        "--model",
        "Corolla",
        "--country",
        "NL",
        "--make-alias",
        "TOYOTA",
        "--model-alias",
        "Corolla Touring Sports",
        "--require",
        "Touring Sports",
        "--exclude",
        "Cross",
        "--fuel",
        "hybrid",
        "--transmission",
        "automatic",
        "--year-from",
        "2016",
        "--year-to",
        "2025",
        "--url",
        record["url"],
        "--no-details",
    ]
    assert portable.main(argv) == 0
    config = json.loads((root / "search.json").read_text())
    selector = config["scope"]["selector"]
    assert selector["make_aliases"] == ["TOYOTA"]
    assert selector["model_terms"] == [
        ["Corolla", "Corolla Touring Sports"],
        ["Touring Sports"],
    ]
    assert selector["fuels"] == ["hybrid"] and selector["transmissions"] == [
        "automatic"
    ]
    assert selector["year_from"] == 2016 and selector["year_to"] == 2025
    assert {
        k: config[k]
        for k in [
            "max_pages",
            "detail_limit",
            "interval_hours",
            "fast_interval_minutes",
            "request_delay",
            "detail_cache_hours",
            "detail_error_backoff_hours",
        ]
    } == {
        "max_pages": 15,
        "detail_limit": 50,
        "interval_hours": 6,
        "fast_interval_minutes": 30,
        "request_delay": 1.5,
        "detail_cache_hours": 24,
        "detail_error_backoff_hours": 6,
    }


def test_compact_is_opt_in_and_complete_snapshot_path_is_reported(tmp_path, offline):
    root = tmp_path / "compact"
    assert portable.main(arguments(root, "NL", "--export-mode", "compact")) == 0
    value = receipt(root)
    assert value["exports"]["mode"] == "compact"
    target = Path(value["exports"]["directory"])
    assert target == root / "exports/compact"
    assert (target / "market.sqlite3").is_file()
    assert not (target / "price-history.csv").exists()
    assert (
        json.loads((target / "export-manifest.json").read_text())["mode"] == "compact"
    )


def test_report_only_retry_does_not_collect_again(tmp_path, offline, monkeypatch):
    original = analytics.generate
    monkeypatch.setattr(
        analytics,
        "generate",
        lambda *_a, **_k: (_ for _ in ()).throw(ImportError("reports extra missing")),
    )
    root = tmp_path / "report-retry"
    assert portable.main(arguments(root)) == 2
    assert receipt(root)["collection_exit_code"] == 0
    monkeypatch.setattr(analytics, "generate", original)
    offline[1].clear()
    assert portable.main(["--workspace", str(root), "report"]) == 0
    assert offline[1] == []


def test_summary_error_keeps_original_partial_result(tmp_path, offline, monkeypatch):
    from car_market import research_cli

    offline[0]["mode"] = "native"
    monkeypatch.setattr(
        research_cli,
        "outcomes",
        lambda *_a: (_ for _ in ()).throw(
            sqlite3.OperationalError("summary unavailable")
        ),
    )
    root = tmp_path / "summary-fault"
    assert portable.main(arguments(root)) == 3
    value = receipt(root)
    assert value["collection"]["status"] == "partial"
    assert value["error"] == {"phase": "Summary", "message": "summary unavailable"}
