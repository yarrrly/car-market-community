"""Bounded public Mercedes stock contracts captured on 08.10.2026.

DE: native /vehicles, multipart /ajxvl and /vehicle. NL: Certified CGI,
Hedin vehiclelist + linked VDP, Van Mossel SSR. Domain or filters do not
establish country or generation. The parent profile controls qualification.
"""

import copy
import datetime as dt
import json
import math
import re
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .identity import normalized_vin
from .parse import Listing, Page, fuel, gearbox, model_year, number, plate

DE_HOST = "gebrauchtwagen.mercedes-benz.de"
NL_HOST = "certified.cars.mercedes-benz.nl"
HEDIN_HOST = "www.hedinautomotive.nl"
MOSSEL_HOST = "www.vanmossel.nl"
HEDIN_PATH = "/voorraad-api/vehiclelist/88/vehicles.json"
MAX_BODY = 12 * 1024 * 1024
MAX_ROWS = 2000


def _url(url):
    try:
        p = urlsplit(url)
        if (
            p.scheme != "https"
            or p.hostname not in {DE_HOST, NL_HOST, HEDIN_HOST, MOSSEL_HOST}
            or p.netloc not in {p.hostname, p.hostname + ":443"}
            or p.username is not None
            or p.password is not None
            or p.fragment
            or re.search(r"[\\\x00-\x20]", url)
            or "%" in p.path
            or ".." in p.path
            or {key.lower() for key in parse_qs(p.query)}
            & {"authorization", "token", "access_token", "password", "auth", "api_key"}
        ):
            return None
        return p
    except (ValueError, TypeError):
        return None


def _query(p):
    return parse_qs(p.query, keep_blank_values=True)


def mercedes_official_publication_id(url):
    """ID only from the publication detail URI; a query URL does not become an ad ID."""
    p = _url(url)
    if p is None:
        return None
    q = _query(p)
    if p.hostname == DE_HOST and p.path == "/vehicle":
        ids = q.get("vehicle", [])
        return ids[0] if len(ids) == 1 and re.fullmatch(r"[1-9]\d{4,11}", ids[0]) else None
    if p.hostname == HEDIN_HOST:
        m = re.fullmatch(
            r"/(?:mercedes-benz/)?(?:occasion|nieuw)/p/([1-9]\d{4,11})/[a-z0-9-]+/?", p.path
        )
    elif p.hostname == MOSSEL_HOST:
        m = re.fullmatch(r"/mercedes-benz/voorraad/([1-9]\d{4,11}-\d+)-[a-z0-9-]+/?", p.path)
    elif p.hostname == NL_HOST:
        m = re.fullmatch(r"/mercedes-benz/[a-z0-9-]+/[a-z0-9-]+/occ([1-9]\d{4,11})/?", p.path)
    else:
        m = None
    return m[1] if m else None


def _canonical(url):
    identifier = mercedes_official_publication_id(url)
    if identifier is None:
        return None
    p = _url(url)
    query = urlencode({"vehicle": identifier}) if p.hostname == DE_HOST else ""
    return urlunsplit(("https", p.hostname, p.path.rstrip("/"), query, ""))


def _route(url):
    p = _url(url)
    if p is None:
        return None
    if mercedes_official_publication_id(url):
        return "detail"
    if p.hostname == DE_HOST and p.path in {"/vehicles", "/ajxvl"}:
        return (
            "de_post"
            if p.path == "/ajxvl" and not p.query
            else "de_search"
            if p.path == "/vehicles"
            else None
        )
    if p.hostname == HEDIN_HOST and p.path == HEDIN_PATH:
        q = _query(p)
        if (
            q.get("webspaceKey") == ["mercedes-benz"]
            and q.get("locale") == ["nl"]
            and set(q.get("brand[]", [])) == {"Mercedes-Benz"}
        ):
            return "hedin_search"
    if p.hostname == MOSSEL_HOST and p.path == "/mercedes-benz/voorraad":
        return "mossel_search"
    if p.hostname == NL_HOST:
        if p.path == "/aanbod":
            return "nl_search"
        if p.path == "/cgi/voertuigen.cgi" and _query(p).get("site") == ["certified-cars"]:
            return "nl_search"
    return None


_SEARCH_KEYS = {
    "de_search": {
        "referrer",
        "page",
        "sorting",
        "modtyp",
        "bbodty",
        "gearty",
        "firrey[min]",
        "enginp",
        "view",
        "language",
        "geozip",
    },
    "hedin_search": {
        "limit",
        "sort",
        "webspaceKey",
        "locale",
        "brand[]",
        "model[Mercedes-Benz][]",
        "carrosserie[]",
        "brandstof[]",
        "transmissie[]",
        "page",
        "aanschafvormen[]",
        "aanschafvormen_private[]",
        "aanschafvormen_business[]",
    },
    "mossel_search": {"brand", "model", "bodystyle", "country", "type"},
    "nl_search": {
        "site",
        "zipcode_field",
        "perpage",
        "template",
        "sort",
        "sort_actieprijs",
        "locatie_voertuig",
        "dealer_id",
        "transmissiemerk",
        "carrosserie_soort",
        "model",
        "transmissie",
        "brandstof",
        "bouwjaar_van",
        "bouwjaar_tot",
        "prijs_range",
        "kmstand_range",
        "zipcode",
        "distance",
        "kleur_basis",
        "import_id",
        "keyword",
    },
}


def _safe_search_query(route, p):
    q = _query(p)
    return set(q) <= _SEARCH_KEYS.get(route, set()) and all(
        len(values) == 1
        or route == "hedin_search"
        and key == "brand[]"
        and set(values) == {"Mercedes-Benz"}
        for key, values in q.items()
    )


