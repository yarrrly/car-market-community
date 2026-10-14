"""Instamotion and Carwow: publication pages captured on 08.10.2026.

Carwow retains the frontier: site country, HSN and origin do not prove
the country of an individual offer. The core dispatcher is connected separately.
"""

import dataclasses
import hashlib
import json
import math
import re
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .db import now
from .germany import observe_german_item, qualify_german_item
from .identity import normalized_vin
from .parse import Listing, Page, fuel, gearbox, number

INSTAMOTION_SEARCH = "https://www.instamotion.com/de/autos/bmw/group-2-series"
INSTAMOTION_MODEL = "https://www.instamotion.com/de/autos/bmw/218"
CARWOW_SEARCH = (
    "https://angebote.carwow.de/stock_cars?brand_slug=bmw"
    "&model_slug=2er-active-tourer&vehicle_state_group=all"
)
SOURCES = {"instamotion_de", "carwow_de", "markt_de", "facebook_de"}
_CW_ATTR = "data-stock-cars-v2--results-"


def _object(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _amount(value):
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
        return amount if math.isfinite(amount) and amount >= 0 else None
    except (TypeError, ValueError):
        return None


def _integer(value):
    amount = _amount(value)
    return int(amount) if amount is not None and amount.is_integer() else None


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def _public(source, url):
    from .germany_additional_public import public_route

    try:
        parsed = urlsplit(url)
        host = {
            "instamotion_de": "www.instamotion.com",
            "carwow_de": "angebote.carwow.de",
            "markt_de": "www.markt.de",
            "facebook_de": "www.facebook.com",
        }
        if (
            parsed.scheme != "https"
            or parsed.hostname != host.get(source)
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            return False
        return public_route(url)
    except (ValueError, TypeError):
        return False


def _reference(source, url, identity):
    return {
        "source": source,
        "url": url,
        "external_id": identity,
        "kind": "explicit_own_publication_link",
    }


def additional_queries(include_controls=False):
    """Requests for collect_additional without price, year or transmission limits."""
    queries = [
        {
            "source": "instamotion_de",
            "key": "de-instamotion-bmw-2er-all",
            "url": INSTAMOTION_SEARCH,
            "max_pages": 3,
            "detail_limit": 4,
            "request_limit": 8,
        },
        {
            "source": "carwow_de",
            "key": "de-carwow-active-tourer-all",
            "url": CARWOW_SEARCH,
            "max_pages": 6,
            "detail_limit": 1,
            "request_limit": 16,
        },
    ]
    if include_controls:
        from .germany_additional_public import FACEBOOK_PUBLIC, MARKT_SEARCH

        queries.extend(
            [
                {
                    "source": "instamotion_de",
                    "key": "de-instamotion-bmw-218-control",
                    "url": INSTAMOTION_MODEL,
                    "max_pages": 1,
                    "detail_limit": 4,
                    "request_limit": 5,
                },
                {
                    "source": "markt_de",
                    "key": "de-markt-218i-active-tourer",
                    "url": MARKT_SEARCH,
                    "max_pages": 6,
                    "detail_limit": 1,
                    "request_limit": 12,
                },
                {
                    "source": "facebook_de",
                    "key": "de-facebook-public-berlin-unfiltered-verification",
                    "url": FACEBOOK_PUBLIC,
                    "max_pages": 1,
                    "detail_limit": 1,
                    "request_limit": 1,
                    "enabled": False,
                    "scope_status": "target_query_unverified",
                },
            ]
        )
    return queries


def _instamotion_item(own, url):
    identity, slug = str(own.get("id") or ""), own.get("slug")
    if not identity.isdigit() or not isinstance(slug, str):
        return None
    expected = f"https://www.instamotion.com/de/auto/{identity}/{slug}"
    if not _public("instamotion_de", expected) or expected != url:
        return None
    location = _object(own.get("location_country"))
    country = location.get("iso_code")
    features = {
        f.get("const_key"): f.get("label")
        for f in _list(own.get("catalog_features"))
        if isinstance(f, dict) and isinstance(f.get("const_key"), str)
    }
    pricing = _object(own.get("price_information"))
    nice = _object(pricing.get("nice_price_data"))
    # The detail price is here; price_data is a different price that is not displayed.
    gross = _amount(nice.get("price"))
    net = _amount(nice.get("price_without_vat"))
    currency = _object(pricing.get("currency")).get("name")
    raw = {
        "own_inventory_record": own,
        "model_brand": _object(own.get("make")).get("label"),
        "model_family": _object(own.get("model")).get("label"),
        "price_currency": currency or "UNKNOWN",
        "price_gross_eur": gross if currency == "EUR" else None,
        "price_net_eur": net if currency == "EUR" else None,
        "price_monthly_eur": None,
        "price_lease_monthly_eur": None,
        "dealer_stock_number": own.get("external_id"),
        "country_evidence": {
            "path": "location_country.iso_code",
            "value": country,
            "location": location,
        },
        "publication_reference": _reference("instamotion_de", expected, identity),
        "condition": [v for k, v in features.items() if k.startswith("CONDITION_")],
        "only_for_export": own.get("only_for_export"),
        "availability_status": "sold"
        if own.get("sold_at") or own.get("status") == "sold"
        else "available"
        if own.get("status") == "active"
        else "unknown",
    }
    if isinstance(country, str) and country:
        raw["market_country"] = country.upper()
    if own.get("external_url"):
        # Retain the explicit link; it does not prove the identity of another listing.
        raw["explicit_publisher_url"] = own["external_url"]
    registration = str(own.get("registration_date") or "")
    try:
        year = date.fromisoformat(registration).year
    except ValueError:
        year = None
    description = own.get("description")
    item = Listing(
        "instamotion_de",
        identity,
        expected,
        str(own.get("title") or ""),
        price=gross if gross is not None else net,
        price_type="advertised_price" if gross is not None else "net_price" if net else "unknown",
        mileage=_integer(own.get("mileage")),
        year=year,
        fuel=fuel(" ".join(str(v or "") for k, v in features.items() if k.startswith("FUELTYPE_"))),
        transmission=gearbox(
            " ".join(str(v or "") for k, v in features.items() if k.startswith("TRANSMISSION_"))
        ),
        vin=normalized_vin(own.get("vin")),
        seller=_object(own.get("seller")).get("name"),
        city=own.get("location_city"),
        description=BeautifulSoup(description, "html.parser").get_text(" ", strip=True)
        if isinstance(description, str)
        else None,
        raw=raw,
    )
    if re.search(r"\b218i\b", item.title, re.IGNORECASE) and not re.search(
        r"active\s*tour|gran\s*(?:tour|coup)|\bcoup", item.title, re.IGNORECASE
    ):
        raw["model_status"] = "body_unconfirmed"
    if own.get("hidden") is True or own.get("status") not in {"active", "sold"}:
        raw["inventory_status"] = "unavailable_or_unverified"
    if own.get("only_for_export") is True:
        item.price_type = "export_price"
        raw["price_warning"] = "export_only"
    return qualify_german_item(item)


def _parse_instamotion(soup, url):
    node = soup.select_one('script#__NEXT_DATA__[type="application/json"]')
    try:
        doc = json.loads(node.get_text()) if node else {}
    except ValueError:
        return Page([], search_filters={"parse_status": "invalid_state"})
    props = _object(_object(doc.get("props")).get("pageProps"))
    route = _object(props.get("routerState"))
    query = _object(route.get("query"))
    parsed = urlsplit(url)
    detail = re.fullmatch(r"/de/auto/(\d+)/([a-z0-9-]+)", parsed.path)
    if detail:
        own = _object(props.get("carData"))
        if (
            str(props.get("carId")) != detail[1]
            or str(query.get("carId")) != detail[1]
            or str(own.get("id")) != detail[1]
            or own.get("slug") != detail[2]
        ):
            return Page([], search_filters={"parse_status": "detail_identity_mismatch"})
        item = _instamotion_item(own, url)
        if item and props.get("carUnavailable") is True:
            item.raw["inventory_status"] = "unavailable_or_unverified"
        return Page([item] if item else [], search_filters={"own_detail": True})
    if query.get("filters") not in (["bmw", "group-2-series"], ["bmw", "218"]):
        return Page([], search_filters={"parse_status": "search_filter_unverified"})
    current = _integer(query.get("page", 1))
    if current != _integer(parse_qs(parsed.query).get("page", [1])[0]) or not current:
        return Page([], search_filters={"parse_status": "page_identity_mismatch"})
    results = _object(props.get("searchResults"))
    total = _integer(results.get("total"))
    anchors = {urljoin(url, a["href"]) for a in soup.select("a[href]")}
    items = []
    records = _list(results.get("cars"))
    for own in records:
        if not isinstance(own, dict):
            continue
        item_url = f"https://www.instamotion.com/de/auto/{own.get('id')}/{own.get('slug')}"
        if item_url in anchors and (item := _instamotion_item(own, item_url)):
            items.append(item)
    links = sorted(
        u
        for u in anchors
        if _public("instamotion_de", u) and urlsplit(u).path == "/de/autos" and "page=" in u
    )
    pages = (
        max(
            [_integer(parse_qs(urlsplit(u).query).get("page", [0])[0]) or 0 for u in links]
            + [current or 0]
        )
        or None
    )
    return Page(
        items,
        total,
        pages,
        current,
        links,
        {
            "native_model_group": "MAKE_BMW-MODELFAMILYGROUP_SERIES_2"
            if query["filters"][1] == "group-2-series"
            else None,
            "native_model": "MAKE_BMW-MODELFAMILY_218" if query["filters"][1] == "218" else None,
            "country_policy": "own_location_country_required",
            "counter_scope": "own_native_model_filter",
            "retained_ids": [str(r["id"]) for r in records if isinstance(r, dict) and "id" in r],
            "id_binding_gap": len(records) - len(items),
        },
    )


def _parse_instamotion_native(body, url):
    try:
        document = json.loads(body)
    except (ValueError, TypeError):
        return Page([], search_filters={"parse_status": "invalid_native_json"})
    props = _object(document.get("pageProps"))
    own_query = _object(_object(props.get("routerState")).get("query"))
    request = parse_qs(urlsplit(url).query)
    filters = request.get("filters")
    current = _integer(request.get("page", [1])[0])
    if (
        filters != ["bmw", "group-2-series"]
        or own_query.get("filters") != filters
        or _integer(own_query.get("page", 1)) != current
        or not current
    ):
        return Page([], search_filters={"parse_status": "native_filter_or_page_mismatch"})
    results = _object(props.get("searchResults"))
    total = _integer(results.get("total"))
    records = _list(results.get("cars"))
    items = []
    for own in records:
        if isinstance(own, dict):
            item_url = f"https://www.instamotion.com/de/auto/{own.get('id')}/{own.get('slug')}"
            if item := _instamotion_item(own, item_url):
                item.raw["identity_binding"] = "native_inventory_id_and_slug_observed_in_own_ui"
                items.append(item)
    pages = math.ceil(total / 20) if total else 1 if total == 0 else None
    links = []
    if pages:
        for number in range(1, pages + 1):
            links.append(
                url.split("?")[0]
                + "?"
                + urlencode(
                    [
                        ("filters", "bmw"),
                        ("filters", "group-2-series"),
                        ("page", number),
                    ]
                )
            )
    return Page(
        items,
        total,
        pages,
        current,
        links,
        {
            "native_paging": True,
            "native_limit": 20,
            "native_limit_proof": "own visible pagination links limit=20 and own UI page2 JSON request",
            "retained_ids": [str(x["id"]) for x in records if isinstance(x, dict) and "id" in x],
            "id_binding_gap": len(records) - len(items),
            "counter_scope": "own_native_model_filter",
            "banner_counter_semantics": "unproven",
        },
    )


def _carwow_detail(soup, url, identity):
    # The cash/PCH section ID binds the VDP even without a canonical URL.
    bound = soup.find(id="cash-offer-breakdown-" + identity) or soup.find(
        id="pch-offer-breakdown-" + identity
    )
    heading = soup.select_one("h1.deal-title__main")
    if not bound or not heading:
        return Page([], search_filters={"parse_status": "detail_identity_unverified"})
    values = {}
    for dl in soup.select(".deal-details__key-information dl.summary-list"):
        for term in dl.select("dt"):
            definition = term.find_next_sibling("dd")
            if definition:
                values[_text(term)] = _text(definition)
    cash = soup.select_one(".deal-pricing-info__cash")
    price = cash.select_one(".deal-pricing__carwow-price") if cash else None
    tax = _text(cash.select_one(".deal-pricing__tax-rate")) if cash else ""
    gross = number(_text(price)) if price and re.search(r"inkl.*Mehrwertsteuer", tax) else None
    raw = {
        "publication_reference": _reference("carwow_de", url, identity),
        "price_currency": "EUR",
        "price_gross_eur": gross,
        "price_lease_text": _text(soup.select_one(".deal-pricing-info__pch")) or None,
        "price_net_eur": None,
        "own_detail_values": values,
        "country_evidence": None,
        "geography_status": "needs_confirmation",
        "country_policy": "site_country_origin_and_hsn_are_not_stock_country",
    }
    item = Listing(
        "carwow_de",
        identity,
        url,
        _text(heading),
        price=gross,
        price_type="advertised_price" if gross is not None else "unknown",
        year=_integer(values.get("Jahr")),
        mileage=number(values.get("Kilometerstand")),
        fuel=fuel(values.get("Kraftstoff")),
        transmission=gearbox(values.get("Getriebe")),
        raw=raw,
    )
    # The captured Carwow VDP has no VIN field. Do not read neighboring listings.
    return Page([qualify_german_item(item)], search_filters={"own_detail": True})


def _parse_carwow(soup, url):
    parsed = urlsplit(url)
    detail = re.fullmatch(r"/deals/([a-f0-9]{24,32})", parsed.path)
    if detail:
        return _carwow_detail(soup, url, detail[1])
    frame = soup.select_one("turbo-frame#stock_cars_v2_cards[src]")
    if frame:
        native = urljoin(url, frame["src"])
        return Page([], search_filters={"native_payload_url": native})
    node = soup.select_one("[" + _CW_ATTR + "count-value]")
    if not node:
        return Page([], search_filters={"parse_status": "inventory_not_loaded"})
    try:
        identities = json.loads(node.get(_CW_ATTR + "deal-ids-value", "null"))
        records = json.loads(node.get(_CW_ATTR + "ecommerce-items-value", "null"))
    except ValueError:
        return Page([], search_filters={"parse_status": "invalid_inventory"})
    if not isinstance(identities, list) or not isinstance(records, list):
        return Page([], search_filters={"parse_status": "invalid_inventory"})
    rows = {r.get("item_id"): r for r in records if isinstance(r, dict)}
    cards = {
        f.get("id", "").removeprefix("lazy_deal_card_"): urljoin(url, f["src"])
        for f in node.select('turbo-frame[id^="lazy_deal_card_"][src]')
    }
    items = []
    for identity in identities:
        if not isinstance(identity, str) or identity not in rows or identity not in cards:
            continue
        card = cards[identity]
        if not _public("carwow_de", card) or parse_qs(urlsplit(card).query).get("id") != [identity]:
            continue
        own = rows[identity]
        item = Listing(
            "carwow_de",
            identity,
            card,
            str(own.get("item_name") or ""),
            year=_integer(own.get("year_registered")),
            price_type="unknown",
            raw={
                "own_inventory_record": own,
                "native_card_url": card,
                "country_evidence": None,
                "discovery_amount": _amount(own.get("price")),
                "discovery_only": True,
            },
        )
        items.append(qualify_german_item(item))
    links = sorted(
        {
            urljoin(url, a["href"])
            for a in soup.select("a[data-page][href]")
            if _public("carwow_de", urljoin(url, a["href"]))
        }
    )
    pages = (
        max([_integer(parse_qs(urlsplit(u).query).get("page", [0])[0]) or 0 for u in links] + [0])
        or None
    )
    return Page(
        items,
        _integer(node.get(_CW_ATTR + "count-value")),
        pages,
        _integer(node.get(_CW_ATTR + "current-page-value")),
        links,
        {
            "country_policy": "own_detail_country_required",
            "retained_ids": identities,
            "id_binding_gap": len(identities) - len(items),
            "discovery_only": True,
        },
    )


def _next_scope(source, url, page_number):
    """A page from native links retains the original scope without adding restrictions."""
    p = urlsplit(url)
    q = parse_qs(p.query)
    if q.pop("page", None) != [str(page_number)]:
        return False
    if source == "carwow_de":
        return p.path == "/stock_cars" and q == parse_qs(urlsplit(CARWOW_SEARCH).query)
    if source == "instamotion_de" and "/_next/data/" in p.path:
        return q == {"filters": ["bmw", "group-2-series"]}
    return p.path == "/de/autos" and q == {
        "model-group[]": ["MAKE_BMW-MODELFAMILYGROUP_SERIES_2"],
        "sort": ["publish-date"],
        "direction": ["desc"],
        "limit": ["20"],
    }


def _retained_body(source, body):
    """Remove embedded keys, anonymous CSRF and session state before raw/SQLite storage.

    Retain the wire SHA separately. The public projection keeps the source
    inventory records and observed native links; authentication fields are unnecessary.
    """
    if source == "markt_de":
        from .germany_additional_public import retain_public_markup

        return retain_public_markup(body)
    if source == "facebook_de" and (body or "").lstrip().startswith("{"):
        return body
    if source == "instamotion_de" and (body or "").lstrip().startswith("{"):
        try:
            doc = json.loads(body)
            props = _object(doc.get("pageProps"))
            return json.dumps(
                {
                    "pageProps": {
                        k: v
                        for k, v in props.items()
                        if k in {"routerState", "searchResults", "searchBanners"}
                    }
                }
            )
        except ValueError:
            return "{}"
    body = re.sub(r"AIza[0-9A-Za-z_-]{35}", "[REDACTED_PUBLIC_EMBED_KEY]", body or "")
    soup = BeautifulSoup(body, "html.parser")
    for element in soup.select(
        'iframe,meta[name="csrf-token"],input[name="authenticity_token"],'
        'input[name="csrf_token"],input[name="fb_dtsg"],input[name="lsd"]'
    ):
        element.decompose()
    for script in soup.select("script"):
        if source == "instamotion_de" and script.get("id") == "__NEXT_DATA__":
            try:
                doc = json.loads(script.get_text())
                props = _object(_object(doc.get("props")).get("pageProps"))
                keys = {"routerState", "searchResults", "carId", "carUnavailable", "carData"}
                script.string = json.dumps(
                    {
                        "page": doc.get("page"),
                        "query": doc.get("query"),
                        "buildId": doc.get("buildId"),
                        "props": {"pageProps": {k: v for k, v in props.items() if k in keys}},
                    }
                )
            except (ValueError, AttributeError):
                script.decompose()
        else:
            script.decompose()
    return str(soup)


def parse_additional(source, body, url):
    if source not in SOURCES or not _public(source, url):
        return Page([], search_filters={"parse_status": "invalid_source_or_route"})
    if source in {"markt_de", "facebook_de"}:
        from .germany_additional_public import parse_facebook_projection, parse_markt

        return (
            parse_markt(body, url) if source == "markt_de" else parse_facebook_projection(body, url)
        )
    if source == "instamotion_de" and "/_next/data/" in urlsplit(url).path:
        return _parse_instamotion_native(body, url)
    soup = BeautifulSoup(body or "", "html.parser")
    return _parse_instamotion(soup, url) if source == "instamotion_de" else _parse_carwow(soup, url)


def _validate_query(query):
    source, url = query.get("source"), query.get("url")
    if not _public(source, url):
        raise ValueError("invalid_additional_query")
    p = urlsplit(url)
    if source == "instamotion_de":
        valid = (
            p.path in {urlsplit(INSTAMOTION_SEARCH).path, urlsplit(INSTAMOTION_MODEL).path}
            and not p.query
        )
    elif source == "markt_de":
        from .germany_additional_public import MARKT_SEARCH

        valid = url == MARKT_SEARCH
    elif source == "facebook_de":
        from .germany_additional_public import FACEBOOK_PUBLIC

        valid = url == FACEBOOK_PUBLIC
    else:
        valid = p.path == "/stock_cars" and parse_qs(p.query) == parse_qs(
            urlsplit(CARWOW_SEARCH).query
        )
    if not valid:
        raise ValueError("unverified_additional_search_scope")
    for key, maximum in [("max_pages", 100), ("detail_limit", 200), ("request_limit", 500)]:
        value = query.get(key, 1)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= maximum:
            raise ValueError("invalid_additional_budget")


def collect_additional(collector, run, query):
    """Separate entry point using fetcher/fetch_record/DE Database from Collector.

    Save the frontier before details. An incomplete crawl cannot remove missing listings.
    Carwow does not yet write observations: its stock country has not been obtained.
    """
    if getattr(collector, "country", None) != "DE":
        raise ValueError("additional_requires_german_collector")
    _validate_query(query)
    source, key, started = query["source"], query["key"], now()
    ledger = {
        "source": source,
        "query_key": key,
        "pages": [],
        "cards": {},
        "captures": [],
        "details": {},
        "errors": [],
        "requests": 0,
        "infer_missing": False,
    }
    expected, seen, targets = None, set(), set()

    def detail_facts(entry):
        facts = {
            field: entry.get(field)
            for field in ("price", "year", "mileage", "fuel", "transmission", "vin")
        }
        raw = entry.get("raw") or {}
        own = raw.get("own_inventory_record") or {}
        # Only Instamotion's ID-bound native schema proves these stable fields.
        # Card title, country of the site and another publication do not.
        if source == "instamotion_de" and str(own.get("id")) == entry["id"]:
            for field in ("model_brand", "model_family"):
                value = raw.get(field)
                if isinstance(value, str) and value.strip():
                    facts["raw." + field] = value
            for field in ("make", "model"):
                value = _object(own.get(field)).get("const_key")
                if isinstance(value, str) and value.strip():
                    facts["raw.own_inventory_record." + field + ".const_key"] = value
            country = _object(own.get("location_country")).get("iso_code")
            if isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country):
                facts["raw.market_country"] = raw.get("market_country")
        return facts

    def persist():
        collector.db.conn.execute(
            "INSERT OR REPLACE INTO monitor_state VALUES (?,?)",
            ("germany_additional:" + key, json.dumps(ledger, ensure_ascii=False)),
        )
        collector.db.conn.commit()

    def capture(response):
        wire_hash = (
            getattr(response, "source_body_sha256", None)
            or hashlib.sha256((response.body or "").encode()).hexdigest()
        )
        response = dataclasses.replace(
            response, body=_retained_body(source, response.body)
        )
        fetch_id = collector.fetch_record(run, source, response, retain_raw=True)
        for extra in getattr(response, "extra_responses", []):
            extra_id = collector.fetch_record(run, "markt_de", extra, retain_raw=True)
            for proof in (response.country_publication or {}).get("proofs", []):
                if (
                    proof["final_uri"] == extra.final_url
                    and proof["method"] == extra.request_method
                ):
                    proof["fetch_id"] = extra_id
        for proof in (getattr(response, "country_publication", None) or {}).get("proofs", []):
            if proof["final_uri"] == response.final_url:
                proof["fetch_id"] = fetch_id
        ledger["captures"].append(
            {
                "fetch_id": fetch_id,
                "date": response.timestamp,
                "request_url": response.url,
                "final_uri": response.final_url,
                "source_body_sha256": wire_hash,
                "retained_body_sha256": hashlib.sha256(response.body.encode()).hexdigest(),
                "retention": "public_inventory_projection_no_session_or_embedded_keys",
            }
        )
        return response, fetch_id

    def fetch(url, request_body=None, *, entry=None, replay=False):
        if not _public(source, url):
            raise ValueError("invalid_native_route")
        policy = getattr(collector, "specialized_detail_policy", None)
        if entry is not None and callable(policy):
            try:
                result = policy(run).fetch(
                    source, url, identity=entry["id"], capture=capture, replay=replay,
                    query_remaining=query.get("request_limit", 1) - ledger["requests"],
                    facts=detail_facts(entry) if not replay else None,
                )
            except BaseException as error:
                ledger["requests"] += getattr(error, "detail_policy_requests", 0)
                raise
            ledger["requests"] += result.requests
            response, fetch_id = result.response, result.fetch_id
            if result.cached:
                ledger["captures"].append({"fetch_id": fetch_id, "date": response.timestamp,
                                           "request_url": url, "final_uri": response.final_url,
                                           "retained_body_sha256": hashlib.sha256(response.body.encode()).hexdigest(), "cached": True})
        else:
            if ledger["requests"] >= query.get("request_limit", 1):
                raise ValueError("additional_request_limit")
            ledger["requests"] += 1
            if request_body is not None:
                hop = collector.fetcher.post_public_navigation(url, request_body)
                if not hop.redirect_to or not _public(source, hop.redirect_to):
                    raise ValueError("public_pagination_redirect_unverified")
                collector.fetch_record(run, source, hop, retain_raw=True)
                response = collector.fetcher.get(hop.redirect_to)
            else:
                response = collector.fetcher.get(url)
            response, fetch_id = capture(response)
        if response.error or response.status != 200 or not _public(source, response.final_url):
            raise ValueError(response.error or f"http_or_redirect_{response.status}")
        return response, fetch_id

    def observe(item, response, fetch_id):
        from .germany_additional_public import apply_country_publication, response_proof

        apply_country_publication(item, response)
        item.raw["field_evidence"] = {
            field: {
                "value": value,
                "advert_id": item.external_id,
                "own_url": item.url,
                "fetch_id": fetch_id,
                **response_proof(response),
            }
            for field, value in {
                "title": item.title,
                "price": item.price,
                "vin": item.vin,
                "year": item.year,
                "mileage": item.mileage,
                "fuel": item.fuel,
                "transmission": item.transmission,
            }.items()
            if value is not None
        }
        if source == "facebook_de" and not item.raw.get("query_verified"):
            return
        if item.raw.get("market_country") != "DE" or item.raw.get("inventory_status"):
            return
        identity = observe_german_item(collector.db, run, item, response.timestamp, fetch_id, key)
        if identity is not None and item.scope == "target":
            targets.add(item.external_id)
        return identity

    url, request_body = query["url"], None
    first_response, first_page = None, None
    last = None
    for page_number in range(1, query.get("max_pages", 1) + 1):
        try:
            response, fetch_id = fetch(url, request_body)
            page = parse_additional(source, response.body, response.final_url)
            native = page.search_filters.get("native_payload_url")
            if native:
                if parse_qs(urlsplit(native).query) != parse_qs(urlsplit(response.final_url).query):
                    raise ValueError("native_frame_filter_drift")
                response, fetch_id = fetch(native)
                page = parse_additional(source, response.body, response.final_url)
            if source == "facebook_de":
                for item in page.listings:
                    seen.add(item.external_id)
                    ledger["cards"][item.external_id] = {
                        "id": item.external_id,
                        "url": item.url,
                        "title": item.title,
                        "scope": "unverified_query",
                        "market_country": item.raw.get("market_country", "UNKNOWN"),
                        "raw": item.raw,
                        "fetch_id": fetch_id,
                    }
                ledger["errors"].append("target_query_scope_unverified")
                persist()
                break
            if page.page != page_number or page.total is None or page.pages is None:
                raise ValueError("counter_or_page_unverified")
            if page.pages < page.page or (
                source == "carwow_de"
                and page.page
                != _integer(parse_qs(urlsplit(response.final_url).query).get("page", [1])[0])
            ):
                raise ValueError("page_identity_mismatch")
            if expected is not None and expected != page.total:
                ledger["errors"].append("counter_changed")
            expected, last = page.total, page.pages
            if first_page is None:
                first_page, first_response = page, response
            if page.search_filters.get("id_binding_gap"):
                ledger["errors"].append("id_binding_gap")
            identities = [i.external_id for i in page.listings]
            if seen.intersection(identities) or len(set(identities)) != len(identities):
                ledger["errors"].append("repeated_inventory_ids")
            ledger["pages"].append(
                {
                    "page": page.page,
                    "total": page.total,
                    "pages": page.pages,
                    "fetch_id": fetch_id,
                    "url": response.final_url,
                    "ids": identities,
                    "native_ids": page.search_filters.get("retained_ids", identities),
                }
            )
            for item in page.listings:
                seen.add(item.external_id)
                ledger["cards"].setdefault(
                    item.external_id,
                    {
                        "id": item.external_id,
                        "url": item.url,
                        "title": item.title,
                        "scope": item.scope,
                        "market_country": item.raw.get("market_country", "UNKNOWN"),
                        "model_status": item.raw.get("model_status"),
                        "vin": item.vin,
                        "price": item.price,
                        "price_type": item.price_type,
                        "year": item.year,
                        "mileage": item.mileage,
                        "fuel": item.fuel,
                        "transmission": item.transmission,
                        "raw": item.raw,
                        "fetch_id": fetch_id,
                        "fetched_at": response.timestamp,
                    },
                )
                try:
                    observe(item, response, fetch_id)
                except ValueError as error:
                    # Identity contradictions cannot erase the remaining source census.
                    ledger.setdefault("search_observation_errors", {})[
                        item.external_id
                    ] = {
                        "status": "unresolved",
                        "reason": str(error),
                        "fetch_id": fetch_id,
                        "fetched_at": response.timestamp,
                    }
                    ledger["errors"].append(
                        f"search_observation:{item.external_id}:{error}"
                    )
            persist()
            if page_number >= last:
                break
            if source == "markt_de" or (
                source == "instamotion_de" and query.get("native_paging")
            ):
                from .germany_additional_public import additional_page_request

                request = additional_page_request(
                    source,
                    query["url"],
                    page_number + 1,
                    first_page,
                    first_response.body,
                )
                url, request_body = request["url"], request.get("body")
                continue
            candidates = [
                u
                for u in page.links
                if parse_qs(urlsplit(u).query).get("page") == [str(page_number + 1)]
            ]
            if len(candidates) != 1:
                raise ValueError("native_next_link_unverified")
            if not _next_scope(source, candidates[0], page_number + 1):
                raise ValueError("native_next_filter_drift")
            url = candidates[0]
        except Exception as exc:  # noqa: BLE001 — a block does not prove an empty result set
            ledger["errors"].append(f"search:{page_number}:{exc}")
            break
    if last and len(ledger["pages"]) < last:
        ledger["errors"].append("page_budget_or_incomplete")
    if expected is not None and len(seen) != expected:
        ledger["errors"].append(f"counter_gap:{expected}:{len(seen)}")
    # Conclusive ID-bound country/make/model facts precede optional body hints.
    for entry in ledger["cards"].values():
        country = detail_facts(entry).get("raw.market_country")
        raw = entry.get("raw") or {}
        own = raw.get("own_inventory_record") or {}
        reason, status = None, None
        if country is not None and country != "DE":
            reason, status = "search_country_excluded", "foreign_country"
        elif (
            source == "instamotion_de"
            and str(own.get("id")) == entry["id"]
            and entry["scope"] == "excluded"
        ):
            make = _object(own.get("make")).get("const_key")
            model = _object(own.get("model")).get("const_key")
            if (
                isinstance(make, str)
                and make.startswith("MAKE_")
                and make not in {"MAKE_BMW", "MAKE_UNKNOWN", "MAKE_OTHER"}
            ):
                reason, status = "search_brand_excluded", "excluded_model"
            elif isinstance(model, str) and re.fullmatch(
                r"MAKE_BMW-MODELFAMILY_(?:216|220|223|225|230|M2|M235|M240i)", model
            ):
                reason, status = "search_model_excluded", "excluded_model"
        if reason:
            ledger.setdefault("search_exclusions", {})[entry["id"]] = {
                "status": status,
                "scope": "excluded",
                "country": entry["market_country"],
                "reason": reason,
                "evidence": "bound_native_search",
                "fetch_id": entry["fetch_id"],
                "fetched_at": entry["fetched_at"],
                "url": entry["url"],
                "cached": False,
            }
    frontier = [
        entry
        for entry in ledger["cards"].values()
        if entry["id"] not in ledger.get("search_exclusions", {})
        and (
            source in {"carwow_de", "markt_de"}
            or entry["scope"] == "target"
            or entry.get("model_status")
        )
    ]
    frontier.sort(key=lambda entry: entry["scope"] != "target")
    for entry in frontier[: query.get("detail_limit", 1)]:
        try:
            response, fetch_id = fetch(
                entry["url"], entry=entry, replay=source == "carwow_de"
            )
            if source == "carwow_de":
                soup = BeautifulSoup(response.body or "", "html.parser")
                frame = soup.find("turbo-frame", id="lazy_deal_card_" + entry["id"])
                link = frame.select_one("a.deal-card__link[href]") if frame else None
                own_url = urljoin(response.final_url, link["href"]) if link else ""
                if (
                    not _public(source, own_url)
                    or urlsplit(own_url).path != "/deals/" + entry["id"]
                ):
                    raise ValueError("own_card_publication_unverified")
                entry["publication_reference"] = _reference(
                    source, own_url, entry["id"]
                )
                persist()
                response, fetch_id = fetch(own_url, entry=entry)
            if getattr(response, "detail_policy_accepted", False):
                item = Listing(**response.detail_policy_item)
                if item.scope == "target":
                    targets.add(item.external_id)
            else:
                page = parse_additional(source, response.body, response.final_url)
                if (
                    len(page.listings) != 1
                    or page.listings[0].external_id != entry["id"]
                ):
                    raise ValueError("own_detail_identity_unverified")
                item = page.listings[0]
                from .bmw_detail_policy import DetailDeferred, detail_fact_conflicts

                conflicts = detail_fact_conflicts(
                    detail_facts(entry), dataclasses.asdict(item), require_both=True
                )
                if conflicts:
                    error = DetailDeferred(
                        "detail_search_conflict:" + ",".join(conflicts)
                    )
                    error.capture = {
                        "fetch_id": fetch_id,
                        "fetched_at": response.timestamp,
                        "content_hash": hashlib.sha256(
                            response.body.encode()
                        ).hexdigest(),
                    }
                    raise error
                listing_id = observe(item, response, fetch_id)
                if callable(getattr(collector, "specialized_detail_policy", None)):
                    collector.specialized_detail_policy(run).accept_observation(
                        item, response.timestamp, fetch_id, listing_id, key
                    )
                if (
                    item.raw.get("market_country") == "DE"
                    and item.scope != "excluded"
                    and listing_id is None
                ):
                    error = DetailDeferred("detail_observation_pending")
                    error.capture = {
                        "fetch_id": fetch_id,
                        "fetched_at": response.timestamp,
                        "content_hash": hashlib.sha256(
                            response.body.encode()
                        ).hexdigest(),
                    }
                    raise error
            ledger["details"][entry["id"]] = {
                "fetch_id": fetch_id,
                "url": item.url,
                "scope": item.scope,
                "country": item.raw.get("market_country", "UNKNOWN"),
                "status": "own_de"
                if item.raw.get("market_country") == "DE"
                else "held_country"
                if item.raw.get("market_country") in {None, "UNKNOWN"}
                else "foreign_country",
                "vin": item.vin,
                "price": item.price,
                "price_type": item.price_type,
                "raw": item.raw,
                "cached": bool(getattr(response, "detail_policy_cached", False)),
                "fetched_at": response.timestamp,
            }
            if source == "markt_de":
                from .germany_additional_public import (
                    carwow_publication_redirect,
                    markt_carwow_navigation,
                )

                navigation = markt_carwow_navigation(response.body, response.final_url)
                if navigation:
                    try:
                        policy = getattr(collector, "specialized_detail_policy", None)
                        if callable(policy):
                            result = policy(run).fetch(source, navigation["url"], cache=False,
                                query_remaining=query.get("request_limit", 1) - ledger["requests"],
                                request=lambda: collector.fetcher.post_public_navigation(navigation["url"], navigation["body"]))
                            ledger["requests"] += result.requests
                            nav, nav_fetch = result.response, result.fetch_id
                        else:
                            nav = collector.fetcher.post_public_navigation(navigation["url"], navigation["body"])
                            nav_fetch = collector.fetch_record(run, source, nav, retain_raw=True)
                        publication = carwow_publication_redirect(nav)
                        if publication:
                            state_key = "germany_country_publications:carwow_de"
                            previous = collector.db.conn.execute(
                                "SELECT value FROM monitor_state WHERE key=?", (state_key,)
                            ).fetchone()
                            mappings = json.loads(previous[0]) if previous else {}
                            proof = {
                                "claim_id": item.external_id,
                                "claim_url": item.url,
                                "publisher_uri": publication["url"],
                                "detail_fetch_id": fetch_id,
                                "navigation_fetch_id": nav_fetch,
                                "fetched_at": nav.timestamp,
                            }
                            mappings[publication["external_id"]] = proof
                            collector.db.conn.execute(
                                "INSERT OR REPLACE INTO monitor_state VALUES (?,?)",
                                (state_key, json.dumps(mappings)),
                            )
                            ledger["details"][entry["id"]]["publisher"] = proof
                    except Exception as exc:  # noqa: BLE001 — own detail remains retained
                        from .bmw_detail_policy import DetailDeferred, deferred_record

                        if isinstance(exc, DetailDeferred):
                            ledger.setdefault("publisher_deferrals", {})[entry["id"]] = deferred_record(exc, navigation["url"])
                        ledger["requests"] += getattr(exc, "detail_policy_requests", 0)
                        ledger["errors"].append(f"publisher:{entry['id']}:{exc}")
        except Exception as exc:  # noqa: BLE001 — retain the frontier until the next pass
            from .bmw_detail_policy import DetailDeferred, deferred_record

            ledger["details"][entry["id"]] = deferred_record(exc, entry["url"]) if isinstance(exc, DetailDeferred) else {"status": "failed", "error": str(exc)}
            ledger["errors"].append(f"detail:{entry['id']}:{exc}")
        persist()
    enumeration_complete = (
        expected is not None
        and len(seen) == expected
        and len(ledger["pages"]) == last
        and not any(
            e.startswith(("search:", "counter_", "repeated_", "page_budget", "id_binding"))
            for e in ledger["errors"]
        )
    )
    resolved_frontier = sum(entry["id"] in ledger["details"] for entry in frontier)
    if resolved_frontier < len(frontier):
        ledger["errors"].append(
            f"detail_frontier_incomplete:{resolved_frontier}:{len(frontier)}"
        )
    ledger["pending_details"] = {
        entry["id"]: ledger["details"].get(entry["id"], {"status": "deferred", "url": entry["url"], "reason": "query_detail_limit"})
        for entry in frontier
        if ledger["details"].get(entry["id"], {}).get("status") not in {"own_de", "foreign_country"}
    }
    unconfirmed_country = sum(x.get("status") == "held_country" for x in ledger["details"].values())
    unconfirmed_models = sum(
        x.get("country") == "DE" and x.get("scope") == "needs_confirmation"
        for x in ledger["details"].values()
    )
    if unconfirmed_country:
        ledger["errors"].append(f"country_unconfirmed_details:{unconfirmed_country}")
    if unconfirmed_models:
        ledger["errors"].append(f"model_unconfirmed_details:{unconfirmed_models}")
    ledger.update(
        enumeration_status="complete" if enumeration_complete else "partial",
        http_requests=getattr(collector.fetcher, "http_requests", None),
        expected_count=expected,
        received_count=len(seen),
        target_count=len(targets),
        coverage_status="partial" if ledger["errors"] else "enumerated_scope_only",
        observation_status="own_de_only"
        if targets
        else "held_country"
        if source == "carwow_de"
        else "held_or_no_verified_target",
    )
    persist()
    collector.db.finish_source(
        run,
        source,
        key,
        started,
        "partial" if ledger["errors"] or (source == "carwow_de" and not targets) else "complete",
        expected,
        len(seen),
        len(targets),
        len(ledger["pages"]),
        ";".join(ledger["errors"]) or None,
        infer_missing=False,
    )
    return ledger
