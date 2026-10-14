"""Public Autohero NL records; query context never supplies vehicle facts."""

import copy
import json
import math
import re
from urllib.parse import parse_qs, urlsplit

from .model_profiles import qualify_mercedes
from .parse import Listing, Page, fuel, gearbox, plate

AUTOHERO_API = "https://www.autohero.com/v1/retail-customer-gateway/graphql/searchAdV9AdsV2"
_UUID = r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"


def _dict(value):
    return value if isinstance(value, dict) else {}


def _public(url):
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.netloc == "www.autohero.com"
        and not parts.fragment
        and (parts.path.startswith("/nl/") or parts.path == urlsplit(AUTOHERO_API).path)
    )


def _identity(url):
    if not _public(url):
        return None
    match = re.fullmatch(r"/nl/[a-z0-9-]+/id/(" + _UUID + r")/?", urlsplit(url).path)
    return match[1] if match else None


def _terms(node):
    if not isinstance(node, dict):
        return
    if node.get("field"):
        yield node
    elif node.get("op") == "and" and isinstance(node.get("value"), list):
        for child in node["value"]:
            yield from _terms(child)


def autohero_nl_page_request(url, page, request_body):
    """Clone an observed NL Mercedes/C-Klasse body, changing only pagination.

    Returns the same method/url/body contract as secondary_page_request. The
    caller must pass body explicitly to Fetcher; its default is all NL stock.
    """
    if not _public(url) or type(page) is not int or not 1 <= page <= 200:
        raise ValueError("invalid_autohero_nl_page")
    if not isinstance(request_body, dict):
        raise TypeError("observed_autohero_nl_request_body_required")
    body = copy.deepcopy(request_body)
    search = _dict(_dict(body.get("variables")).get("search"))
    terms = list(_terms(search.get("filter")))
    countries = [t for t in terms if t["field"] == "countryCode"]
    makes = [t for t in terms if t["field"] == "make"]
    models = [t for t in terms if t["field"] == "model"]
    if (
        body.get("operationName") != "searchAdV9AdsV2"
        or countries != [{"field": "countryCode", "op": "eq", "value": "NL"}]
        or makes != [{"field": "make", "op": "eq", "value": "570"}]
        or models != [{"field": "model", "op": "eq", "value": ["c-klasse"]}]
        or search.get("limit") != 24
    ):
        raise ValueError("own_verified_autohero_nl_mercedes_c_klasse_filter_required")
    search["offset"] = (page - 1) * 24
    return {
        "method": "POST",
        "url": AUTOHERO_API + f"?pageNumber={page}",
        "body": body,
        "page": page,
    }


def _apollo(body):
    match = re.search(r'window\.__APOLLO_STATE__\s*=\s*("(?:[^"\\]|\\.)*")', body)
    if not match:
        return {}
    try:
        return _dict(_dict(json.loads(json.loads(match[1]))).get("ROOT_QUERY"))
    except (ValueError, TypeError):
        return {}


def _money(value):
    value = _dict(value)
    amount, scale = value.get("amountMinorUnits"), value.get("conversionMajor")
    if (
        value.get("currency") != "EUR"
        or type(amount) not in (int, float)
        or type(scale) not in (int, float)
        or not math.isfinite(amount)
        or not math.isfinite(scale)
        or amount <= 0
        or scale <= 0
    ):
        return None
    return amount / scale


def _registration(own):
    value = own.get("registration")
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T\d{6}\.\d{3}Z", value)
    return "-".join(match.groups()) if match else value


def _availability(own):
    states = {
        str(own.get(key) or "").strip().lower()
        for key in ("state", "retailAdState", "availabilityStatus", "availability_status")
    }
    sold = bool(states & {"sold", "seller_marked_sold"}) or any(
        own.get(key) is True for key in ("sold", "isSold")
    )
    reserved = bool(states & {"reserved", "seller_reserved"}) or any(
        own.get(key) is True for key in ("reserved", "isReserved")
    )
    historical = (
        sold
        or reserved
        or own.get("published") is False
        or bool(
            states
            & {"deleted", "unpublished", "historical", "historical_only", "archived", "inactive"}
        )
        or any(own.get(key) is True for key in ("historical", "historicalOnly", "historical_only"))
    )
    result = {"sold": sold, "reserved": reserved}
    if sold:
        result["availability_status"] = "sold"
    elif reserved:
        result["availability_status"] = "reserved"
    elif historical:
        result["availability_status"] = "historical_only"
    if historical:
        result.update(historical_only=True, own_availability_exclusion=True)
    return result