def mercedes_official_page_request(url, page=1, request_body=None):
    """Only observed routes/fields; POST uses multipart without authentication.

    DE request_body: fields from the captured form {page,type,q}. HTTP
    success=0 requires the native browser UI. NL Certified pagination remains
    unverified: the CGI depends on retained filter state.
    """
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 200:
        raise ValueError("invalid_mercedes_page")
    route = _route(url)
    p = _url(url)
    if route == "de_post":
        if (
            not isinstance(request_body, dict)
            or set(request_body) != {"page", "type", "q"}
            or request_body.get("type") != "vehiclelist"
        ):
            raise ValueError("own_mercedes_native_form_required")
        if (
            not isinstance(request_body.get("q"), str)
            or len(request_body["q"]) > 8192
            or _url("https://" + DE_HOST + "/vehicles?" + request_body["q"]) is None
            or not _safe_search_query(
                "de_search", urlsplit("https://" + DE_HOST + "/vehicles?" + request_body["q"])
            )
        ):
            raise ValueError("invalid_mercedes_native_query")
        fields = copy.deepcopy(request_body)
        fields["page"] = str(page)
        return {
            "method": "POST",
            "url": url,
            "form_data": fields,
            "encoding": "multipart/form-data",
            "transport": "browser_native_UI_or_verified_HTTP",
        }
    if route not in _SEARCH_KEYS or request_body is not None or not _safe_search_query(route, p):
        raise ValueError("invalid_mercedes_search_url")
    if route in {"mossel_search", "nl_search"} and page != 1:
        raise ValueError("mercedes_paging_contract_not_captured")
    q = _query(p)
    if len(q.get("page", [])) > 1:
        raise ValueError("ambiguous_mercedes_page")
    if route in {"de_search", "hedin_search"}:
        q["page"] = [str(page)]
    return {"method": "GET", "url": urlunsplit(p._replace(query=urlencode(q, doseq=True)))}


def _stock_post_error(body, page):
    try:
        document = json.loads(body.lstrip("\ufeff"))
        data = document.get("data")
        if (
            document.get("success") != 1
            or not isinstance(data, dict)
            or _int(data.get("count")) is None
            or data.get("page") != page
            or not isinstance(document.get("html"), str)
        ):
            return "native_response_not_inventory"
    except (ValueError, TypeError, AttributeError):
        return "native_response_not_inventory"
    return None


def _browser_official_get(request, timeout):
    from playwright.sync_api import sync_playwright

    from .db import now
    from .fetch import Response

    url = request["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                locale="de-DE" if _url(url).hostname == DE_HOST else "nl-NL"
            )
            page = context.new_page()
            if request["method"] == "GET":
                native = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                if (
                    native is None
                    or native.status != 200
                    or _route(page.url) is None
                    or _url(page.url).hostname != _url(url).hostname
                ):
                    return Response(
                        url,
                        page.url,
                        native.status if native else None,
                        None,
                        now(),
                        "public_browser_blocked_or_redirected",
                        "browser_native_UI",
                    )
                if _route(url) == "hedin_search":
                    # Chromium renders JSON inside <pre>; keep the native API body.
                    body = native.text()
                else:
                    page.wait_for_timeout(1200)
                    body = page.content()
                return Response(
                    url,
                    page.url,
                    native.status,
                    body if len(body) <= MAX_BODY else None,
                    now(),
                    None if len(body) <= MAX_BODY else "response_too_large",
                    "browser_native_UI",
                )
            fields = request["form_data"]
            ui_url = "https://" + DE_HOST + "/vehicles?" + fields["q"]
            native = page.goto(ui_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            if (
                native is None
                or native.status != 200
                or _url(page.url) is None
                or _url(page.url).hostname != DE_HOST
                or _route(page.url) != "de_search"
            ):
                return Response(
                    url,
                    page.url,
                    native.status if native else None,
                    None,
                    now(),
                    "public_browser_blocked_or_redirected",
                    "browser_native_UI",
                )
            page.wait_for_timeout(1200)
            target = int(fields["page"])
            button = page.locator('.list-page-link[data-page="' + str(target) + '"]').first
            if not button.count():
                return Response(
                    url,
                    url,
                    None,
                    None,
                    now(),
                    "own_native_paging_button_unavailable",
                    "browser_native_UI",
                )
            with page.expect_response(
                lambda response: response.url == url and response.request.method == "POST",
                timeout=timeout * 1000,
            ) as observed:
                button.evaluate("element => element.click()")
            response = observed.value
            body = response.text()
            error = (
                "http_" + str(response.status)
                if response.status != 200
                else _stock_post_error(body, target)
            )
            if len(body) > MAX_BODY:
                body, error = None, "response_too_large"
            return Response(
                url, response.url, response.status, body, now(), error, "browser_native_UI"
            )
        finally:
            browser.close()


class _OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        if _route(newurl) is None or _url(newurl).hostname != _url(request.full_url).hostname:
            raise ValueError("unsafe_official_redirect")
        return super().redirect_request(request, response, code, message, headers, newurl)


def fetch_mercedes_official(url, request_body=None, *, page=None, browser=False, timeout=30):
    """Separate Response getter: Fetcher.get cannot send Mercedes multipart POST.

    browser=True selects a fresh public UI in advance; HTTP failure does not
    trigger automatic retries. No saved cookies/profile or authentication.
    Detail GETs omit page/request_body; POSTs require native {page,type,q}.
    """
    from .db import now
    from .fetch import Response

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 1 <= timeout <= 60
    ):
        raise ValueError("invalid_official_timeout")
    if _route(url) == "detail" and request_body is None and page is None:
        request = {"method": "GET", "url": url}
    else:
        if page is None:
            value = (
                request_body.get("page")
                if isinstance(request_body, dict)
                else _query(_url(url)).get("page", ["1"])[0]
                if _url(url)
                else None
            )
            page = _int(value)
        request = mercedes_official_page_request(url, page, request_body)
    try:
        if browser:
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(_browser_official_get, request, timeout).result()
        headers = {
            "User-Agent": "FirstCarMarket/0.1 (public vehicle market research)",
            "Accept-Language": "de-DE" if _url(url).hostname == DE_HOST else "nl-NL",
        }
        data = None
        if request["method"] == "POST":
            boundary = "FirstCarMarket" + uuid.uuid4().hex
            data = (
                "".join(
                    "--"
                    + boundary
                    + '\r\nContent-Disposition: form-data; name="'
                    + key
                    + '"\r\n\r\n'
                    + value
                    + "\r\n"
                    for key, value in request["form_data"].items()
                )
                + "--"
                + boundary
                + "--\r\n"
            ).encode()
            headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        native_request = urllib.request.Request(  # noqa: S310 — own HTTPS route validated by helper
            request["url"], data=data, headers=headers, method=request["method"]
        )
        # Only the validated own HTTPS request and same-host redirects reach open.
        with urllib.request.build_opener(_OfficialRedirect()).open(
            native_request, timeout=timeout
        ) as response:
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                return Response(
                    url, response.url, response.status, None, now(), "response_too_large"
                )
            if _route(response.url) is None or _url(response.url).hostname != _url(url).hostname:
                return Response(
                    url, response.url, response.status, None, now(), "unsafe_official_redirect"
                )
            body = raw.decode("utf-8-sig")
            error = (
                _stock_post_error(body, int(request["form_data"]["page"]))
                if data is not None
                else None
            )
            return Response(url, response.url, response.status, body, now(), error)
    except urllib.error.HTTPError as error:
        return Response(url, error.url, error.code, None, now(), "http_" + str(error.code))
    except Exception as error:  # noqa: BLE001 — retain per-response HTTP/optional browser failures
        return Response(
            url,
            url,
            None,
            None,
            now(),
            type(error).__name__,
            "browser_native_UI" if browser else "http",
        )


