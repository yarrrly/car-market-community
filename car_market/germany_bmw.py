"""National BMW DE: native records from the public browser POST on 07.10.2026.

HTTP replay returned 403; the next browser page has not yet been accepted.
Requests use the retained native UI request, without authentication or stealth.
"""

import copy
import html
import json
import math
import re
from contextlib import contextmanager
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .identity import normalized_vin
from .parse import Listing, Page, fuel, gearbox

SOURCE = "bmw_de"
BMW_SEED_URL = "https://www.bmw.de/de-de/sl/gebrauchtwagen"
BMW_API_HOST = "vehicle-selection.prod.stolo.eu-central-1.aws.bmw.cloud"
BMW_API_PATH = "/vehiclesearch/search/de-de/gebrauchtwagen"
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"


def _dict(value):
    return value if isinstance(value, dict) else {}


def bmw_publication_id(uri):
    """UUID only from the published detail URI; stock documentId is not a VIN."""
    try:
        parsed = urlsplit(uri)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.bmw.de"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
        ):
            return None
        match = re.fullmatch(
            r"/de-de/sl/gebrauchtwagen/details/(" + _UUID + r")/?", parsed.path, re.IGNORECASE
        )
        return match[1].lower() if match else None
    except (ValueError, TypeError):
        return None


def bmw_2er_page_request(url, page, request_body, facet_body, active_only=False):
    """Model codes come from the native facet rather than guesses based on generation."""
    facet = json.loads(facet_body) if isinstance(facet_body, (str, bytes)) else facet_body
    series = next(
        (
            node
            for node in _dict(facet).get("aggregateGeneric", [])
            if isinstance(node, dict) and node.get("name") == "MARKETING_SERIES"
        ),
        None,
    )
    two = next(
        (
            node
            for node in _dict(series).get("values", [])
            if isinstance(node, dict) and node.get("name") == "2"
        ),
        None,
    )
    ranges = next(
        (
            node
            for node in _dict(two).get("values", [])
            if isinstance(node, dict) and node.get("name") == "MARKETING_MODEL_RANGE"
        ),
        None,
    )
    selected, generations = [], set()
    for group in _dict(ranges).get("values", []):
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            continue
        native = next(
            (
                node
                for node in group.get("values", [])
                if isinstance(node, dict) and node.get("name") == "MODEL_RANGE"
            ),
            {},
        )
        codes = {node.get("name") for node in native.get("values", []) if isinstance(node, dict)}
        if not active_only or codes & {"F45", "U06"}:
            selected.append(group["name"])
            generations.update(codes)
    if not selected or not {"F45", "U06"} <= generations:
        raise ValueError("own_BMW_facet_must_cover_F45_and_U06")
    request = bmw_broad_page_request(url, page, request_body)
    for context in request["request_body"]["searchContext"]:
        context["model"] = {"marketingModelRange": {"value": selected}}
    filters = {"MARKETING_MODEL_RANGE": selected, "IS_INSTALLMENT": False}
    request.update(
        transport="browser_native_UI",
        model_ranges=selected,
        ui_url=BMW_SEED_URL
        + "/results?filters="
        + quote(quote(json.dumps(filters, separators=(",", ":")))),
        generation_coverage=sorted(generations),
    )
    return request