def _description(own):
    """Only native own text and equipment descriptions; no portal boilerplate."""
    parts = []
    for key in ("description", "summary", "accidentDamageDescriptions"):
        value = own.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            parts.extend(v.strip() for v in value if isinstance(v, str) and v.strip())
    for group in own.get("features") or []:
        if not isinstance(group, dict):
            continue
        values = [
            item["description"].strip()
            for item in group.get("items") or []
            if isinstance(item, dict)
            and isinstance(item.get("description"), str)
            and item["description"].strip()
        ]
        if values:
            category = group.get("translatedCategoryName") or group.get("categoryName")
            parts.append((str(category) + ": " if category else "") + "; ".join(values))
    return "\n".join(dict.fromkeys(parts)) or None


def _images(own):
    """Keep native image URLs verbatim, including disclosed {size} templates."""
    urls = []
    if isinstance(own.get("ahMainImageUrl"), str):
        urls.append(own["ahMainImageUrl"])
    for records in _dict(own.get("carDetailsImageComposites")).values():
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, dict):
                value = record.get("ahUrl") or record.get("fullUrl")
                if isinstance(value, str):
                    urls.append(value)
    return list(dict.fromkeys(value for value in urls if urlsplit(value).scheme == "https"))


def _item(own, detail_url=None):
    detail = detail_url is not None
    identity = str(own.get("adId" if detail else "id") or "").lower()
    slug = own.get("carUrlTitle")
    if not re.fullmatch(_UUID, identity) or not isinstance(slug, str):
        return None
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        return None
    url = f"https://www.autohero.com/nl/{slug}/id/{identity}/"
    if detail and (
        _identity(detail_url) != identity
        or urlsplit(detail_url).path.rstrip("/") != urlsplit(url).path.rstrip("/")
    ):
        return None
    native_country = own.get("retailCountry" if detail else "countryCode")
    registration = _registration(own)
    vin = own.get("vin")
    vin = (
        vin.upper()
        if isinstance(vin, str) and re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin.upper())
        else None
    )
    branch = _dict(own.get("branch" if detail else "esBranch"))
    money = own.get("price" if detail else "offerPrice")
    raw = {
        "own_inventory_record": copy.deepcopy(own),
        "model_brand": own.get("manufacturer"),
        "model_family": own.get("model"),
        "model_variant": own.get("subType"),
        "body_type": own.get("bodyType"),
        "generation": own.get("modelGeneration") or own.get("generation"),
        "own_first_registration": registration,
        "built_year": own.get("builtYear"),
        "model_year": own.get("modelYear"),
        "production_date": own.get("productionDate"),
        "country_code": native_country,
        "own_country_disclosure": {
            "path": "retailCountry" if detail else "countryCode",
            "value": native_country,
        },
        "own_dealer_address": copy.deepcopy(branch),
        "own_full_vin": vin,
        "dealer_stock_number": own.get("stockNumber"),
        "own_doors": own.get("doorCount"),
        "own_price": copy.deepcopy(money),
        "monthly_payment": copy.deepcopy(own.get("monthlyPayment")),
        "vat_type": own.get("vatType"),
        "financed_price": copy.deepcopy(own.get("financedPrice")),
        "own_description_fields": {
            key: copy.deepcopy(own[key])
            for key in ("description", "summary", "accidentDamageDescriptions", "features")
            if key in own
        },
        "own_gallery": copy.deepcopy(own.get("carDetailsImageComposites")),
        "image_urls_are_native_templates": any("{size}" in value for value in _images(own)),
        "public_callbacks": {
            k: copy.deepcopy(v) for k, v in own.items() if "callback" in k.lower()
        },
        "availability": {
            k: own[k]
            for k in (
                "published",
                "state",
                "retailAdState",
                "isComingSoon",
                "prospective",
                "inShowroom",
                "availableInShowroom",
                "visitBookingAvailable",
                "availabilityStatus",
                "availability_status",
                "sold",
                "isSold",
                "reserved",
                "isReserved",
                "historical",
                "historicalOnly",
                "historical_only",
            )
            if k in own
        },
        "publication_reference": {"source": "autohero", "external_id": identity, "url": url},
        "url_evidence": "bound_own_detail_adId_carUrlTitle" if detail else "own_id_and_carUrlTitle",
        "infer_missing": False,
    }
    raw.update(_availability(own))
    title = " ".join(
        str(own.get(k) or "") for k in ("manufacturer", "model", "subType", "subTypeExtra")
    ).strip()
    mileage = _dict(own.get("mileage"))
    year_match = re.match(r"^(\d{4})(?:-|$)", registration or "")
    item = Listing(
        "autohero",
        identity,
        url,
        title,
        price=_money(money),
        price_type="net_price"
        if own.get("isNetPrice") is True or own.get("priceType") == "NET"
        else "advertised_price",
        mileage=mileage.get("distance") if mileage.get("unit") == "KM" else None,
        year=int(year_match[1]) if year_match else None,
        fuel=fuel(own.get("fuelType"))
        if detail
        else {1039: "petrol", 1044: "electric", 1046: "hybrid"}.get(own.get("fuelType")),
        transmission=gearbox(own.get("gearType"))
        if detail
        else {1138: "manual", 1139: "automatic"}.get(own.get("gearType")),
        vin=vin,
        kenteken=plate(own.get("licensePlate")),
        seller="Autohero",
        city=branch.get("city"),
        description=_description(own),
        images=_images(own),
        raw=raw,
    )
    qualify_mercedes(item, "NL")
    if raw.get("own_availability_exclusion"):
        item.scope = "excluded"
    return item