def _dict(x):
    return x if isinstance(x, dict) else {}


def _text(x):
    return x.get_text(" ", strip=True) if x is not None else ""


def _int(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, int) and 0 <= x <= 10000000:
        return x
    if isinstance(x, str) and re.fullmatch(r"\d{1,8}", x):
        return int(x)
    return None


def _amount(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, dict):
        return _amount(x.get("value"))
    if isinstance(x, str) and (
        not re.fullmatch(r"(?:€\s*)?\d[\d.,\s\xa0]*(?:\s*€)?", x.strip())
        or re.search(r"\d[.,]\d{3}[.,]\d{3}[.,]", x)
    ):
        return None
    value = number(x)
    return (
        float(value)
        if isinstance(value, (int, float)) and math.isfinite(value) and 0 < value < 10000000
        else None
    )


def _date(x):
    if not isinstance(x, str):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", x):
        x = x[:10]
    parts = re.fullmatch(r"(\d{1,2})([.-])(\d{1,2})\2(\d{4})", x)
    if parts:
        x = f"{int(parts[4]):04d}-{int(parts[3]):02d}-{int(parts[1]):02d}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", x):
        return None
    try:
        return dt.date.fromisoformat(x).isoformat()
    except ValueError:
        return None


def _country(x):
    if isinstance(x, dict):
        x = x.get("name") or x.get("value")
    value = str(x or "").strip().upper()
    return {
        "DE": "DE",
        "DEU": "DE",
        "GERMANY": "DE",
        "DEUTSCHLAND": "DE",
        "NL": "NL",
        "NLD": "NL",
        "NEDERLAND": "NL",
        "NETHERLANDS": "NL",
        "BE": "BE",
        "BELGIUM": "BE",
        "BELGIË": "BE",
    }.get(value)


def _country_proof(identifier, explicit=None, address=None, phone=None, dealer=None):
    evidence = {
        "kind": "own_native_country",
        "native_value": explicit,
        "publication_id": identifier,
        "dealer": dealer,
        "address": address,
    }
    if explicit not in (None, ""):
        return _country(explicit), evidence
    if dealer and address and re.search(r"\b\d{4}\s?[A-Z]{2}\b", address):
        evidence["kind"] = "own_bound_dealer_NL_postal_address"
        return "NL", evidence
    if (
        dealer
        and address
        and re.search(r"\b\d{5}\s+[A-ZÄÖÜ]", address)
        and re.match(r"\+49(?:\D|\d)", phone or "")
    ):
        evidence.update(kind="own_bound_dealer_DE_postal_and_phone", phone=phone)
        return "DE", evidence
    return None, None


def _raw(identifier, url, native, country=None, evidence=None, **fields):
    return {
        "market_country": country,
        "country_evidence": evidence,
        "identity_evidence": {
            "kind": "own_native_ad_binding",
            "publication_id": identifier,
            "canonical": _canonical(url),
        },
        "own_identifiers": {"publication_id": identifier},
        "native_vehicle": native,
        "generation": None,
        "model_generation": None,
        "body_type": None,
        "model_brand": None,
        "model_family": None,
        "model_variant": None,
        "production_date": None,
        "production_year": None,
        "model_year": None,
        "first_registration": None,
        "dealer_address": None,
        **fields,
    }