def bmw_price_partition_url(url, low=None, high=None):
    """Use the observed public PRICE filter; no request keys are copied."""
    p = urlsplit(url)
    if (
        p.scheme != "https"
        or p.hostname != "www.bmw.de"
        or p.username
        or p.password
        or p.port not in (None, 443)
        or p.path != BMW_SEED_URL.split("www.bmw.de")[1] + "/results"
        or p.fragment
    ):
        raise ValueError("invalid_BMW_UI_search")
    for bound in (low, high):
        if bound is not None and (
            isinstance(bound, bool)
            or not isinstance(bound, (int, float))
            or not math.isfinite(bound)
            or bound < 0
        ):
            raise ValueError("invalid_BMW_PRICE_bound")
    if low is not None and high is not None and low > high:
        raise ValueError("reversed_BMW_PRICE_bounds")
    q = parse_qs(p.query, keep_blank_values=True)
    if len(q.get("filters", [])) != 1:
        raise ValueError("own_BMW_UI_filters_required")
    filters = json.loads(unquote(q["filters"][0]))
    if not isinstance(filters, dict) or filters.get("IS_INSTALLMENT") is not False:
        raise ValueError("own_BMW_cash_filter_required")
    if low is None and high is None:
        filters.pop("PRICE", None)
    else:
        filters["PRICE"] = [low, high]
    q["filters"] = [quote(json.dumps(filters, separators=(",", ":")))]
    return urlunsplit(p._replace(query=urlencode(q, doseq=True)))


def validate_bmw_price_request(request_body, low, high):
    """Every native search context must echo the same EUR gross bounds."""
    if request_body.get("isMonthlyInstallmentFilterSelected") is not False:
        raise ValueError("BMW_PRICE_is_not_gross_purchase")
    contexts = request_body.get("searchContext")
    if not isinstance(contexts, list) or not contexts:
        raise ValueError("BMW_PRICE_context_missing")
    expected = {}
    if low is not None:
        expected["minValue"] = {"amount": low, "currency": "EUR"}
    if high is not None:
        expected["maxValue"] = {"amount": high, "currency": "EUR"}
    for context in contexts:
        if context.get("price", []) != ([expected] if expected else []):
            raise ValueError("BMW_PRICE_filter_not_applied")
    return True


def _ui_bindings(ui_body):
    soup = BeautifulSoup(ui_body, "html.parser")
    cards = {}
    for card in soup.select('stl-modelcard[data-component="model-card"][data-dealer-information]'):
        try:
            data = json.loads(card["data-dealer-information"])
        except (ValueError, TypeError):
            continue
        pair = (
            (data.get("documentId"), data.get("vssId")) if isinstance(data, dict) else (None, None)
        )
        if all(isinstance(value, str) for value in pair):
            cards[pair] = True
    links = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            document = json.loads(script.string or script.get_text())
        except ValueError:
            continue
        for node in _dict(document).get("@graph", []):
            if (
                not isinstance(node, dict)
                or node.get("@type") != "ItemList"
                or node.get("@id") != "https://www.bmw.de#/schema/item-list/1"
            ):
                continue
            rows = [
                row
                for part in node.get("itemListElement", [])
                for row in (part if isinstance(part, list) else [part])
            ]
            for row in rows:
                item = _dict(_dict(row).get("item"))
                uri = item.get("url")
                identity = bmw_publication_id(uri)
                if identity is not None and item.get("@type") == "Vehicle":
                    links.setdefault(identity, set()).add(uri)
    return {
        pair: next(iter(links[pair[1]])) for pair in cards if len(links.get(pair[1], set())) == 1
    }


def bundle_bmw_inventory(native_body, ui_body):
    """Native browser response and exact UI link between both IDs; no title guesses."""
    native = json.loads(native_body) if isinstance(native_body, (str, bytes)) else native_body
    bindings = _ui_bindings(ui_body)
    cards, rows = [], []
    for hit in _dict(native).get("hits", []):
        own = _dict(_dict(hit).get("vehicle"))
        pair = (own.get("documentId"), own.get("vssId"))
        uri = bindings.get(pair)
        if uri is None:
            continue
        data = {"documentId": pair[0], "vssId": pair[1]}
        cards.append(
            '<stl-modelcard data-component="model-card" data-dealer-information="'
            + html.escape(json.dumps(data), quote=True)
            + '"></stl-modelcard>'
        )
        rows.append({"@type": "ListItem", "item": {"@type": "Vehicle", "url": uri}})
    ld = {
        "@graph": [
            {
                "@id": "https://www.bmw.de#/schema/item-list/1",
                "@type": "ItemList",
                "itemListElement": rows,
            }
        ]
    }
    projection = (
        "".join(cards) + '<script type="application/ld+json">' + json.dumps(ld) + "</script>"
    )
    return json.dumps(
        {"bmw_native_inventory": native, "bmw_own_UI": projection}, ensure_ascii=False
    )


