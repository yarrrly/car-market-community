"""Ordinary HTTP, native paging and Markt location evidence from 08.10.2026.

The transport does not retain cookies/headers or log in. Public PRG is used
only for observed pagination/publisher buttons.
"""

import dataclasses
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .db import now
from .fetch import Response
from .germany import qualify_german_item
from .identity import normalized_vin
from .parse import Listing, Page, fuel, gearbox, number

MARKT_SEARCH = (
    "https://www.markt.de/fahrzeuge/autos/suche/218i+active+tourer/?attr_carmanufacturer=bmw"
)
FACEBOOK_PUBLIC = "https://www.facebook.com/marketplace/berlin/vehicles/"
INSTAMOTION_MODEL = "https://www.instamotion.com/de/autos/bmw/218"
_COUNTRIES = {
    "DE": "DE",
    "DEU": "DE",
    "DEUTSCHLAND": "DE",
    "GERMANY": "DE",
    "NL": "NL",
    "NLD": "NL",
    "NETHERLANDS": "NL",
    "NIEDERLANDE": "NL",
}


def public_route(url):
    try:
        p = urlsplit(url)
        q = parse_qs(p.query)
        if (
            p.scheme != "https"
            or p.username
            or p.password
            or p.fragment
            or p.port not in (None, 443)
            or any(re.search(r"token|session|api.?key|password|auth", k, re.IGNORECASE) for k in q)
        ):
            return False
        if p.hostname == "www.instamotion.com":
            return p.path in {
                "/de/autos",
                "/de/autos/bmw/group-2-series",
                "/de/autos/bmw/218",
            } or bool(
                re.fullmatch(r"/de/auto/\d+/[a-z0-9-]+|/_next/data/[\w-]+/de/search\.json", p.path)
            )
        if p.hostname == "angebote.carwow.de":
            return p.path in {
                "/stock_cars",
                "/stock_cars/deal-cards",
                "/stock_cars/deal-card",
            } or bool(
                re.fullmatch(r"/deals/[a-f0-9]{32}|/more_from_this_supplier/DEALERSHIP_\d+", p.path)
            )
        if p.hostname == "www.markt.de":
            return p.path == urlsplit(MARKT_SEARCH).path or bool(
                re.fullmatch(
                    r"/[a-z0-9-]+/a/[a-f0-9]{8}/|/(?:[a-z0-9-]+/)?urlMaskingRedirect\.htm", p.path
                )
            )
        return p.hostname == "www.facebook.com" and p.path == urlsplit(FACEBOOK_PUBLIC).path
    except (ValueError, TypeError):
        return False


def country(value):
    if isinstance(value, dict):
        value = value.get("name") or value.get("identifier")
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip().upper()
    return _COUNTRIES.get(value, value if re.fullmatch(r"[A-Z]{2}", value) else None)


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def markt_layers(soup):
    output = []
    for node in soup.select("script"):
        if node.get("id") == "additional-own-geography":
            payload = node.get_text()
        else:
            match = re.search(r"window\.clsyDataLayer\s*=\s*(\[.*?\]);", node.get_text(), re.DOTALL)
            if not match:
                continue
            payload = match[1]
        try:
            values = json.loads(payload)
        except ValueError:
            continue
        if isinstance(values, list):
            output.extend(
                {
                    k: v
                    for k, v in row.items()
                    if k
                    in {
                        "pageClass",
                        "advertId",
                        "geoId",
                        "country",
                        "state",
                        "county",
                        "city",
                        "source",
                        "partner",
                        "organization",
                        "wantedAdvert",
                        "priceType",
                    }
                }
                for row in values
                if isinstance(row, dict)
            )
    return output


def retain_public_markup(body):
    soup = BeautifulSoup(body or "", "html.parser")
    layers = markt_layers(soup)
    for script in soup.select("script"):
        if script.get("type") != "application/ld+json":
            script.decompose()
    for el in soup.select('iframe,meta[name="csrf-token"],input[name="authenticity_token"]'):
        el.decompose()
    if layers:
        node = soup.new_tag("script", id="additional-own-geography", type="application/json")
        node.string = json.dumps(layers)
        soup.append(node)
    return str(soup)


def _markt_id(url):
    if not public_route(url) or urlsplit(url).hostname != "www.markt.de":
        return None
    match = re.fullmatch(r"/[a-z0-9-]+/a/([a-f0-9]{8})/", urlsplit(url).path)
    return match[1] if match else None