def _finish(item, request_url):
    item.raw["request_url"] = request_url
    item.raw["identity_evidence"]["request_url"] = request_url
    item.raw["native_country_evidence"] = copy.deepcopy(item.raw.get("country_evidence"))
    item.raw["mercedes_own_fields"] = {
        "body": item.raw.get("body_type"),
        "generation": item.raw.get("generation"),
        "production_date": item.raw.get("production_date"),
        "built_year": item.raw.get("production_year"),
        "model_year": item.raw.get("model_year"),
        "first_registration": item.raw.get("first_registration"),
        "dealer_address": item.raw.get("dealer_address"),
        "country": item.raw.get("market_country"),
    }
    if item.vin:
        item.raw["own_identifiers"]["vin"] = item.vin
    # No new qualifier or query-derived generation is installed here.
    return item.qualify()


def _status(status, **extra):
    return Page([], search_filters={"parse_status": status, **extra})


def _coherent_rows(rows):
    """Do not select the first record when native IDs/facts contradict one another."""
    by_uri, native_ids = {}, {}
    rejected = set()
    for item in rows:
        previous = by_uri.get(item.url)
        if previous is not None and previous.raw["native_vehicle"] != item.raw["native_vehicle"]:
            rejected.add(item.url)
        by_uri[item.url] = item
        own_id = item.raw["own_identifiers"].get("native_vehicle_id")
        if own_id:
            native_ids.setdefault(own_id, set()).add(item.url)
    for uris in native_ids.values():
        if len(uris) > 1:
            rejected.update(uris)
    return [item for uri, item in by_uri.items() if uri not in rejected]


def _price_label(amount, label):
    label = label.lower()
    if any(
        x in label
        for x in ("inzahlungnahme", "inruil", "nur bei", "prijs ab", "prijs vanaf", "preis ab")
    ):
        return None, "conditional_price"
    if any(x in label for x in ("monat", "rate", "p/m", "maand", "lease", "vanaf")):
        return None, "monthly_or_conditional"
    if any(x in label for x in ("netto", "excl", "zzgl. mwst")):
        return None, "net_only"
    if any(x in label for x in ("inkl. mwst", "incl. btw")):
        return _amount(amount), "advertised_price"
    return _amount(amount), "basis_unknown"


def _de_cards(soup, url):
    rows = []
    for card in soup.select('div[id^="resultListItem"]')[:MAX_ROWS]:
        m = re.fullmatch(r"resultListItem([1-9]\d{4,11})", card.get("id", ""))
        if not m:
            continue
        own = m[1]
        detail_links = [
            a["href"] for a in card.select("a[href]") if urlsplit(a["href"]).path == "/vehicle"
        ]
        if not detail_links or any(
            mercedes_official_publication_id(link) != own for link in detail_links
        ):
            continue
        uris = {
            _canonical(a["href"])
            for a in card.select("a[href]")
            if mercedes_official_publication_id(a["href"]) == own
        }
        uris.discard(None)
        ids = {x.get("data-vehicle") for x in card.select("[data-vehicle]")}
        if len(uris) != 1 or ids != {own}:
            continue
        uri = next(iter(uris))
        values = {}
        for row in card.select(".vc-vehicle-attribute-row"):
            icon = row.select_one("use")
            key = icon.get("xlink:href", "") if icon else ""
            values[key] = _text(row.select_one(".vc-vehicle-attribute-text"))
        location = card.select_one(".result-box-location-item")
        parts = (
            [_text(x) for x in location.select(".vc-vehicle-attribute-text")] if location else []
        )
        price_node = copy.copy(card.select_one(".vehicle_price_headline"))
        if price_node:
            for footnote in price_node.select("sup"):
                footnote.decompose()
        label = _text(card.select_one(".vehicle-price-label-box"))
        cash, basis = _price_label(_text(price_node), label)
        first = _date(values.get("#vehicle-box-first-registration"))
        native = {
            "attributes": values,
            "dealer": parts,
            "price_text": _text(price_node),
            "price_label": label,
            "title": _text(card.select_one(".vehicle_title_headline")),
        }
        raw = _raw(
            own,
            uri,
            native,
            body_type=values.get("#vehicle-box-body-type"),
            first_registration=first,
            dealer_address=parts[1] if len(parts) > 1 else None,
            price_components={"original_price_text": native["price_text"], "basis_label": label},
            detail_complete=False,
        )
        rows.append(
            _finish(
                Listing(
                    "mercedes_benz_de",
                    uri,
                    uri,
                    native["title"],
                    price=cash,
                    price_type=basis,
                    mileage=number(values.get("#vehicle-box-mileage")),
                    year=model_year(first),
                    fuel=fuel(values.get("#vehicle-box-engine-ice")),
                    seller=parts[0] if parts else None,
                    images=[
                        x.get("data-src") or x.get("src") for x in card.select("img[data-src]")
                    ],
                    raw=raw,
                ),
                url,
            )
        )
    return _coherent_rows(rows)


def _de_paging(soup):
    button = soup.select_one("#pagingTopMenuButton")
    m = re.fullmatch(r"Seite (\d+) von (\d+)", button.get("aria-label", "")) if button else None
    return (int(m[1]), int(m[2])) if m else (None, None)