def parse_mercedes_autohero_nl(body, url):
    """Parse only own public inventory or an identity-bound NL detail record."""
    if not _public(url):
        return Page([])
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        doc = None
    if isinstance(doc, dict) and doc.get("errors"):
        return Page([])
    root = _apollo(body) if doc is None else _dict(_dict(doc).get("ROOT_QUERY"))
    detail_id = _identity(url)
    if detail_id:
        key = f'getCarDetailsStoreAd({{"adId":"{detail_id}","locale":"nl-NL"}})'
        own = _dict(root.get(key)) or _dict(
            _dict(_dict(doc).get("data")).get("getCarDetailsStoreAd")
        )
        item = _item(own, url)
        return Page(
            [item] if item else [],
            total=1 if item else None,
            pages=1 if item else None,
            page=1 if item else None,
        )
    query = {}
    if isinstance(doc, dict) and "data" in doc:
        payload = _dict(doc["data"]).get("searchAdV9AdsV2")
    else:
        searches = [(k, v) for k, v in root.items() if k.startswith("searchAdV9AdsV2:")]
        if len(searches) != 1:
            return Page([])
        key, payload = searches[0]
        try:
            query = _dict(json.loads(key.split(":", 1)[1])).get("search", {})
        except ValueError:
            return Page([])
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return Page([])
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return Page([])
    items = [item for own in payload["data"] if isinstance(own, dict) and (item := _item(own))]
    total = payload.get("total")
    total = total if type(total) is int and total >= 0 else None
    page_number = parse_qs(urlsplit(url).query).get("pageNumber", [None])[0]
    page_number = (
        int(page_number)
        if isinstance(page_number, str) and page_number.isdigit() and int(page_number) > 0
        else None
    )
    return Page(
        items,
        total=total,
        pages=math.ceil(total / 24) if total is not None else None,
        page=page_number,
        search_filters={
            "payload": "own_autohero_nl_inventory",
            "native_query": query,
            "api_page_requires_request_context": page_number is None,
            "pagination_page_size": 24,
            "completeness": "observed_query_only",
            "scope_partial": True,
            "scope_limitations": [
                "recent_publication_horizon",
                "eligible_date_filter",
                "prospective_stock_included",
            ],
        },
    )