def parse_markt(body, url):
    soup = BeautifulSoup(body or "", "html.parser")
    identity = _markt_id(url)
    if identity:
        own = []
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                node = json.loads(script.get_text())
            except ValueError:
                continue
            if isinstance(node, dict) and node.get("@type") == "Product":
                offer = node.get("offers")
                if isinstance(offer, dict) and offer.get("url") == url:
                    own.append(node)
        claims = [
            x
            for x in markt_layers(soup)
            if x.get("pageClass") == "ADVERT_EXPOSE" and x.get("advertId") == identity
        ]
        heading = soup.select_one("h1")
        if len(own) != 1 or len(claims) != 1 or _text(heading) != own[0].get("name"):
            return Page([], search_filters={"parse_status": "own_identity_unverified"})
        product, claim = own[0], claims[0]
        description = BeautifulSoup(product.get("description") or "", "html.parser").get_text(
            " ", strip=True
        )
        offer = product["offers"]
        own_country = country(claim.get("country"))
        raw = {
            "own_inventory_record": product,
            "own_geography_record": claim,
            "price_currency": offer.get("priceCurrency"),
            "country_evidence": {
                "path": "clsyDataLayer[advertId].country",
                "value": own_country,
                "advert_id": identity,
                "claim": claim,
            },
            "publication_reference": {
                "source": "markt_de",
                "external_id": identity,
                "url": url,
                "kind": "explicit_own_publication_link",
            },
            "model_status": "motor_unconfirmed",
        }
        if own_country:
            raw["market_country"] = own_country
        spec = offer.get("priceSpecification")
        kind = (
            "net_price"
            if isinstance(spec, dict) and spec.get("valueAddedTaxIncluded") is False
            else "advertised_price"
        )
        if claim.get("priceType") in {"MONTHLY", "LEASING"}:
            kind = "monthly_price"
        fields = {}
        for key in ["Erstzulassung", "Kilometerstand", "Getriebe", "Kraftstoff"]:
            match = re.search(
                re.escape(key)
                + r":\s*(.*?)(?=\s+(?:Erstzulassung|Kilometerstand|Leistung|Getriebe|Kraftstoff):|$)",
                description,
            )
            if match:
                fields[key] = match[1]
        year = re.match(r"(\d{4})-", fields.get("Erstzulassung", ""))
        item = Listing(
            "markt_de",
            identity,
            url,
            product["name"],
            price=number(offer.get("price")),
            price_type=kind,
            year=int(year[1]) if year else None,
            mileage=number(fields.get("Kilometerstand")),
            transmission=gearbox(fields.get("Getriebe")),
            fuel=fuel(fields.get("Kraftstoff")),
            city=claim.get("city"),
            vin=normalized_vin(product.get("vehicleIdentificationNumber")),
            description=description,
            raw=raw,
        )
        return Page([qualify_german_item(item)], search_filters={"own_detail": True})
    if urlsplit(url).path != urlsplit(MARKT_SEARCH).path:
        return Page([], search_filters={"parse_status": "invalid_markt_route"})
    title = soup.title.get_text() if soup.title else ""
    scope = parse_qs(urlsplit(url).query)
    if not title.startswith("218i Active Tourer:") or scope.get("attr_carmanufacturer") != ["bmw"]:
        return Page([], search_filters={"parse_status": "search_scope_unverified"})
    counter = re.search(r"Suchen\s*\(([\d.]+) Treffer\)", _text(soup))
    nav = soup.select_one('nav[aria-label="Seitennavigation"]')
    current = nav.select_one(".clsy-c-pagination__current") if nav else None
    pages = (
        [int(x["title"]) for x in nav.select("button[title]") if x["title"].isdigit()]
        if nav
        else []
    )
    request_page = int(scope.get("page", ["1"])[0])
    displayed_page = int(_text(current)) if _text(current).isdigit() else None
    items = {}
    for anchor in soup.select("a[href]"):
        target = urljoin(url, anchor["href"])
        ad_id = _markt_id(target)
        if ad_id:
            items.setdefault(
                ad_id,
                Listing(
                    "markt_de",
                    ad_id,
                    target,
                    _text(anchor),
                    raw={"discovery_only": True, "country_evidence": None},
                ),
            )
    forms = []
    if nav:
        for button in nav.select("button[data-targeturl][title]"):
            if not button["title"].isdigit():
                continue
            form = soup.find("form", id=button.get("form"))
            target = urljoin(url, form.get("action", "")) if form else ""
            if public_route(target):
                forms.append(
                    {
                        "page": int(button["title"]),
                        "method": "POST",
                        "url": target,
                        "body": {"targetUrl": button["data-targeturl"]},
                    }
                )
    return Page(
        list(items.values()),
        int(counter[1].replace(".", "")) if counter else None,
        max(pages + [displayed_page or 0]) or None,
        displayed_page,
        search_filters={
            "native_form_requests": forms,
            "discovery_only": True,
            "request_page_matches": displayed_page == request_page,
            "query_scope": "own_bmw_218i_active_tourer_search",
        },
    )