def _de_detail(soup, url):
    own = mercedes_official_publication_id(url)
    uri = _canonical(url)
    canonical = {_canonical(x.get("href", "")) for x in soup.select("link[rel=canonical]")}
    ids = {x.get("data-vehicle") for x in soup.select(".v-det-a[data-vehicle][data-type]")}
    if canonical != {uri} or ids != {own}:
        return _status("identity_mismatch")
    specs = {}
    for row in soup.select(".basic-data"):
        key, value = _text(row.select_one(".data-label")), _text(row.select_one(".data-value"))
        if key in specs and specs[key] != value:
            return _status("conflicting_own_specs")
        specs[key] = value
    title_node = soup.select_one(".contact-dealer-title")
    block = title_node
    for _ in range(4):
        block = block.parent if block is not None else None
    dealer = _text(title_node)
    address_node = (
        block.select_one('.contact-dealer-p-text a[href*="share.here.com"]') if block else None
    )
    address = _text(address_node)
    phones = {a.get("href", "")[4:] for a in block.select('a[href^="tel:"]')} if block else set()
    phone = next(iter(phones)) if len(phones) == 1 else None
    country, proof = _country_proof(own, address=address, phone=phone, dealer=dealer)
    price_nodes = soup.select(".vt-hl:has(.vehicle-price-label-vehicle)")
    cash, basis, price_text, price_label = None, "unknown", None, None
    if len(price_nodes) == 1:
        node = copy.copy(price_nodes[0])
        price_label = _text(node.select_one(".vehicle-price-label-vehicle"))
        for child in node.select(".vehicle-price-label-vehicle,sup"):
            child.decompose()
        price_text = _text(node)
        cash, basis = _price_label(price_text, price_label)
    name = " ".join(
        [
            key + " " + value
            for key, value in specs.items()
            if key in {"Mercedes-Benz", "Mercedes-AMG"}
        ]
    )
    if not name or not specs.get("Fahrzeugnummer"):
        return _status("not_own_detail")
    first = _date(specs.get("Erstzulassung"))
    body = specs.get("Fahrzeugart") or specs.get("Coupé")
    raw = _raw(
        own,
        uri,
        specs,
        country,
        proof,
        model_brand="Mercedes-Benz" if "Mercedes-Benz" in specs else "Mercedes-AMG",
        model_variant=specs.get("Mercedes-Benz") or specs.get("Mercedes-AMG"),
        body_type=body,
        model_year=model_year(specs.get("Modeljahr")),
        first_registration=first,
        dealer_address=address,
        price_components={"original_price_text": price_text, "basis_label": price_label},
        dealer_stock_id=specs["Fahrzeugnummer"],
        detail_complete=True,
    )
    item = Listing(
        "mercedes_benz_de",
        uri,
        uri,
        name,
        price=cash,
        price_type=basis,
        year=model_year(first),
        mileage=number(specs.get("Laufleistung")),
        transmission=gearbox(specs.get("Getriebe")),
        fuel=fuel(specs.get("Kraftstoffart")),
        seller=dealer,
        raw=raw,
    )
    return Page([_finish(item, url)], search_filters={"parse_status": "own_detail"})


def _hedin_record(record, request_url):
    a = _dict(record.get("attributes"))

    def value(k):
        return _dict(a.get(k)).get("value")

    uri = record.get("url")
    identifier = mercedes_official_publication_id(uri)
    if (
        not identifier
        or _url(uri).hostname != HEDIN_HOST
        or not _url(uri).path.endswith("/" + str(record.get("slug")))
        or str(value("voertuignr_hexon")) != identifier
        or not re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", str(record.get("id", ""))
        )
        or record.get("brand") != "Mercedes-Benz"
    ):
        return None
    prices = _dict(record.get("prices"))
    sale = _dict(prices.get("sale"))
    cash = (
        _amount(sale.get("value"))
        if sale.get("includingVat") is not False and sale.get("currency") in (None, "EUR")
        else None
    )
    basis = (
        "advertised_price"
        if cash is not None
        else "net_only"
        if sale.get("includingVat") is False
        else "unknown"
    )
    first = _date(value("datum_deel_1"))
    country, proof = _country_proof(identifier, value("land"))
    raw = _raw(
        identifier,
        uri,
        record,
        country,
        proof,
        model_brand=record["brand"],
        model_family=record.get("model"),
        model_variant=record.get("type"),
        body_type=value("carrosserie"),
        production_year=model_year(value("bouwjaar")),
        production_year_field="attributes.bouwjaar",
        first_registration=first,
        dealer_address=None,
        price_components=prices,
        price_currency=sale.get("currency"),
        price_basis="gross"
        if sale.get("includingVat") is True
        else "net"
        if sale.get("includingVat") is False
        else "unspecified",
        detail_complete=False,
        condition=value("source_status"),
        availability_status="sold"
        if value("verkocht") == 1
        else "reserved"
        if value("gereserveerd") == 1
        else "available"
        if value("verkocht") == 0
        else None,
    )
    raw["own_identifiers"]["native_vehicle_id"] = record["id"]
    raw["price_components"] = {
        "native_prices": prices,
        "original_retail": _dict(prices.get("retail")),
        "current_sale": sale,
        "action": prices.get("action"),
        "advertised_purchase": record.get("price"),
    }
    return _finish(
        Listing(
            "mercedes_benz_nl",
            _canonical(uri),
            _canonical(uri),
            " ".join(
                str(x) for x in [record["brand"], record.get("model"), record.get("type")] if x
            ),
            price=cash,
            price_type=basis,
            year=model_year(first),
            mileage=number(value("tellerstand")),
            transmission=gearbox(value("transmissie")),
            fuel=fuel(value("brandstof")),
            kenteken=plate(value("kenteken")),
            seller=value("vestiging"),
            city=value("plaatsnaam"),
            images=[
                x["path"]
                for x in record.get("images", [])
                if isinstance(x, dict) and isinstance(x.get("path"), str)
            ],
            raw=raw,
        ),
        request_url,
    )