@contextmanager
def bmw_ui_session(request):
    """One clean Chrome context using the native UI request, without saved profiles."""
    from playwright.sync_api import sync_playwright

    codes = request.get("model_ranges")
    if not isinstance(codes, list) or not codes:
        raise ValueError("own_BMW_model_ranges_required")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(locale="de-DE")
        page = context.new_page()
        try:
            with (
                page.expect_response(
                    lambda response: _matches_ui_response(response, 0, codes), timeout=45000
                ) as response,
                page.expect_request_finished(
                    lambda native: _matches_ui_request(native, 0, codes), timeout=45000
                ),
            ):
                document = page.goto(
                    request["ui_url"], wait_until="domcontentloaded", timeout=45000
                )
            if document is None or document.status != 200:
                raise ValueError("BMW_UI_bootstrap_not_200")
            yield page, _native_ui_result(page, response.value)
        finally:
            context.close()
            browser.close()


def _matches_ui_response(response, offset, codes):
    return _matches_ui_request(response.request, offset, codes)


def _matches_ui_request(request, offset, codes):
    try:
        _, _, _, actual = _api_query(request.url)
        selected = request.post_data_json["searchContext"][0]["model"]["marketingModelRange"][
            "value"
        ]
        return actual == offset and set(selected) == set(codes)
    except (ValueError, TypeError, KeyError):
        return False


def _native_ui_result(page, response):
    body = response.text()
    if response.status != 200:
        return {
            "status": response.status,
            "final_url": response.url,
            "body": body,
            "error": "native_UI_blocked_no_retry",
        }
    return {
        "status": 200,
        "final_url": response.url,
        "body": bundle_bmw_inventory(body, page.content()),
        "native_body": body,
        "request_body": response.request.post_data_json,
        "transport": "browser_native_UI",
    }


def browser_bmw_ui_next(page, request, page_number):
    """Use the next native Mehr anzeigen button rather than an arbitrary jump offset."""
    _, _, _, offset = _api_query(
        bmw_page_request(request["url"], page_number, request["request_body"])["url"]
    )
    if page_number < 2:
        raise ValueError("BMW_UI_next_requires_page_at_least_two")
    reject = page.locator("epaas-consent-drawer-shell").get_by_role(
        "button", name="Verbieten", exact=True
    )
    for button in reject.all():
        if button.is_visible():
            button.click()
            break
    more = page.locator('[data-test="stolo-plp-show-more-button"]')
    # Headers may arrive while the body stalls; wait for completion with a bound.
    with (
        page.expect_response(
            lambda response: _matches_ui_response(response, offset, request["model_ranges"]),
            timeout=30000,
        ) as response,
        page.expect_request_finished(
            lambda native: _matches_ui_request(native, offset, request["model_ranges"]),
            timeout=30000,
        ),
    ):
        more.evaluate("el => el.scrollIntoView({block:'center'})")
        more.click(timeout=15000, no_wait_after=True)
    return _native_ui_result(page, response.value)


def _amount(value):
    return (
        value
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        else None
    )


def _year(value):
    match = re.fullmatch(r"(\d{4})-\d{2}-\d{2}", str(value or ""))
    return int(match[1]) if match and 1886 <= int(match[1]) <= 2100 else None


