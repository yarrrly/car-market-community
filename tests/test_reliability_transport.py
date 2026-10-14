"""Access denial evidence and anonymous cookie boundaries; no external requests."""

import hashlib
import io
import urllib.error
from email.message import Message

import pytest

from car_market.db import Database
from car_market.fetch import Fetcher, cookie_matches


@pytest.mark.parametrize("status", [403, 429, 502])
def test_http_error_retains_original_body_and_status_for_evidence(tmp_path, monkeypatch, status):
    body = "<html>Access denied — synthetic response</html>".encode()
    headers = Message()
    headers["Content-Type"] = "text/html; charset=utf-8"
    url = "https://synthetic.invalid/search"

    def denied(*_args, **_kw):
        raise urllib.error.HTTPError(url, status, "synthetic error", headers, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", denied)
    response = Fetcher(delay=0, browser=True).get(url)
    assert response.status == status and response.error == f"http_{status}"
    assert response.body.encode() == body
    db = Database(tmp_path / "market.sqlite3")
    try:
        run = db.start_run("live")
        fetch = db.save_fetch(run, "autoscout24", url, url, status, response.body,
                              response.timestamp, error=response.error)
        row = db.conn.execute("SELECT * FROM fetches WHERE id=?", (fetch,)).fetchone()
        assert row["content_hash"] == hashlib.sha256(body).hexdigest()
        assert row["raw_path"] and row["error"] == f"http_{status}"
    finally:
        db.conn.close()


def test_oversized_denial_keeps_status_without_retaining_unbounded_body(monkeypatch):
    def denied(*_args, **_kw):
        raise urllib.error.HTTPError("https://synthetic.invalid", 403, "denied", Message(),
                                     io.BytesIO(b"x" * 12_000_001))

    monkeypatch.setattr("urllib.request.urlopen", denied)
    response = Fetcher(delay=0).get("https://synthetic.invalid")
    assert response.status == 403 and response.body is None
    assert response.error == "http_403:response_too_large"


@pytest.mark.parametrize("domain,path,secure,expires,url,allowed", [
    (".example.invalid", "/", True, -1, "https://example.invalid:443/a", True),
    (".example.invalid", "/", True, -1, "https://sub.example.invalid/a", True),
    ("example.invalid", "/", False, -1, "https://sub.example.invalid/a", False),
    (".example.invalid", "/", False, -1, "https://notexample.invalid/a", False),
    ("", "/", False, -1, "https://example.invalid/a", False),
    (".example.invalid", "/search", False, -1, "https://example.invalid/search/a", True),
    (".example.invalid", "/search", False, -1, "https://example.invalid/searching", False),
    (".example.invalid", "/search", False, -1, "https://example.invalid/", False),
    (".example.invalid", "/", True, -1, "http://example.invalid/a", False),
    (".example.invalid", "/", True, 50, "https://example.invalid/a", False),
    (".example.invalid", "/", True, 150, "https://example.invalid/a", True),
])
def test_session_cookie_is_bound_to_its_native_scope(monkeypatch, domain, path, secure, expires, url, allowed):
    monkeypatch.setattr("car_market.fetch.time.time", lambda: 100)
    assert cookie_matches({"domain": domain, "path": path, "secure": secure, "expires": expires}, url) is allowed