def _hedin_detail(soup, url):
    own, uri = mercedes_official_publication_id(url), _canonical(url)
    cars = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            car = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        if isinstance(car, dict) and car.get("@type") == "Car":
            cars.append(car)
    if len(cars) != 1:
        return _status("not_own_detail")
    car = cars[0]
    offer = _dict(car.get("offers"))
    if (
        str(car.get("mpn")) != own
        or _canonical(offer.get("url")) != uri
        or car.get("brand") != "Mercedes-Benz"
    ):
        return _status("identity_mismatch")
    nodes = soup.select("#vehicle-intro .compare-toggle[data-vehicle]")
    try:
        payloads = [json.loads(x["data-vehicle"]) for x in nodes]
    except (ValueError, TypeError):
        return _status("not_own_detail")
    if (
        not payloads
        or any(x.get("id") != car.get("sku") for x in payloads)
        or len({json.dumps(x, sort_keys=True) for x in payloads}) != 1
    ):
        return _status("identity_mismatch")
    record = payloads[0]
    if (
        record.get("brand") != car.get("brand")
        or record.get("model") != car.get("model")
        or record.get("type") != car.get("vehicleConfiguration")
        or _amount(offer.get("price")) != _amount(record.get("price"))
    ):
        return _status("identity_or_price_mismatch")
    locations = soup.select("#vehicle-location .card-body")
    addresses = {_text(x.select_one("p.mb-3")) for x in locations}
    dealers = {_text(x.select_one("p.mb-1")) for x in locations}
    if len(addresses) != 1 or len(dealers) != 1:
        return _status("conflicting_own_dealer")
    address, dealer = next(iter(addresses)), next(iter(dealers))
    country, proof = _country_proof(own, address=address, dealer=dealer)
    specs = {}
    for term in soup.select("#vehicle-specs dt, #vehicle-specs-modal-specs-pane dt"):
        key, value = _text(term), _text(term.find_next_sibling("dd"))
        if key in specs and specs[key] != value:
            return _status("conflicting_own_specs")
        specs[key] = value
    engines = car.get("vehicleEngine") or []
    engines = engines if isinstance(engines, list) else [engines]
    types = {fuel(x.get("fuelType")) for x in engines if isinstance(x, dict)}
    if (
        types != {fuel(specs.get("Brandstofsoort"))}
        or gearbox(car.get("vehicleTransmission")) != gearbox(specs.get("Transmissie"))
        or car.get("bodyType") != specs.get("Carrosserie")
    ):
        return _status("conflicting_own_specs")
    native_price = _amount(offer.get("price")) if offer.get("priceCurrency") == "EUR" else None
    raw = _raw(
        own,
        uri,
        record,
        country,
        proof,
        model_brand=car.get("brand"),
        model_family=car.get("model"),
        model_variant=car.get("vehicleConfiguration"),
        body_type=car.get("bodyType"),
        production_date=car.get("productionDate"),
        production_year=model_year(car.get("productionDate")),
        model_year=model_year(car.get("vehicleModelDate")),
        number_of_doors=number(car.get("numberOfDoors")),
        dealer_address=address,
        schema_vehicle=car,
        detail_complete=True,
        price_currency=offer.get("priceCurrency"),
        price_basis="unspecified",
        price_components={
            "native_offer": offer,
            "widget_price": record.get("price"),
            "vat_margin_label": specs.get("BTW / marge"),
        },
        availability_status="available"
        if offer.get("availability") == "https://schema.org/InStock"
        else None,
    )
    raw["own_identifiers"]["native_vehicle_id"] = car["sku"]
    item = Listing(
        "mercedes_benz_nl",
        uri,
        uri,
        car.get("description") or record["attributes"].get("title", ""),
        price=native_price,
        price_type="advertised_price" if native_price is not None else "unknown",
        year=None,
        mileage=number(car.get("mileageFromOdometer")),
        transmission=gearbox(car.get("vehicleTransmission")),
        fuel=next(iter(types)),
        kenteken=plate(specs.get("Kenteken")),
        vin=normalized_vin(car.get("vehicleIdentificationNumber")),
        seller=dealer,
        images=[car["image"]] if isinstance(car.get("image"), str) else [],
        raw=raw,
    )
    return Page([_finish(item, url)], search_filters={"parse_status": "own_detail"})


def _nuxt_decode(data, index, active=None, depth=0):
    active = set() if active is None else active
    if index == -1:
        return None
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or not 0 <= index < len(data)
        or depth > 60
        or index in active
    ):
        raise ValueError("invalid_native_reference")
    active.add(index)
    x = data[index]
    if isinstance(x, dict):
        x = {k: _nuxt_decode(data, v, active, depth + 1) for k, v in x.items()}
    elif isinstance(x, list):
        if len(x) == 2 and x[0] in ("Reactive", "ShallowReactive", "Ref", "ShallowRef"):
            x = _nuxt_decode(data, x[1], active, depth + 1)
        else:
            x = [_nuxt_decode(data, v, active, depth + 1) for v in x]
    active.remove(index)
    return x