def parse_facebook_projection(body, url):
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        return Page([], search_filters={"parse_status": "native_public_projection_required"})
    records = doc.get("public_listing_records", []) if isinstance(doc, dict) else []
    request = doc.get("request_context") or {}
    variables = request.get("variables") or {}
    # The query/count=10 request has no verified vehicle inventory result set.
    verified = doc.get("verified_inventory_query") == "BMW 218i Active Tourer"
    items = []
    for row in records:
        identity = str(row.get("id") or "")
        if not identity.isdigit():
            continue
        raw = {
            "own_inventory_record": row,
            "country_evidence": None,
            "discovery_only": True,
            "query_verified": verified,
            "query_variables": variables,
        }
        own_geo = row.get("location") or {}
        reverse = own_geo.get("reverse_geocode") or {}
        own_country = country(reverse.get("country_code") or reverse.get("country"))
        if own_country:
            raw["market_country"] = own_country
            raw["country_evidence"] = {
                "path": "location.reverse_geocode.country",
                "value": own_country,
            }
        items.append(
            Listing(
                "facebook_de",
                identity,
                "https://www.facebook.com/marketplace/item/" + identity + "/",
                str(row.get("marketplace_listing_title") or ""),
                raw=raw,
            )
        )
    return Page(
        items,
        search_filters={
            "query_scope": "native_inventory_query"
            if verified
            else "unfiltered_or_query_unverified",
            "query_verified": verified,
            "discovery_only": True,
            "country_policy": "per_ad_country_not_berlin_or_state_code",
        },
    )


@dataclasses.dataclass
class PublicResponse(Response):
    source_body_sha256: str | None = None
    redirect_to: str | None = None
    request_method: str = "GET"
    request_body: dict | None = None
    country_publication: dict | None = None
    extra_responses: list = dataclasses.field(default_factory=list)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def response_proof(response):
    return {
        "url": response.url,
        "final_uri": response.final_url,
        "date": response.timestamp,
        "status": response.status,
        "method": getattr(response, "request_method", "GET"),
        "source_body_sha256": getattr(response, "source_body_sha256", None),
        "retained_sha256": hashlib.sha256((response.body or "").encode()).hexdigest(),
    }


def markt_carwow_navigation(body, url):
    """Only the observed publisher button of one country-proven own advert."""
    page = parse_markt(body, url)
    if len(page.listings) != 1 or page.listings[0].raw.get("market_country") != "DE":
        return None
    soup = BeautifulSoup(body, "html.parser")
    buttons = [
        node
        for node in soup.select("button[data-targeturl][form]")
        if _text(node) == "Jetzt bei Carwow mehr erfahren!"
    ]
    if len(buttons) != 1:
        return None
    button = buttons[0]
    forms = soup.find_all("form", id=button["form"])
    action = urljoin(url, forms[0].get("action", "")) if len(forms) == 1 else ""
    if not public_route(action) or not urlsplit(action).path.endswith("/urlMaskingRedirect.htm"):
        return None
    return {
        "url": action,
        "body": {"targetUrl": button["data-targeturl"]},
        "claim_id": page.listings[0].external_id,
        "claim_url": page.listings[0].url,
    }


def carwow_publication_redirect(response):
    if response.status not in {301, 302, 303, 307, 308}:
        return None
    target = getattr(response, "redirect_to", None)
    if target and not public_route(target):
        targets = [
            v[0]
            for v in parse_qs(urlsplit(target).query).values()
            if len(v) == 1 and v[0].startswith("https://angebote.carwow.de/deals/")
        ]
        target = targets[0] if len(targets) == 1 else None
    if not target or not public_route(target) or urlsplit(target).hostname != "angebote.carwow.de":
        return None
    own = re.fullmatch(r"/deals/([a-f0-9]{32})", urlsplit(target).path)
    return {"external_id": own[1], "url": target} if own else None


