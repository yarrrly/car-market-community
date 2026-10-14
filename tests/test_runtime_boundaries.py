"""Failure boundaries after adopting the shared runtime contracts."""

import urllib.error
from types import SimpleNamespace

import pytest
from car_market import cli
from car_market.fetch import Fetcher
from car_market.infra_support import FileLock


def test_http_429_is_preserved_without_retry_or_browser_bypass(monkeypatch):
    calls = []

    def blocked(request, **kwargs):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 429, "rate limit", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", blocked)
    fetcher = Fetcher(delay=0, browser=True)
    monkeypatch.setattr(
        fetcher, "browser_get", lambda _url: pytest.fail("HTTP restriction bypassed")
    )
    result = fetcher.get("https://example.test/listings")
    assert calls == ["https://example.test/listings"]
    assert result.status == 429
    assert result.error == "http_429"
    assert result.body is None


def test_failed_pass_releases_writer_lock_for_next_pass(tmp_path, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("source pass failed")

    monkeypatch.setattr(cli, "Collector", lambda *a, **kw: SimpleNamespace(run=failed))
    args = SimpleNamespace(
        db=tmp_path / "market.sqlite3",
        browser=False,
        sources=[],
        no_details=True,
        no_rdw=True,
        export_dir=tmp_path / "exports",
    )
    with pytest.raises(RuntimeError, match="source pass failed"):
        cli.collect_once(args, {})
    with FileLock(tmp_path / "market.sqlite3.lock", blocking=False):
        pass