def _mossel(soup, url):
    nodes = soup.select('script#__NUXT_DATA__[data-ssr="true"]')
    if len(nodes) != 1:
        return _status("not_inventory")
    try:
        data = json.loads(nodes[0].get_text())
        if (
            not isinstance(data, list)
            or not 1 <= len(data) <= 50000
            or data[0][0] != "ShallowReactive"
        ):
            return _status("not_inventory")
        root = data[data[0][1]]
        native_path = urlsplit(_nuxt_decode(data, root["path"]))
        if native_path.path != _url(url).path or parse_qs(native_path.query) != parse_qs(
            _url(url).query
        ):
            return _status("route_mismatch")
        wrapper = data[root["data"]]
        mapping = data[wrapper[1]]
        refs = [
            v
            for v in mapping.values()
            if isinstance(v, int)
            and v >= 0
            and isinstance(data[v], dict)
            and set(data[v]) == {"vehicles", "amount"}
        ]
        if len(refs) != 1:
            return _status("not_inventory")
        stock = _nuxt_decode(data, refs[0])
    except (ValueError, TypeError, KeyError, IndexError, RecursionError):
        return _status("invalid_native_payload")
    records = stock.get("vehicles")
    if not isinstance(records, list) or len(records) > MAX_ROWS:
        return _status("not_inventory")
    links = {_canonical(urljoin(url, x["href"])) for x in soup.select("a[href]")}
    rows = []
    for record in records:
        if not isinstance(record, dict) or record.get("brand") != "Mercedes-Benz":
            continue
        uris = {
            x
            for x in links
            if x
            and mercedes_official_publication_id(x) == record.get("id")
            and _url(x).path.endswith("-" + str(record.get("slug")))
        }
        if len(uris) != 1:
            continue
        uri = next(iter(uris))
        own = record["id"]
        specs = {
            x.get("type"): x.get("value") for x in record.get("specs", []) if isinstance(x, dict)
        }
        advertiser = _dict(record.get("advertiser"))
        country, proof = _country_proof(own, advertiser.get("country"))
        purchase = _dict(_dict(record.get("price")).get("purchase"))
        cash, basis = _price_label(purchase.get("formatted", ""), purchase.get("suffix") or "")
        if _dict(purchase.get("discount")).get("isDiscounted") is True:
            cash, basis = None, "conditional_price"
        raw = _raw(
            own,
            uri,
            record,
            country,
            proof,
            model_brand=record["brand"],
            model_family=record.get("model"),
            model_variant=record.get("type"),
            native_display_year=record.get("year"),
            price_components=record.get("price"),
            condition=_dict(_dict(record.get("condition")).get("formatted")).get("key"),
            detail_complete=False,
        )
        rows.append(
            _finish(
                Listing(
                    "mercedes_benz_nl",
                    uri,
                    uri,
                    " ".join(str(record.get(k) or "") for k in ("brand", "model", "type")),
                    price=cash,
                    price_type=basis,
                    year=None,
                    mileage=number(specs.get("mileage")),
                    transmission=gearbox(specs.get("transmission_type")),
                    fuel=fuel(specs.get("fuel")),
                    seller=advertiser.get("name"),
                    images=[
                        x["src"]
                        for x in record.get("images", [])
                        if isinstance(x, dict) and isinstance(x.get("src"), str)
                    ],
                    raw=raw,
                ),
                url,
            )
        )
    rows = _coherent_rows(rows)
    return Page(
        rows,
        total=_int(stock.get("amount")),
        search_filters={
            "parse_status": "own_native_SSR",
            "paging_certified": False,
            "coverage_complete": False,
            "rejected_records": len(records) - len(rows),
        },
    )


def _nl_cards(soup, url):
    rows = []
    for card in soup.select("a.c-overview__item[href]")[:MAX_ROWS]:
        uri = _canonical(urljoin(url, card["href"]))
        if uri is None:
            continue
        own = mercedes_official_publication_id(uri)
        images = [
            urljoin(url, x["src"])
            for x in card.select("img[src]")
            if re.search(r"/" + re.escape(own) + r"_\d+\.jpg", x["src"])
        ]
        if not images:
            continue
        specs = [_text(x) for x in card.select(".c-overview__item__list-item")]
        if len(specs) < 4:
            continue
        title = _text(card.select_one(".c-overview__item__title"))
        price_text = _text(card.select_one(".regularPrice .c-overview__item__price"))
        cash, basis = _price_label(price_text, "")
        raw = _raw(
            own,
            uri,
            {"specs": specs, "price_text": price_text},
            native_display_year=specs[0],
            detail_complete=False,
            price_components={"original_price_text": price_text},
        )
        rows.append(
            _finish(
                Listing(
                    "mercedes_benz_nl",
                    uri,
                    uri,
                    "Mercedes-Benz " + title,
                    price=cash,
                    price_type=basis,
                    year=None,
                    mileage=number(specs[1]),
                    transmission=gearbox(specs[2]),
                    fuel=fuel(specs[3]),
                    seller=_text(card.select_one(".c-dealerbox__info")),
                    images=list(dict.fromkeys(images)),
                    raw=raw,
                ),
                url,
            )
        )
    return rows