def _api_query(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != BMW_API_HOST
        or parsed.path != BMW_API_PATH
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise ValueError("invalid_BMW_DE_inventory_URI")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query.get("brand") != ["BMW"]:
        raise ValueError("BMW_brand_required")
    hashes = query.get("hash", [])
    if len(hashes) != 1 or not re.fullmatch(r"[0-9a-f]{64}", hashes[0]):
        raise ValueError("captured_BMW_UI_hash_required")
    if query.get("context") != ["results-page"]:
        raise ValueError("BMW_results_context_required")
    numbers = []
    for key in ("maxResults", "startIndex"):
        values = query.get(key, [])
        if len(values) != 1 or not values[0].isdigit():
            raise ValueError("invalid_BMW_pagination")
        numbers.append(int(values[0]))
    size, offset = numbers
    if size != 12 or offset % size:
        raise ValueError("unobserved_BMW_page_size_or_offset")
    return parsed, query, size, offset


def bmw_page_request(url, page, request_body):
    """Describe an ordinary anonymous browser POST, retaining its native body."""
    parsed, query, size, _ = _api_query(url)
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValueError("invalid_BMW_page")
    if (
        not isinstance(request_body, dict)
        or not isinstance(request_body.get("searchContext"), list)
        or not request_body["searchContext"]
    ):
        raise ValueError("captured_BMW_searchContext_required")
    if not all(isinstance(context, dict) for context in request_body["searchContext"]):
        raise ValueError("invalid_BMW_searchContext")
    query["startIndex"] = [str((page - 1) * size)]
    return {
        "url": urlunsplit(parsed._replace(query=urlencode(query, doseq=True))),
        "method": "POST",
        "request_body": copy.deepcopy(request_body),
        "headers": {"Accept": "application/json", "Content-Type": "application/json"},
        "transport": "browser_public_fetch",
        "bootstrap_url": BMW_SEED_URL,
        "coverage_status": "browser_pagination_unverified",
    }


def bmw_broad_page_request(url, page, request_body):
    """Remove model restrictions from the native UI request; retain national buNos.

    The actual next response needs verification: this broadens the request,
    without claiming an accepted complete crawl or new model codes.
    """
    request = bmw_page_request(url, page, request_body)
    if set(parse_qs(urlsplit(url).query)) - {
        "maxResults",
        "startIndex",
        "brand",
        "hash",
        "context",
    }:
        raise ValueError("unreviewed_BMW_broad_query_filter")
    for context in request["request_body"]["searchContext"]:
        if set(context) - {"model", "buNos"}:
            raise ValueError("unreviewed_BMW_broad_context_filter")
        context.pop("model", None)
    return request


def browser_bmw_request(browser_page, request):
    """Public fetch from an ordinary BMW page; return 403 without bypassing it."""
    origin = urlsplit(browser_page.url)
    if (
        origin.scheme != "https"
        or origin.hostname != "www.bmw.de"
        or origin.username is not None
        or origin.password is not None
        or origin.port not in (None, 443)
        or origin.path.rstrip("/")
        not in {"/de-de/sl/gebrauchtwagen", "/de-de/sl/gebrauchtwagen/results"}
    ):
        raise ValueError("BMW_public_bootstrap_required")
    _api_query(request["url"])
    if request.get("method") != "POST" or request.get("headers") != {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }:
        raise ValueError("invalid_anonymous_BMW_request")
    return browser_page.evaluate(
        """async request => {
            const response = await fetch(request.url, {
                method: 'POST', mode: 'cors', credentials: 'omit',
                headers: request.headers, body: JSON.stringify(request.request_body)
            });
            return {status: response.status, final_url: response.url,
                    body: await response.text()};
        }""",
        {key: request[key] for key in ("url", "headers", "request_body")},
    )


def _prices(vehicle, retailer):
    offering = _dict(vehicle.get("offering"))
    base = _dict(vehicle.get("price"))
    offers = _dict(offering.get("offerPrices"))
    selected = _dict(offers.get(retailer))
    currency = base.get("listPriceCurrency")
    monthly = []
    for offer in offering.get("sfOffers", []) if isinstance(offering.get("sfOffers"), list) else []:
        if not isinstance(offer, dict):
            continue
        for calculation in (
            offer.get("calculations", []) if isinstance(offer.get("calculations"), list) else []
        ):
            if not isinstance(calculation, dict):
                continue
            parameters = _dict(calculation.get("financialProductParameters"))
            total = _dict(parameters.get("totalInstallment"))
            amount = _amount(total.get("amount"))
            if amount is not None:
                monthly.append(
                    {
                        "amount": amount,
                        "currency": total.get("currency"),
                        "product_type": calculation.get("productCategoryType"),
                        "customer_type": offer.get("customerType"),
                        "buNo": offer.get("buNo"),
                        "parameters": parameters,
                    }
                )
    raw = {
        "price_currency": currency,
        "offer_prices": offers,
        "native_price": base,
        "monthly_payments": monthly,
        "display_pricing": vehicle.get("pricing"),
    }
    gross = _amount(selected.get("offerGrossPrice"))
    path = f"offering.offerPrices.{retailer}.offerGrossPrice"
    if gross is None:
        gross, path = _amount(base.get("grossSalesPrice")), "price.grossSalesPrice"
    net = _amount(selected.get("offerNetPrice"))
    if net is None:
        net = _amount(base.get("netSalesPrice"))
    raw["price_net_eur"] = net if currency == "EUR" else None
    if currency == "EUR" and gross is not None:
        raw["purchase_price_path"] = path
        return gross, "advertised_price", raw
    if currency == "EUR" and net is not None:
        raw["price_warning"] = "net_price"
        return net, "net_price", raw
    current_monthly = [
        value for value in monthly if value["currency"] == "EUR" and value["buNo"] == retailer
    ]
    if current_monthly:
        raw["price_warning"] = "monthly_payment"
        amounts = {value["amount"] for value in current_monthly}
        return next(iter(amounts)) if len(amounts) == 1 else None, "monthly_payment", raw
    if currency not in (None, "EUR"):
        raw["price_warning"] = "non_eur_currency"
        return None, "foreign_currency", raw
    return None, "unknown", raw


def _listing(hit):
    vehicle = _dict(hit.get("vehicle"))
    document_id = vehicle.get("documentId")
    identity = vehicle.get("vssId")
    if (
        not isinstance(document_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", document_id)
        or not isinstance(identity, str)
        or not re.fullmatch(_UUID, identity, re.IGNORECASE)
    ):
        return None
    identity = identity.lower()
    ordering = _dict(vehicle.get("ordering"))
    retail = _dict(ordering.get("retailData"))
    production = _dict(ordering.get("productionData"))
    life = _dict(vehicle.get("vehicleLifeCycle"))
    spec = _dict(_dict(vehicle.get("vehicleSpecification")).get("modelAndOption"))
    model = _dict(spec.get("model"))
    model_range = _dict(spec.get("modelRange"))
    name = model.get("modelName") if isinstance(model.get("modelName"), str) else ""
    family = (
        _dict(model_range.get("description")).get("de_DE")
        or _dict(model_range.get("description")).get("default_DE")
        or ""
    )
    family = family if isinstance(family, str) else ""
    title = " ".join(
        value for value in (spec.get("brand"), name, family) if isinstance(value, str) and value
    )
    price, price_type, raw = _prices(vehicle, retail.get("buNo"))
    proofs = {
        "vehicle.country": vehicle.get("country"),
        "vehicle.ordering.retailData.locationOutletAddress.countryCode": _dict(
            retail.get("locationOutletAddress")
        ).get("countryCode"),
    }
    supplied = [str(value).upper() for value in proofs.values() if value]
    country_ok = (
        bool(supplied)
        and all(value == "DE" for value in supplied)
        and hit.get("country") in (None, "DE")
    )
    raw.update(
        {
            "country_evidence": proofs,
            "model_brand": spec.get("brand"),
            "model_family": family,
            "model_variant": model.get("derivative"),
            "generation": model_range.get("name"),
            "marketing_model_range": spec.get("marketingModelRange"),
            "native_document_id": document_id,
            "vss_id": vehicle.get("vssId"),
            "dealer_id": retail.get("buNo"),
            "dealer_stock_id": life.get("stockNumber"),
            "vehicle_life_cycle": life,
            "usage_state": _dict(ordering.get("orderData")).get("usageState"),
            "first_registration": retail.get("initialRegistrationDate"),
            "production_date": production.get("productionDate"),
            "detail_complete": False,
            "description_may_be_truncated": True,
            "detail_url_status": "not_observed",
            "url_evidence": "inventory_record_without_publication_URI",
            "publication_url_verified": False,
        }
    )
    if country_ok:
        raw["market_country"] = "DE"
    elif supplied or hit.get("country") not in (None, "DE"):
        raw["country_conflict"] = {**proofs, "hit.country": hit.get("country")}
    mileage = _amount(_dict(life.get("mileage")).get("km"))
    images = list(_dict(_dict(vehicle.get("media")).get("usedCarImages")).values())
    images = list(
        dict.fromkeys(
            value
            for value in images
            if isinstance(value, str) and urlsplit(value).scheme in {"https", "http"}
        )
    )
    description = _dict(life.get("vehicleDamage")).get("damageDescription")
    item = Listing(
        SOURCE,
        identity,
        BMW_SEED_URL,
        title,
        price=price,
        price_type=price_type,
        mileage=int(mileage) if mileage is not None and mileage == int(mileage) else None,
        year=_year(retail.get("initialRegistrationDate"))
        or _year(production.get("productionDate")),
        fuel=fuel(spec.get("baseFuelType")),
        transmission=gearbox(spec.get("transmission")),
        vin=normalized_vin(production.get("vin17")),
        kenteken=None,
        city=_dict(retail.get("locationOutletAddress")).get("town"),
        description=description if isinstance(description, str) else None,
        images=images,
        raw=raw,
    )
    from .germany import qualify_german_item

    qualify_german_item(item)
    if not country_ok:
        if supplied or raw.get("country_conflict"):
            item.scope = "excluded"
        elif item.scope != "excluded":
            item.scope = "needs_confirmation"
    return item


def parse_bmw(body, url):
    """Parse only native hits[].vehicle; UI/aggregate data does not prove emptiness."""
    _, _, size, offset = _api_query(url)
    try:
        document = json.loads(body)
    except (ValueError, TypeError, UnicodeError):
        return Page([], search_filters={"parse_status": "not_inventory"})
    bindings = {}
    if (
        isinstance(document, dict)
        and isinstance(document.get("bmw_native_inventory"), dict)
        and isinstance(document.get("bmw_own_UI"), str)
    ):
        bindings = _ui_bindings(document["bmw_own_UI"])
        document = document["bmw_native_inventory"]
    if not isinstance(document, dict) or not isinstance(document.get("hits"), list):
        return Page([], search_filters={"parse_status": "not_inventory"})
    total = _amount(_dict(document.get("metadata")).get("totalCount"))
    total = int(total) if total is not None and total == int(total) else None
    items, seen = [], set()
    for hit in document["hits"]:
        item = _listing(hit) if isinstance(hit, dict) else None
        if item is not None and item.external_id not in seen:
            uri = bindings.get((item.raw["native_document_id"], item.raw["vss_id"]))
            if uri is not None:
                item.url = uri
                item.raw.update(
                    publication_url_verified=True,
                    detail_url_status="own_UI_link",
                    url_evidence="own_UI_card_documentId_vssId_and_primary_ItemList_url",
                    publication_reference={
                        "source": SOURCE,
                        "url": uri,
                        "external_id": item.external_id,
                        "kind": "explicit_own_publication_link",
                    },
                )
            items.append(item)
            seen.add(item.external_id)
    filters = {
        "coverage_status": "browser_pagination_unverified",
        "requested_market": "DE",
        "parse_status": "inventory" if len(items) == len(document["hits"]) else "partial_inventory",
    }
    if items and all(item.raw.get("market_country") == "DE" for item in items):
        filters.update(country="DE", country_proof="own_vehicle_country_fields")
    return Page(
        items,
        total=total,
        pages=math.ceil(total / size) if total is not None else None,
        page=offset // size + 1,
        search_filters=filters,
    )
