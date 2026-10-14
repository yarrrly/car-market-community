"""Public PKW.de and Autohero DE: native responses from 07.10.2026.

The parser reads only typed inventory records. Autohero pagination uses
observed UI GraphQL; PKW uses a GET API while preserving the filter.
"""

import json
import math
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .identity import normalized_vin
from .parse import Listing, Page, fuel, gearbox

PKW_SEARCH = "https://www.pkw.de/api/v1/cars/search/basic?brand_model=9_2326&source=autokatalog"
AUTOHERO_SEARCH = "https://www.autohero.com/de/search/?brand0=bmw&models0=2er"
AUTOHERO_API = "https://www.autohero.com/v1/retail-customer-gateway/graphql/searchAdV9AdsV2"
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"


def _dict(value):
    return value if isinstance(value, dict) else {}


def _amount(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        return None


def _integer(value):
    number = _amount(value)
    return int(number) if number is not None and number.is_integer() else None


def _year(value):
    match = re.match(r"^(\d{4})(?:-|\d{4}T|$)", str(value or ""))
    return int(match[1]) if match and 1886 <= int(match[1]) <= 2100 else None


def _money(value):
    value = _dict(value)
    amount, divisor = _amount(value.get("amountMinorUnits")), _amount(value.get("conversionMajor"))
    return amount / divisor if amount is not None and divisor else None


def _public(url, source):
    try:
        parsed = urlsplit(url)
        hosts = {
            "pkw_de": {"www.pkw.de", "suche.pkw.de"},
            "autohero_de": {"www.autohero.com", "autohero.com"},
        }
        route_ok = (
            source != "autohero_de"
            or parsed.path.startswith("/de/")
            or parsed.path == urlsplit(AUTOHERO_API).path
        )
        return (
            route_ok
            and parsed.scheme == "https"
            and parsed.hostname in hosts.get(source, set())
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )
    except (ValueError, TypeError):
        return False


def _own_id(url, source):
    if not _public(url, source):
        return None
    path = urlsplit(url).path
    pattern = (
        r"/(?:fahrzeuge/details|api/v1/cars)/(\d+)/?"
        if source == "pkw_de"
        else r"/de/[^/]+/id/(" + _UUID + r")/?"
    )
    match = re.fullmatch(pattern, path, re.IGNORECASE)
    return match[1].lower() if match else None


def _reference(source, url, identity):
    return {
        "source": source,
        "url": url,
        "external_id": identity,
        "kind": "explicit_own_publication_link",
    }


def _finish(items, total=None, pages=None, page=None, filters=None):
    # Native records confirm the page country; the domain is insufficient.
    filters = dict(filters or {})
    if items and all(x.raw.get("market_country") == "DE" for x in items):
        filters.update(country="DE", country_proof="own_inventory_records")
    from .germany import qualify_german_item

    return Page(
        [qualify_german_item(item) for item in items], total, pages, page, search_filters=filters
    )


def secondary_page_url(source, url, page):
    """PKW links.next drops the model: retain the entire original query."""
    if (
        source != "pkw_de"
        or not _public(url, source)
        or not isinstance(page, int)
        or isinstance(page, bool)
        or page < 1
    ):
        raise ValueError("invalid_secondary_page")
    parsed = urlsplit(url)
    if parsed.path not in ("/api/v1/cars/search/basic", "/fahrzeuge"):
        raise ValueError("invalid_pkw_search_route")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query.get("brand_model") != ["9_2326"]:
        raise ValueError("pkw_model_filter_required")
    query["page"] = [str(page)]
    return urlunsplit(parsed._replace(query=urlencode(query, doseq=True), fragment=""))


def secondary_page_request(source, url, page):
    """Describe an ordinary public request; no network access or cookies here.

    Autohero properties are retained from the actual UI request. Their effect
    on completeness needs separate verification; do not silently change firstPublishedDays.
    """
    if source == "pkw_de":
        return {"method": "GET", "url": secondary_page_url(source, url, page), "body": None}
    if (
        source != "autohero_de"
        or not _public(url, source)
        or not urlsplit(url).path.startswith("/de/search/")
        or not isinstance(page, int)
        or isinstance(page, bool)
        or page < 1
    ):
        raise ValueError("invalid_secondary_page")
    query = parse_qs(urlsplit(url).query)
    if query.get("brand0") != ["bmw"] or query.get("models0") != ["2er"]:
        raise ValueError("autohero_model_filter_required")
    terms = [
        {"field": "countryCode", "op": "eq", "value": "DE"},
        {
            "field": None,
            "op": "and",
            "value": [
                {"field": "make", "op": "eq", "value": "130"},
                {"field": "model", "op": "eq", "value": ["2er"]},
            ],
        },
    ]
    if "variants" in query:
        if query["variants"] != ["218i"] or query.get("subType") != [
            "218i,218i active tourer,218i gran coupe,218i gran tourer"
        ]:
            raise ValueError("unverified_autohero_variant_filter")
        terms.append(
            {
                "field": None,
                "op": "or",
                "value": [
                    {"field": "subType", "op": "eq", "value": query["subType"][0].split(",")},
                    {"field": "subTypeExtra", "op": "eq", "value": []},
                ],
            }
        )
    elif "subType" in query:
        raise ValueError("autohero_subtype_without_variants_is_ignored")
    search = {
        "offset": (page - 1) * 24,
        "limit": 24,
        "sort": "most_popular",
        "filter": {"field": None, "op": "and", "value": terms},
        "aggs": [],
        "postFilter": None,
        "fields": ["registration", "firstEligibleToBePurchasedAt", "variablePositioning"],
        "properties": {
            "firstPublishedDays": -30,
            "shuffleCategoryBResults": True,
            "resultsCombiner": "abbabbc",
            "filterByEligibleDate": True,
            "includeProspective": True,
        },
    }
    return {
        "method": "POST",
        "url": AUTOHERO_API,
        "body": {
            "operationName": "searchAdV9AdsV2",
            "variables": {"search": search},
            "query": "query searchAdV9AdsV2($search: EsSearchRequestProjectionInput!, $tradeInId: UUID) {\n  searchAdV9AdsV2(search: $search, tradeInId: $tradeInId)\n}",
        },
        "page": page,
    }


def _pkw_item(own):
    identity, url = str(own.get("id") or ""), own.get("url")
    if not identity.isdigit() or _own_id(url, "pkw_de") != identity:
        return None
    location, price = _dict(own.get("location")), _dict(own.get("price"))
    country = str(location.get("country") or "").upper()
    raw = {
        "own_inventory_record": own,
        "model_brand": _dict(own.get("brand")).get("name"),
        "model_family": _dict(own.get("model")).get("name"),
        "dealer_stock_number": own.get("offernumber"),
        "publication_reference": _reference("pkw_de", url, identity),
        "price_currency": "EUR",
        "price_net_eur": _amount(price.get("netto_price")),
        "price_monthly_eur": _amount(price.get("monthly_cost")),
        "country_evidence": {"path": "location.country", "value": country, "location": location},
    }
    if country:
        raw["market_country"] = "DE" if country in {"DE", "DEU", "D", "DEUTSCHLAND"} else country
    cash = _amount(price.get("customer"))
    kind = "advertised_price"
    if cash is None and raw["price_monthly_eur"]:
        cash, kind = raw["price_monthly_eur"], "monthly_price"
    elif cash is None and raw["price_net_eur"] is not None:
        cash, kind = raw["price_net_eur"], "net_price"
    description = own.get("description")
    if isinstance(description, str):
        description = BeautifulSoup(description, "html.parser").get_text(" ", strip=True)
    return Listing(
        "pkw_de",
        identity,
        url,
        str(own.get("name") or ""),
        price=cash,
        price_type=kind,
        mileage=_integer(own.get("mileage")),
        year=_year(own.get("initial_registration")),
        transmission=gearbox(_dict(own.get("geartype")).get("name")),
        fuel=fuel(_dict(own.get("fueltype")).get("name")),
        vin=normalized_vin(own.get("vin")),
        seller=_dict(own.get("owner")).get("name"),
        city=location.get("city"),
        description=description,
        images=[
            x.get("full") for x in own.get("images", []) if isinstance(x, dict) and x.get("full")
        ],
        raw=raw,
    )


def _parse_pkw(body, url):
    doc = None
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        pass
    if isinstance(doc, dict) and "results" in doc:
        own_rows = doc["results"] if isinstance(doc["results"], list) else []
        total = _dict(doc.get("total"))
        items = [x for row in own_rows if isinstance(row, dict) and (x := _pkw_item(row))]
        return _finish(
            items,
            _integer(total.get("count")),
            _integer(total.get("pages")),
            _integer(doc.get("page")),
            {"brand_model": "9_2326"}
            if parse_qs(urlsplit(url).query).get("brand_model") == ["9_2326"]
            else {},
        )
    if isinstance(doc, dict) and "id" in doc:
        item = _pkw_item(doc)
        return _finish(
            [item] if item and _own_id(url, "pkw_de") == item.external_id else [],
            1 if item else None,
            1 if item else None,
            1 if item else None,
        )
    soup = BeautifulSoup(body, "html.parser")
    items = []
    for card in soup.select("car-card[car]"):
        try:
            own = json.loads(card["car"])
            if isinstance(own, dict) and (item := _pkw_item(own)):
                items.append(item)
        except (ValueError, TypeError):
            continue
    # The catalog SEO count differs from the API; do not label it as the API total.
    return _finish(items, filters={"payload": "catalog_car_attribute"})


def _apollo(body):
    match = re.search(r'window\.__APOLLO_STATE__\s*=\s*("(?:[^"\\]|\\.)*")', body)
    if not match:
        return {}
    try:
        return _dict(_dict(json.loads(json.loads(match[1]))).get("ROOT_QUERY"))
    except (ValueError, TypeError):
        return {}


def _autohero_item(own, explicit_urls, detail=False):
    identity = str(own.get("adId" if detail else "id") or "").lower()
    slug = own.get("carUrlTitle")
    if (
        not re.fullmatch(_UUID, identity)
        or not isinstance(slug, str)
        or not re.fullmatch(r"[a-z0-9-]+", slug)
    ):
        return None
    derived = f"https://www.autohero.com/de/{slug}/id/{identity}/"
    url = explicit_urls.get(identity, derived)
    country = str(own.get("retailCountry" if detail else "countryCode") or "").upper()
    price = _dict(own.get("price" if detail else "offerPrice"))
    raw = {
        "own_inventory_record": own,
        "model_brand": own.get("manufacturer"),
        "model_family": own.get("model"),
        "model_variant": own.get("subType"),
        "dealer_stock_number": own.get("stockNumber"),
        "price_currency": price.get("currency"),
        "monthly_payment": own.get("monthlyPayment"),
        "country_evidence": {
            "path": "retailCountry" if detail else "countryCode",
            "value": country,
        },
        "url_evidence": "explicit_own_url"
        if identity in explicit_urls
        else "derived_from_own_id_and_carUrlTitle",
    }
    if country:
        raw["market_country"] = "DE" if country in {"DE", "D", "DEU"} else country
    if identity in explicit_urls:
        raw["publication_reference"] = _reference("autohero_de", url, identity)
    if own.get("licensePlate"):
        raw["registration_identifier"] = own["licensePlate"]
    if detail:
        own_fuel, own_gear = fuel(own.get("fuelType")), gearbox(own.get("gearType"))
    else:
        # Public enums matched the native DE detail; other codes are unknown.
        own_fuel = {1039: "petrol", 1046: "hybrid"}.get(own.get("fuelType"))
        own_gear = {1138: "manual", 1139: "automatic"}.get(own.get("gearType"))
    mileage = _dict(own.get("mileage"))
    return Listing(
        "autohero_de",
        identity,
        url,
        " ".join(
            str(own.get(k) or "") for k in ["manufacturer", "model", "subType", "subTypeExtra"]
        ).strip(),
        price=_money(price),
        mileage=_integer(mileage.get("distance")) if mileage.get("unit") == "KM" else None,
        year=_year(own.get("registration")),
        transmission=own_gear,
        fuel=own_fuel,
        vin=normalized_vin(own.get("vin")) if detail else None,
        seller="Autohero",
        city=_dict(own.get("branch")).get("city") if detail else None,
        raw=raw,
    )


def _parse_autohero(body, url):
    soup = BeautifulSoup(body, "html.parser")
    urls = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            doc = json.loads(script.get_text())
            if not isinstance(doc, dict) or doc.get("@type") not in ("Product", "Vehicle"):
                continue
            own_url = doc.get("url") or _dict(doc.get("offers")).get("url")
            identity = _own_id(own_url, "autohero_de")
            if identity and (not doc.get("productID") or str(doc["productID"]).lower() == identity):
                urls[identity] = own_url
        except (ValueError, TypeError):
            continue
    root, doc = _apollo(body), None
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        pass
    identity = _own_id(url, "autohero_de")
    if identity:
        key = f'getCarDetailsStoreAd({{"adId":"{identity}","locale":"de-DE"}})'
        own = _dict(root.get(key))
        if str(own.get("adId") or "").lower() != identity:
            return Page([])
        item = _autohero_item(own, urls, detail=True)
        return _finish([item] if item else [], 1, 1, 1)
    payload, query = None, {}
    if isinstance(doc, dict):
        if doc.get("errors"):
            return Page([])
        payload = _dict(doc.get("data")).get("searchAdV9AdsV2")
    else:
        searches = [(k, v) for k, v in root.items() if k.startswith("searchAdV9AdsV2:")]
        if len(searches) == 1:
            key, payload = searches[0]
            try:
                query = _dict(json.loads(key.split(":", 1)[1])).get("search", {})
            except ValueError:
                pass
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return Page([])
    items = [
        x for own in payload["data"] if isinstance(own, dict) and (x := _autohero_item(own, urls))
    ]
    total = _integer(payload.get("total"))
    offset = (
        _integer(query.get("offset", 0))
        if root
        else _integer(parse_qs(urlsplit(url).query).get("offset", [None])[0])
    )
    filters = {
        "payload": "autohero_own_inventory",
        "properties": query.get("properties", {}),
        "api_page_requires_request_context": offset is None,
    }
    return _finish(
        items,
        total,
        math.ceil(total / 24) if total is not None else None,
        offset // 24 + 1 if offset is not None else None,
        filters,
    )


def parse_secondary(source, body, url):
    """Page for the two verified sources; unverified sources are explicitly empty."""
    if not _public(url, source):
        return Page([], search_filters={"adapter_status": "unsupported_or_foreign_route"})
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    if not isinstance(body, str):
        return Page([])
    return _parse_pkw(body, url) if source == "pkw_de" else _parse_autohero(body, url)
