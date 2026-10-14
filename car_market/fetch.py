import dataclasses
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from sys import getsizeof
from urllib.parse import parse_qs, urlsplit

from .db import now
from .trovit_details import CAPTCHA_REFUSAL, trovit_captcha_refusal


def cookie_matches(cookie, url):
    """Apply host, path, transport and expiry bounds to anonymous session cookies."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    domain = str(cookie.get("domain") or "").lower()
    bare = domain.lstrip(".")
    if not bare or not (
        host == bare or domain.startswith(".") and host.endswith("." + bare)
    ):
        return False
    if cookie.get("secure") and parsed.scheme != "https":
        return False
    path = parsed.path or "/"
    required = cookie.get("path") or "/"
    if path != required and not (
        path.startswith(required)
        and (required.endswith("/") or path[len(required) :].startswith("/"))
    ):
        return False
    expiry = cookie.get("expires", -1)
    return expiry == -1 or isinstance(expiry, (int, float)) and expiry > time.time()


@dataclasses.dataclass
class Response:
    url: str
    final_url: str
    status: int | None
    body: str | None
    timestamp: str
    error: str | None = None
    transport: str = "http"


class Fetcher:
    def __init__(self, delay=0.7, timeout=25, browser=False, locale="nl-NL"):
        self.delay, self.timeout, self.browser = delay, timeout, browser
        self.locale = locale
        self._last = {}
        self._lock = threading.Lock()
        self._cookies = []
        self._cache = OrderedDict()
        self._cache_sizes = {}
        self._cache_bytes = 0
        self._cache_limit_bytes = 24 * 1024 * 1024
        self._cache_limit_entries = 128
        self._anwb_headers = None

    def _forget_cached(self, key):
        self._cache.pop(key, None)
        self._cache_bytes -= self._cache_sizes.pop(key, 0)

    def _remember_response(self, response, request_body=None):
        """Bound retained bodies; callers may annotate their own response independently."""
        if response.status == 200 and trovit_captcha_refusal(
            response.body, response.final_url
        ):
            response = dataclasses.replace(response, error=CAPTCHA_REFUSAL)
        body_key = (
            json.dumps(request_body, sort_keys=True)
            if request_body is not None
            else None
        )
        size = sum(
            getsizeof(value)
            for value in (
                response,
                response.body,
                response.url,
                response.final_url,
                body_key,
            )
        )
        with self._lock:
            for url in dict.fromkeys((response.url, response.final_url)):
                key = (url, body_key) if body_key is not None else url
                self._forget_cached(key)
                if response.error or size > self._cache_limit_bytes:
                    continue
                self._cache[key] = dataclasses.replace(response)
                self._cache_sizes[key] = size
                self._cache_bytes += size
                while (
                    self._cache_bytes > self._cache_limit_bytes
                    or len(self._cache) > self._cache_limit_entries
                ):
                    self._forget_cached(next(iter(self._cache)))

    def _check_stopped(self):
        stopped = getattr(self, "stopped", None)
        if stopped is not None and stopped.is_set():
            raise KeyboardInterrupt("Collector stopping")

    def _throttle(self, url):
        self._check_stopped()
        host = urlsplit(url).hostname or ""
        with self._lock:
            delay = max(self.delay, 1.5) if "autowereld" in host else self.delay
            wait = max(0, self._last.get(host, 0) + delay - time.monotonic())
            self._last[host] = time.monotonic() + wait
        if wait:
            stopped = getattr(self, "stopped", None)
            if stopped is not None:
                if stopped.wait(wait):
                    raise KeyboardInterrupt("Collector stopping")
            else:
                time.sleep(wait)
        self._check_stopped()

    def get(self, url, refresh=False, request_body=None):
        self._check_stopped()
        cache_key = (
            (url, json.dumps(request_body, sort_keys=True))
            if request_body is not None
            else url
        )
        with self._lock:
            if refresh and cache_key in self._cache:
                final_url = self._cache[cache_key].final_url
                final_key = (
                    (final_url, cache_key[1])
                    if isinstance(cache_key, tuple)
                    else final_url
                )
                self._forget_cached(cache_key)
                self._forget_cached(final_key)
            cached = self._cache.get(cache_key) if not refresh else None
            if cached is not None:
                self._cache.move_to_end(cache_key)
        if cached:
            if cached.status == 200 and trovit_captcha_refusal(
                cached.body, cached.final_url
            ):
                with self._lock:
                    self._forget_cached(cache_key)
                    final_key = (
                        (cached.final_url, cache_key[1])
                        if isinstance(cache_key, tuple)
                        else cached.final_url
                    )
                    self._forget_cached(final_key)
                return dataclasses.replace(cached, url=url, error=CAPTCHA_REFUSAL)
            return dataclasses.replace(cached, url=url)
        if urlsplit(url).scheme not in ["https", "http"]:
            return Response(url, url, None, None, now(), "unsupported_scheme")
        host = urlsplit(url).netloc
        self._throttle(url)
        try:
            headers = {
                "User-Agent": "FirstCarMarket/0.1 (public vehicle market research)",
                "Accept-Language": self.locale
                + ","
                + self.locale.split("-")[0]
                + ";q=0.9,en;q=0.7",
            }
            with self._lock:
                cookies = [
                    c["name"] + "=" + c["value"]
                    for c in self._cookies
                    if cookie_matches(c, url)
                ]
            if cookies:
                headers["Cookie"] = "; ".join(cookies)
            data = None
            if host == "www.autofirst-nederland.nl" and urlsplit(url).path.startswith(
                "/api/v1/medium/occasions"
            ):
                headers.update(
                    Accept="application/json",
                    Referer="https://www.autofirst-nederland.nl/occasions",
                )
            if (
                host == "www.autohero.com"
                and urlsplit(url).path
                == "/v1/retail-customer-gateway/graphql/searchAdV9AdsV2"
            ):
                # Public scroll UI request, verified 06.10.2026: offsets 24/48/72, limit 24.
                page = int(parse_qs(urlsplit(url).query).get("pageNumber", [1])[0])
                headers["Content-Type"] = "application/json"
                data = json.dumps(
                    request_body
                    if request_body is not None
                    else {
                        "operationName": "searchAdV9AdsV2",
                        "variables": {
                            "search": {
                                "offset": (page - 1) * 24,
                                "limit": 24,
                                "sort": "most_popular",
                                "filter": {
                                    "field": "countryCode",
                                    "op": "eq",
                                    "value": "NL",
                                },
                                "aggs": [],
                                "postFilter": None,
                                "fields": [
                                    "registration",
                                    "firstEligibleToBePurchasedAt",
                                    "variablePositioning",
                                ],
                                "properties": {
                                    "firstPublishedDays": -30,
                                    "shuffleCategoryBResults": True,
                                    "resultsCombiner": "abbabbc",
                                    "filterByEligibleDate": True,
                                    "includeProspective": True,
                                },
                            }
                        },
                        "query": "query searchAdV9AdsV2($search: EsSearchRequestProjectionInput!, $tradeInId: UUID) {\n  searchAdV9AdsV2(search: $search, tradeInId: $tradeInId)\n}",
                    }
                ).encode()
            if (
                host == "api.anwb.nl"
                and "/occasion-portal/listings/search-with-counters" in url
            ):
                if self._anwb_headers is None:
                    self.anwb_session()
                headers.update(self._anwb_headers)
                data = json.dumps(
                    request_body
                    if request_body is not None
                    else {
                        "brandModel": {
                            "filter": [
                                {
                                    "slug": "bmw",
                                    "models": [{"slug": "2-serie-active-tourer"}],
                                }
                            ]
                        }
                    }
                ).encode()
            if not url.startswith(("http://", "https://")):
                raise ValueError("only HTTP(S) source URLs are allowed")
            request = urllib.request.Request(url, headers=headers, data=data)  # noqa: S310 — only HTTP(S) is allowed above
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 — HTTP(S) Request validated above
                content_type = response.headers.get("Content-Type", "")
                if not any(x in content_type for x in ["text", "json", "html", "xml"]):
                    return Response(
                        url,
                        response.url,
                        response.status,
                        None,
                        now(),
                        "non_text_response",
                    )
                raw = response.read(12_000_001)
                if len(raw) > 12_000_000:
                    return Response(
                        url,
                        response.url,
                        response.status,
                        None,
                        now(),
                        "response_too_large",
                    )
                body = raw.decode(
                    response.headers.get_content_charset() or "utf-8", errors="replace"
                )
                result = Response(url, response.url, response.status, body, now())
                if result.status == 200 and trovit_captcha_refusal(
                    result.body, result.final_url
                ):
                    result.error = CAPTCHA_REFUSAL
                    return result  # An explicit refusal never invokes browser fallback.
                if (
                    "myprivacy.dpgmedia.nl" in response.url
                    or "DPG Media Privacy Gate" in body
                ):
                    result.error = "consent_gate"
                if result.error and self.browser:
                    return self.browser_get(url)
                if not result.error:
                    self._remember_response(result, request_body)
                return result
        except urllib.error.HTTPError as error:
            # Do not try to bypass access-denied/captcha pages.
            body = None
            note = "http_" + str(error.code)
            try:
                raw = error.read(12_000_001)
                if len(raw) > 12_000_000:
                    note += ":response_too_large"
                elif raw:
                    encoding = (
                        error.headers.get_content_charset() if error.headers else None
                    )
                    body = raw.decode(encoding or "utf-8", errors="replace")
            except (AttributeError, LookupError, OSError, TypeError, ValueError):
                # Preserve the original HTTP status even when its body is unavailable.
                pass
            finally:
                error.close()
            return Response(url, error.url, error.code, body, now(), note)
        except Exception as error:  # noqa: BLE001 — log the source error and continue with the next source
            if self.browser and type(error).__name__ == "RemoteDisconnected":
                return self.browser_get(url)
            return Response(
                url, url, None, None, now(), type(error).__name__ + ": " + str(error)
            )

    def anwb_session(self):
        # Isolate the browser loop so Playwright failure does not disrupt the daemon thread.
        self._check_stopped()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(self._anwb_session).result()

    def _anwb_session(self):
        """Observe the site's anonymous public API request; keep its routing key in memory only."""
        self._check_stopped()
        if not self.browser:
            raise ValueError("ANWB public session requires browser support")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                **(
                    {"channel": os.environ["CAR_MARKET_BROWSER_CHANNEL"]}
                    if os.environ.get("CAR_MARKET_BROWSER_CHANNEL")
                    else {}
                ),
                headless=True,
                timeout=self.timeout * 1000,
            )
            try:
                page = browser.new_page(locale="nl-NL")
                with page.expect_request(
                    lambda r: (
                        "/occasion-portal/listings/search-with-counters" in r.url
                        and r.method == "POST"
                    ),
                    timeout=self.timeout * 1000,
                ) as request:
                    page.goto(
                        "https://www.anwb.nl/auto/kopen/zoeken/merk/bmw/model/2-serie-active-tourer",
                        wait_until="domcontentloaded",
                        timeout=self.timeout * 1000,
                    )
                headers = request.value.headers
                routing = headers.get("apikey")
                if not routing:
                    raise ValueError("ANWB anonymous request unavailable")
                self._anwb_headers = {
                    "apikey": routing,
                    "Content-Type": "application/json",
                    "Origin": "https://www.anwb.nl",
                    "Referer": "https://www.anwb.nl/",
                }
            finally:
                browser.close()

    def browser_get(self, url):
        if urlsplit(url).scheme not in {"https", "http"}:
            return Response(
                url, url, None, None, now(), "unsupported_scheme", "browser"
            )
        self._throttle(url)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(self._browser_get, url).result()

    def _browser_get(self, url):
        """A normal isolated Chrome session; no accounts, stealth or CAPTCHA solving."""
        try:
            self._check_stopped()
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    **(
                        {"channel": os.environ["CAR_MARKET_BROWSER_CHANNEL"]}
                        if os.environ.get("CAR_MARKET_BROWSER_CHANNEL")
                        else {}
                    ),
                    headless=True,
                    timeout=self.timeout * 1000,
                )
                try:
                    context = browser.new_context(locale=self.locale)
                    page = context.new_page()
                    page.set_default_timeout(self.timeout * 1000)
                    response = page.goto(
                        url, wait_until="domcontentloaded", timeout=self.timeout * 1000
                    )
                    page.wait_for_timeout(2000)
                    self._check_stopped()
                    for label in [
                        "Alle ablehnen",
                        "Nur notwendige Cookies",
                        "Ablehnen",
                        "Alles weigeren",
                        "Weiger alles",
                        "Alleen noodzakelijke cookies",
                        "Doorgaan zonder accepteren",
                        "Reject all",
                    ]:
                        button = page.get_by_role("button", name=label, exact=True)
                        if button.count() and button.first.is_visible():
                            button.first.click()
                            page.wait_for_timeout(2500)
                            break
                    result = Response(
                        url,
                        page.url,
                        response.status if response else None,
                        page.content(),
                        now(),
                        transport="browser",
                    )
                    if len(result.body.encode("utf-8")) > 12_000_000:
                        result.body = None
                        result.error = "response_too_large"
                    elif result.status is None:
                        result.error = "navigation_response_unavailable"
                    elif result.status >= 400:
                        result.error = "http_" + str(result.status)
                    elif trovit_captcha_refusal(result.body, result.final_url):
                        result.error = CAPTCHA_REFUSAL
                    elif (
                        "myprivacy.dpgmedia.nl" in page.url
                        or "DPG Media Privacy Gate" in result.body
                    ):
                        result.error = "consent_gate"
                    else:
                        with self._lock:
                            cookies = {
                                (c["domain"], c.get("path", "/"), c["name"]): c
                                for c in [*self._cookies, *context.cookies()]
                            }
                            self._cookies = list(cookies.values())[-256:]
                        self._remember_response(result)
                    return result
                finally:
                    browser.close()
        except Exception as error:  # noqa: BLE001 — log the source error and continue with the next source
            return Response(
                url,
                url,
                None,
                None,
                now(),
                "browser_unavailable: " + str(error),
                "browser",
            )