def _nl_detail(soup, url):
    uri, own = _canonical(url), mercedes_official_publication_id(url)
    if {_canonical(x.get("href", "")) for x in soup.select("link[rel=canonical]")} != {uri}:
        return _status("identity_mismatch")
    intro = soup.select_one(".c-detailheader__intro__title")
    if intro is None or not any(
        re.search(r"/" + re.escape(own) + r"_\d+\.jpg", x.get("src", ""))
        for x in soup.select("img[src]")
    ):
        return _status("not_own_detail")
    specs = {}
    for row in soup.select("tr.c-table__row"):
        cells = row.select("td.c-table__col")
        if len(cells) == 2:
            key, value = map(_text, cells)
            if key in specs and specs[key] != value:
                return _status("conflicting_own_specs")
            specs[key] = value
    summary = [_text(x) for x in intro.parent.parent.select("ul.c-overview__item__list > li")]
    if len(summary) < 4:
        return _status("not_own_detail")
    location = soup.select_one(".c-dealerbox__info")
    dealer = _text(location.select_one(".h2")) if location else None
    address = copy.copy(location.select_one("p")) if location else None
    if address:
        for node in address.select("strong"):
            node.decompose()
    address = _text(address)
    country, proof = _country_proof(own, address=address, dealer=dealer)
    cash, basis = _price_label(
        _text(soup.select_one(".c-detailheader__intro__price")),
        _text(soup.select_one(".c-detailheader__intro__price-desc")),
    )
    first = _date(specs.get("Datum deel 1"))
    raw = _raw(
        own,
        uri,
        specs,
        country,
        proof,
        first_registration=first,
        number_of_doors=number(specs.get("Aantal deuren")),
        native_display_year=summary[0],
        dealer_address=address,
        model_variant=_text(intro.select_one(".c-detailheader__intro__subtitle")),
        model_family=_text(intro).split(" C ")[0],
        detail_complete=True,
        price_components={
            "original_price_text": _text(soup.select_one(".c-detailheader__intro__price")),
            "basis_label": _text(soup.select_one(".c-detailheader__intro__price-desc")),
        },
    )
    item = Listing(
        "mercedes_benz_nl",
        uri,
        uri,
        "Mercedes-Benz " + _text(intro),
        price=cash,
        price_type=basis,
        year=model_year(first),
        mileage=number(summary[1]),
        transmission=gearbox(summary[2]),
        fuel=fuel(summary[3]),
        kenteken=plate(specs.get("Kenteken")),
        seller=dealer,
        raw=raw,
    )
    return Page([_finish(item, url)], search_filters={"parse_status": "own_detail"})


def parse_mercedes_official(body, url):
    """Native stock records only; no recursive search for similar vehicles in JSON."""
    route = _route(url)
    if route is None:
        return _status("unsafe_or_uncaptured_route")
    if route in _SEARCH_KEYS and not _safe_search_query(route, _url(url)):
        return _status("unsafe_or_uncaptured_query")
    if not isinstance(body, (str, bytes)) or len(body) > MAX_BODY:
        return _status("invalid_or_oversize_body")
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return _status("invalid_encoding")
    p = _url(url)
    if route in {"de_post", "hedin_search"}:
        try:
            document = json.loads(body)
        except ValueError:
            return _status("not_inventory")
        if not isinstance(document, dict):
            return _status("not_inventory")
        if route == "de_post":
            data = _dict(document.get("data"))
            if (
                document.get("success") != 1
                or not isinstance(document.get("html"), str)
                or _int(data.get("count")) is None
                or _int(data.get("page")) is None
                or not isinstance(document.get("q"), dict)
            ):
                return _status("native_request_rejected_or_not_inventory")
            soup = BeautifulSoup(document["html"], "html.parser")
            paging = BeautifulSoup(document.get("pageTop", ""), "html.parser")
            page, pages = _de_paging(paging)
            if page != data["page"] or document["q"].get("page") != page:
                return _status("conflicting_native_paging")
            rows = _de_cards(soup, url)
            return Page(
                rows,
                total=data["count"],
                pages=pages,
                page=page,
                search_filters={
                    "parse_status": "own_native_POST",
                    "native_query": document["q"],
                    "paging_certified": pages is not None,
                    "coverage_complete": False,
                },
            )
        records, paging = document.get("items"), _dict(document.get("pages"))
        if (
            not isinstance(records, list)
            or len(records) > MAX_ROWS
            or _int(document.get("count")) is None
            or _int(paging.get("current")) is None
            or not isinstance(document.get("filters"), dict)
        ):
            return _status("not_inventory")
        requested = _query(p).get("page", ["1"])
        if requested != [str(paging["current"])]:
            return _status("conflicting_native_paging")
        rows = _coherent_rows(
            [
                item
                for record in records
                if isinstance(record, dict) and (item := _hedin_record(record, url)) is not None
            ]
        )
        return Page(
            rows,
            total=document["count"],
            pages=_int(paging.get("total")),
            page=paging["current"],
            search_filters={
                "parse_status": "own_native_JSON",
                "native_filters": document["filters"],
                "paging_certified": True,
                "coverage_complete": False,
                "rejected_records": len(records) - len(rows),
            },
        )
    soup = BeautifulSoup(body, "html.parser")
    if route == "detail":
        if p.hostname == DE_HOST:
            return _de_detail(soup, url)
        if p.hostname == HEDIN_HOST:
            return _hedin_detail(soup, url)
        if p.hostname == NL_HOST:
            return _nl_detail(soup, url)
        return _status("detail_contract_not_captured")
    if route == "mossel_search":
        return _mossel(soup, url)
    if route == "de_search":
        title = _text(soup.select_one("#vehicle-result-title"))
        match = re.fullmatch(r"Ihr Suchergebnis - ([\d.]+) Fahrzeuge?", title)
        if match is None:
            return _status("not_inventory")
        rows = _de_cards(soup, url)
        page, pages = _de_paging(soup)
        requested = _query(p).get("page", [str(page)])
        if requested != [str(page)]:
            return _status("conflicting_native_paging")
        return Page(
            rows,
            total=int(match[1].replace(".", "")),
            pages=pages,
            page=page,
            search_filters={
                "parse_status": "own_native_HTML",
                "paging_certified": pages is not None,
                "coverage_complete": False,
            },
        )
    count = soup.select_one(".c-topbar__text .update-num")
    if (
        count is None
        or "resultaten op basis van uw selectie" not in _text(count.parent)
        or soup.select_one('form#search-form[action="/cgi/voertuigen.cgi"]') is None
    ):
        return _status("not_inventory")
    total = _int(_text(count).replace(".", ""))
    if total is None:
        return _status("not_inventory")
    return Page(
        _nl_cards(soup, url),
        total=total,
        search_filters={
            "parse_status": "own_native_HTML",
            "paging_certified": False,
            "portal_scope": "Certified",
            "coverage_complete": False,
        },
    )