class AdditionalPublicGetter:
    """GET/observed public PRG without a session; the limit includes redirect hops."""

    def __init__(self, max_requests=20, timeout=25, country_publications=None, request_delay=0.7):
        if not isinstance(max_requests, int) or isinstance(max_requests, bool) or max_requests < 1:
            raise ValueError("invalid_public_request_budget")
        self.max_requests, self.timeout = max_requests, timeout
        self.country_publications = country_publications or {}
        self.http_requests = 0
        self.captures = []
        if (
            isinstance(request_delay, bool)
            or not isinstance(request_delay, (int, float))
            or not math.isfinite(request_delay)
            or not 0 <= request_delay <= 10
        ):
            raise ValueError("invalid_public_request_delay")
        self.request_delay = request_delay
        self.last_request = None
        self.blocked_hosts = {}
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _request(self, url, method="GET", body=None, follow=True):
        from .germany_additional import _retained_body

        initial = url
        for _ in range(4):
            if not public_route(url):
                raise ValueError("invalid_public_route")
            guard = getattr(self, "before_request", None)
            if guard:
                guard(url)
            host = urlsplit(url).hostname
            if host in self.blocked_hosts:
                raise ValueError(f"public_host_blocked:{host}:{self.blocked_hosts[host]}")
            if self.http_requests >= self.max_requests:
                raise ValueError("public_http_request_budget")
            if self.last_request is not None:
                time.sleep(max(0, self.request_delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.http_requests += 1
            data = urlencode(body).encode() if body is not None else None
            req = urllib.request.Request(  # noqa: S310 — public_route allows only the source HTTPS routes
                url,
                data=data,
                method=method,
                headers={
                    "User-Agent": "FirstCarMarket/0.1 (public vehicle market research)",
                    "Accept-Language": "de-DE,de;q=0.9",
                },
            )
            stamp = now()
            try:
                with self.opener.open(req, timeout=self.timeout) as result:
                    payload, status, headers = (
                        result.read(12_000_001),
                        result.status,
                        result.headers,
                    )
            except urllib.error.HTTPError as exc:
                payload, status, headers = exc.read(12_000_001), exc.code, exc.headers
            if status in {403, 429}:
                self.blocked_hosts[host] = status
            text = payload.decode("utf-8", errors="replace")
            redirect = (
                urljoin(url, headers.get("Location", ""))
                if status in {301, 302, 303, 307, 308}
                else None
            )
            source = (
                "markt_de"
                if urlsplit(url).hostname == "www.markt.de"
                else "instamotion_de"
                if "instamotion" in url
                else "carwow_de"
            )
            safe = (
                retain_public_markup(text) if source == "markt_de" else _retained_body(source, text)
            )
            if "/_next/data/" in url:
                doc = json.loads(text)
                props = doc.get("pageProps", {})
                safe = json.dumps(
                    {
                        "pageProps": {
                            k: v
                            for k, v in props.items()
                            if k in {"routerState", "searchResults", "searchBanners"}
                        }
                    }
                )
            response = PublicResponse(
                initial,
                url,
                status,
                safe,
                stamp,
                "response_too_large" if len(payload) > 12_000_000 else None,
                source_body_sha256=hashlib.sha256(payload).hexdigest(),
                redirect_to=redirect,
                request_method=method,
                request_body=body,
            )
            self.captures.append(response)
            if not follow or not redirect or not public_route(redirect):
                return response
            url = redirect
            if status in {301, 302, 303}:
                method, body = "GET", None
        raise ValueError("public_redirect_depth")

    def post_public_navigation(self, url, body):
        if set(body) != {"targetUrl"} or not urlsplit(url).path.endswith("/urlMaskingRedirect.htm"):
            raise ValueError("only_observed_public_prg_allowed")
        return self._request(url, "POST", body, follow=False)

    def get(self, url):
        response = self._request(url)
        match = re.fullmatch(r"/deals/([a-f0-9]{32})", urlsplit(response.final_url).path)
        claim_url = self.country_publications.get(match[1]) if match else None
        if response.status != 200 or not claim_url:
            return response
        own = self._request(claim_url)
        page = parse_markt(own.body, own.final_url)
        if own.status != 200 or len(page.listings) != 1:
            return response
        claim = page.listings[0]
        soup = BeautifulSoup(own.body, "html.parser")
        buttons = [
            b
            for b in soup.select("button[data-targeturl][form]")
            if _text(b) == "Jetzt bei Carwow mehr erfahren!"
        ]
        if len(buttons) != 1 or not claim.raw.get("market_country"):
            return response
        button = buttons[0]
        form = soup.find("form", id=button["form"])
        action = urljoin(own.final_url, form["action"]) if form else ""
        nav = self.post_public_navigation(action, {"targetUrl": button["data-targeturl"]})
        target = nav.redirect_to
        if target and not public_route(target):
            targets = [
                v[0]
                for v in parse_qs(urlsplit(target).query).values()
                if len(v) == 1 and v[0].startswith("https://angebote.carwow.de/deals/")
            ]
            target = targets[0] if len(targets) == 1 else None
        if not target or not public_route(target) or urlsplit(target).path != "/deals/" + match[1]:
            return response
        response.country_publication = {
            "kind": "explicit_own_publication_country",
            "country": claim.raw["market_country"],
            "claim_source": "markt_de",
            "claim_id": claim.external_id,
            "claim_url": claim.url,
            "native_id": match[1],
            "native_url": response.final_url,
            "explicit_publisher_url": target,
            "country_evidence": claim.raw["country_evidence"],
            "proofs": [response_proof(own), response_proof(nav), response_proof(response)],
        }
        response.extra_responses = [own, nav]
        return response


def apply_country_publication(item, response):
    proof = getattr(response, "country_publication", None)
    if (
        not proof
        or proof.get("native_id") != item.external_id
        or proof.get("native_url") != item.url
    ):
        return item
    if (
        proof.get("kind") != "explicit_own_publication_country"
        or _markt_id(proof.get("claim_url")) != proof.get("claim_id")
        or urlsplit(proof.get("explicit_publisher_url", "")).path != "/deals/" + item.external_id
    ):
        return item
    # Explicit vehicle location takes precedence over a linked publication/seller country.
    values = item.raw.get("own_detail_values", {})
    location_country = country(values.get("Fahrzeugstandort Land"))
    existing = item.raw.get("market_country")
    selected = location_country or existing or country(proof.get("country"))
    if not selected:
        return item
    item.raw["market_country"] = selected
    item.raw["country_evidence"] = (
        {"path": "own_detail_values.Fahrzeugstandort Land", "value": selected}
        if location_country
        else proof
    )
    item.raw.pop("geography_status", None)
    return qualify_german_item(item)


def additional_page_request(source, seed_url, page, first_page=None, first_body=None):
    if (
        not isinstance(page, int)
        or isinstance(page, bool)
        or page < 1
        or not public_route(seed_url)
    ):
        raise ValueError("invalid_additional_page_request")
    if page == 1:
        return {"method": "GET", "url": seed_url, "body": None}
    if first_page is None or first_page.pages is None or page > first_page.pages:
        raise ValueError("page_outside_observed_native_navigation")
    if source == "markt_de":
        candidates = [
            x
            for x in first_page.search_filters.get("native_form_requests", [])
            if x["page"] == page
        ]
        if len(candidates) != 1:
            raise ValueError("own_pagination_form_missing")
        return candidates[0]
    if (
        source == "instamotion_de"
        and urlsplit(seed_url).path.endswith("/group-2-series")
        and first_body
    ):
        soup = BeautifulSoup(first_body, "html.parser")
        node = soup.find(id="__NEXT_DATA__")
        doc = json.loads(node.get_text()) if node else {}
        build = doc.get("buildId")
        if not isinstance(build, str) or not re.fullmatch(r"[\w-]+", build):
            raise ValueError("native_build_id_missing")
        return {
            "method": "GET",
            "url": f"https://www.instamotion.com/_next/data/{build}/de/search.json?"
            + urlencode([("filters", "bmw"), ("filters", "group-2-series"), ("page", page)]),
            "body": None,
        }
    candidates = [
        u for u in first_page.links if parse_qs(urlsplit(u).query).get("page") == [str(page)]
    ]
    if len(candidates) != 1:
        raise ValueError("own_pagination_link_missing")
    return {"method": "GET", "url": candidates[0], "body": None}


def get_additional_page(getter, source, seed_url, page=1, first_page=None, first_body=None):
    from .germany_additional import parse_additional

    request = additional_page_request(source, seed_url, page, first_page, first_body)
    if request["method"] == "POST":
        hop = getter.post_public_navigation(request["url"], request["body"])
        if not hop.redirect_to or not public_route(hop.redirect_to):
            raise ValueError("public_pagination_redirect_unverified")
        response = getter.get(hop.redirect_to)
    else:
        response = getter.get(request["url"])
    if response.status != 200 or response.error:
        raise ValueError(response.error or f"http_{response.status}")
    parsed = parse_additional(source, response.body, response.final_url)
    if parsed.page != page:
        raise ValueError("native_page_identity_mismatch")
    return parsed, response
