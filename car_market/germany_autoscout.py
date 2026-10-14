"""Public German AutoScout24 adapter; retained own responses: 2026-10-07.

Only typed current listings are read. Recommendations, JSON-LD and recursive
UUID matches are deliberately outside this adapter's evidence boundary.
"""

import html
import json
import math
import re
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .parse import Listing, Page, fuel, gearbox, number

SOURCE = "autoscout24_de"
PAGE_SIZE = 20
PAGE_CAP = 200
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_HOSTS = {"autoscout24.de", "www.autoscout24.de", "autoscout24.com", "www.autoscout24.com"}


def _public(url):
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in _HOSTS
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )
    except (ValueError, TypeError):
        return False


def _own_id(url):
    if not _public(url):
        return None
    parsed = urlsplit(url)
    smyle = re.fullmatch(r"/smyle/details/(" + _UUID + r")/?", parsed.path, re.IGNORECASE)
    if smyle and parsed.hostname in {"autoscout24.de", "www.autoscout24.de"}:
        return smyle[1].lower()
    prefix = "angebote" if parsed.hostname.endswith(".de") else "offers"
    match = re.fullmatch(
        r"/" + prefix + r"/(?:[^/]*-)?(" + _UUID + r")/?", parsed.path, re.IGNORECASE
    )
    return match[1].lower() if match else None


def autoscout_de_page_url(url, page):
    """Use AS24's page=N contract; never request the false-empty page 201."""
    if (
        not _public(url)
        or not isinstance(page, int)
        or isinstance(page, bool)
        or not 1 <= page <= PAGE_CAP
    ):
        raise ValueError("invalid_autoscout_de_page")
    parsed = urlsplit(url)
    if not (parsed.path.startswith("/lst/") or parsed.path.startswith("/_next/data/")):
        raise ValueError("invalid_autoscout_de_search_route")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query.get("cy") not in (["D"], ["DE"]):
        raise ValueError("autoscout_de_country_required")
    query["page"] = [str(page)]
    return urlunsplit(parsed._replace(query=urlencode(query, doseq=True), fragment=""))


def autoscout_de_search_urls():
    """Broad source controls, with no motor/variant/year/gear/price exclusion.

    The 2er taxonomy control includes unknown motor variants; the legacy 218
    model control complements it. Qualifying targets remains Listing.qualify.
    """
    common = "cy=D&atype=C&damaged_listing=include&ocs_listing=include"
    return [
        "https://www.autoscout24.de/lst/bmw?cat=ma13gr100098&" + common,
        "https://www.autoscout24.com/lst/bmw/218?" + common,
    ]


def _state(body):
    try:
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        if body.lstrip().startswith("{"):
            doc = json.loads(body)
            envelope = "next_json"
        else:
            soup = BeautifulSoup(body, "html.parser")
            scripts = soup.select("script#__NEXT_DATA__")
            if len(scripts) != 1:
                return None, None, [], None
            doc = json.loads(scripts[0].string or "")
            envelope = "__NEXT_DATA__"
            canonical = [node["href"] for node in soup.select('link[rel="canonical"][href]')]
        if not isinstance(doc, dict):
            return None, None, [], None
        props = doc.get("props", doc)
        state = props.get("pageProps") if isinstance(props, dict) else None
        return (
            (state if isinstance(state, dict) else None),
            envelope,
            canonical if envelope == "__NEXT_DATA__" else [],
            doc.get("buildId"),
        )
    except (ValueError, AttributeError, TypeError, UnicodeError):
        return None, None, [], None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return (
        BeautifulSoup(html.unescape(value), "html.parser").get_text(" ", strip=True)
        if isinstance(value, str)
        else ""
    )


def _label(value):
    if isinstance(value, dict):
        value = value.get("name") or value.get("formatted") or value.get("label")
    return _text(value)


def _numeric(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str) and re.search(r"(?:^|[^\w])[-−]\s*\d", value):
        return None
    if isinstance(value, str):
        value = re.sub(r"\s", " ", value)
    result = number(value)
    return result if result is not None and math.isfinite(result) else None


def _positive(value):
    result = _numeric(value)
    return result if result is not None and result > 0 else None


def _integer(value):
    result = _numeric(value)
    return (
        int(result)
        if result is not None and result >= 0 and result == int(result)
        else None
    )


def _registration_year(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:T.*)?", value)
    try:
        if match:
            return date(*map(int, match.groups())).year
        match = re.fullmatch(r"(\d{2})[./-](\d{2})[./-](\d{4})", value)
        if match:
            return date(int(match[3]), int(match[2]), int(match[1])).year
        match = re.fullmatch(r"(\d{2})[./-](\d{4})", value)
        if match and 1 <= int(match[1]) <= 12 and 1886 <= int(match[2]) <= 2100:
            return int(match[2])
    except ValueError:
        pass
    return None


def _price_currency(own):
    """Currency belongs to the chosen own quote, never the site's country."""
    chosen = _dict(_dict(own.get("prices")).get("public")) or _dict(own.get("price"))
    claims = set()
    for key in ("currency", "currencyCode", "priceCurrency"):
        value = chosen.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3}", value.strip()):
            claims.add(value.strip().upper())
    for key in ("price", "priceFormatted", "netPrice"):
        label = chosen.get(key)
        if not isinstance(label, str):
            continue
        claims.update(
            re.findall(r"\b(?:EUR|CHF|USD|CAD|AUD|NZD|GBP|PLN|SEK|NOK|DKK|CZK|HUF)\b", label.upper())
        )
        if "€" in label:
            claims.add("EUR")
        if "£" in label:
            claims.add("GBP")
        if "$" in label:
            # A dollar sign does not distinguish USD, CAD or AUD.
            if not claims.intersection({"USD", "CAD", "AUD", "NZD"}):
                claims.add("UNKNOWN_DOLLAR")
    if len(claims) == 1:
        return next(iter(claims))
    return "CONFLICT" if claims else "UNKNOWN"


def _price(own):
    display = _dict(own.get("price"))
    public = _dict(_dict(own.get("prices")).get("public"))
    chosen = public or display
    label = " ".join(
        str(chosen.get(k) or "")
        for k in ("price", "priceFormatted", "vatLabel", "type", "priceType")
    ).lower()
    path = "prices.public" if public else "price"
    if chosen.get("onRequestOnly") is True:
        return None, "price_on_request", path + ".onRequestOnly"
    if re.search(r"op aanvraag|auf anfrage|price on request", label):
        field = ".priceFormatted" if chosen.get("priceFormatted") else ".price"
        return None, "price_on_request", path + field
    amount = _positive(chosen.get("priceRaw"))
    if chosen.get("priceRaw") is None:
        amount = _positive(chosen.get("priceFormatted") or chosen.get("price"))
    if re.search(
        r"monat|monthly|maand|/\s*(?:mo|month|mnd)\b|per month|p\s*/\s*m\b|mtl|leasing|lease", label
    ):
        kind = "lease_price" if "leas" in label else "monthly_payment"
    elif amount is None and _positive(chosen.get("netPriceRaw")) is not None:
        amount, kind, path = _positive(chosen["netPriceRaw"]), "net_price", path + ".netPriceRaw"
    elif re.search(r"exkl|zzgl.*(?:mwst|vat)|netto|\bnet\b|excl", label):
        kind = "net_price"
    elif "export" in label:
        kind = "export_price"
    elif re.search(r"anzahlung|aanbetaling|down payment", label):
        kind = "conditional_price"
    elif display.get("isConditionalPrice") is True:
        kind = "conditional_price"
    else:
        kind = "advertised_price"
    currency = _price_currency(own)
    if currency == "CONFLICT":
        return None, "ambiguous_price", path
    if currency not in {"EUR", "UNKNOWN"}:
        return None, "non_eur", path
    if kind == "advertised_price":
        if amount is None:
            kind = "unknown"
        elif currency == "UNKNOWN":
            amount, kind = None, "unknown_currency"
    return amount, kind, path


def _listing(own, own_url, envelope, detail=False):
    vehicle = _dict(own.get("vehicle"))
    identity = str(own.get("id") or "").lower()
    if not vehicle or not re.fullmatch(_UUID, identity) or _own_id(own_url) != identity:
        return None
    location = _dict(own.get("location"))
    if location.get("countryCode") != "DE":
        return None
    seller = _dict(own.get("seller"))
    make = vehicle.get("make")
    make = _dict(make).get("name") if isinstance(make, dict) else make
    title = " ".join(
        str(x)
        for x in [
            make,
            vehicle.get("motorTypeName") or vehicle.get("model"),
            vehicle.get("variant"),
            vehicle.get("modelVersionInput"),
        ]
        if x
    )
    description = _text(own.get("description")) or None
    details = (
        {
            x.get("iconName"): x.get("data")
            for x in own.get("vehicleDetails", [])
            if isinstance(x, dict) and isinstance(x.get("iconName"), str)
        }
        if isinstance(own.get("vehicleDetails", []), list)
        else {}
    )
    registered = (
        vehicle.get("firstRegistrationDateRaw")
        or vehicle.get("firstRegistrationDate")
        or details.get("calendar")
    )
    mileage = vehicle.get("mileageInKmRaw")
    mileage = mileage if mileage is not None else vehicle.get("mileageInKm")
    fuel_value = _dict(vehicle.get("fuelCategory")).get("formatted") or vehicle.get("fuel")
    price, price_type, price_path = _price(own)
    vin = vehicle.get("vin") or vehicle.get("vehicleIdentificationNumber")
    vin = vin.strip().upper() if isinstance(vin, str) else ""
    vin = vin if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) and len(set(vin)) > 1 else None
    reference = {
        "source": SOURCE,
        "url": own_url,
        "external_id": identity,
        "kind": "explicit_own_publication_link",
    }
    raw = dict(own)
    raw.update({"market_country": "DE", "country_code": "DE", "publication_reference": reference})
    raw.pop("price_currency", None)
    raw["price_warning"] = None if price_type == "advertised_price" else price_type
    raw["own_metadata_fields"] = {
        "first_registration": registered,
        "fuel": fuel_value,
        "transmission": vehicle.get("transmissionType") or vehicle.get("transmission"),
        "power_kw": vehicle.get("rawPowerInKw"),
        "engine_displacement_cc": vehicle.get("rawDisplacementInCCM"),
        "equipment": vehicle.get("equipment"),
        "service_history": vehicle.get("hasFullServiceHistory"),
        "warranty": own.get("warranty"),
        "warranty_exists": own.get("warrantyExists"),
        "offer_type": vehicle.get("offerType"),
        "damage_conditions": vehicle.get("damageConditions"),
        "had_accident": vehicle.get("hadAccident"),
        "dealer_stock_reference": _dict(own.get("identifier")).get("offerReference"),
    }
    if detail:
        raw["detail_complete"] = bool(description)
    raw["origin"] = {
        "country": {"value": "DE", "path": "location.countryCode"},
        "own_stock": {
            "kind": "publisher_listing",
            "external_id": identity,
            "url": own_url,
            "envelope": envelope,
            "path": "pageProps.listingDetails" if detail else "pageProps.listings",
        },
        "ad_facts": {
            "kind": "seller_claims",
            "price_path": price_path,
            "first_registration": registered,
            "equipment": vehicle.get("equipment"),
            "service": {
                k: vehicle[k]
                for k in (
                    "hasFullServiceHistory",
                    "lastTechnicalServiceDate",
                    "lastBeltServiceDate",
                )
                if k in vehicle
            },
            "warranty": own.get("warranty"),
            "warrantyExists": own.get("warrantyExists"),
        },
    }
    images = own.get("images")
    item = Listing(
        SOURCE,
        identity,
        own_url,
        title,
        price=price,
        price_type=price_type,
        mileage=_integer(mileage),
        year=_registration_year(registered),
        transmission=gearbox(vehicle.get("transmissionType") or vehicle.get("transmission")),
        fuel=fuel(fuel_value),
        kenteken=None,
        vin=vin,
        seller=seller.get("companyName") or seller.get("contactName"),
        city=location.get("city"),
        description=description,
        images=images if isinstance(images, list) else [],
        raw=raw,
    ).qualify()
    raw["price_currency"] = _price_currency(own)
    if str(make or "").strip().upper() != "BMW":
        item.scope = "excluded"
    return item


def parse_autoscout_de(body, url) -> Page:
    """Read DE own stock on .de or .com; fail closed on route/country mismatch."""
    result = Page([])
    if not _public(url):
        return result
    state, envelope, canonicals, build_id = _state(body)
    if state is None:
        return result
    parsed = urlsplit(url)
    identity = _own_id(url)
    if identity:
        if parsed.path.startswith("/smyle/details/"):
            proper = _dict(state.get("properData"))
            own = _dict(proper.get("carDetails"))
            ocs = _dict(own.get("ocsInfo"))
            location = _dict(ocs.get("location"))
            if (
                state.get("listingAvailable") is not True
                or proper.get("guid") != identity
                or own.get("id") != identity
                or ocs.get("status") != "ACTIVE"
                or location.get("countryCode") != "DE"
                or any(_own_id(urljoin(url, link)) != identity for link in canonicals)
            ):
                return result
            vehicle = dict(_dict(own.get("vehicle")))
            vehicle.update(
                make=own.get("make"),
                model=own.get("model"),
                modelVersionInput=own.get("modelVersion"),
                vin=own.get("vin"),
            )
            vehicle["fuel"] = _dict(vehicle.get("primaryFuel")).get("formatted")
            images = [own["imageUrl"]] if own.get("imageUrl") else []
            for group in _dict(_dict(ocs.get("media")).get("images")).values():
                if not isinstance(group, list):
                    continue
                for photo in group:
                    formats = _dict(_dict(_dict(photo).get("formats")).get("jpg"))
                    candidate = next(
                        (
                            formats[key]
                            for key in ("size2560x1920", "size1280x960", "size800x600")
                            if isinstance(formats.get(key), str)
                        ),
                        None,
                    )
                    if (
                        candidate
                        and urlsplit(candidate).scheme == "https"
                        and urlsplit(candidate).hostname == "prod.pictures.autoscout24.net"
                    ):
                        images.append(candidate)
            transformed = {
                "id": identity,
                "vehicle": vehicle,
                "location": location,
                "description": None,
                "price": {
                    "priceRaw": _dict(_dict(own.get("price")).get("value")).get("raw"),
                    "priceFormatted": _dict(_dict(own.get("price")).get("value")).get("formatted"),
                },
                "seller": ocs.get("seller"),
                "identifier": own.get("identifier"),
                "images": list(dict.fromkeys(images)),
            }
            item = _listing(transformed, url, "properData.carDetails", detail=True)
            if item:
                item.raw["own_smyle_record"] = own
                item.raw["country_evidence"] = {
                    "path": "properData.carDetails.ocsInfo.location.countryCode",
                    "value": "DE",
                }
                item.raw["origin"]["country"]["path"] = (
                    "properData.carDetails.ocsInfo.location.countryCode"
                )
                item.raw["own_price_path"] = "properData.carDetails.price.value.raw"
                item.raw["origin"]["ad_facts"]["price_path"] = item.raw["own_price_path"]
                item.raw["origin"]["own_stock"]["path"] = "pageProps.properData.carDetails"
                item.raw["detail_complete"] = False
                result.listings = [item]
                result.total = result.pages = result.page = 1
                result.search_filters = {"country": "DE", "cy": "D"}
            return result
        own = state.get("listingDetails")
        if (
            not isinstance(own, dict)
            or own.get("status") != "Active"
            or str(own.get("id", "")).lower() != identity
        ):
            return result
        reference = own.get("webPage")
        if (
            not isinstance(reference, str)
            or _own_id(reference) != identity
            or any(_own_id(urljoin(url, link)) != identity for link in canonicals)
        ):
            return result
        item = _listing(own, reference, envelope, detail=True)
        if item:
            result.listings = [item]
            result.total = result.pages = result.page = 1
            result.search_filters = {"country": "DE", "cy": "D"}
        return result
    if not (
        parsed.path.startswith("/lst/")
        or re.fullmatch(r"/_next/data/[^/]+/lst/[^?]+\.json", parsed.path)
    ):
        return result
    query = _dict(state.get("pageQuery"))
    request = parse_qs(parsed.query, keep_blank_values=True)
    if query.get("cy") not in ("D", "DE") or request.get("cy") not in (["D"], ["DE"]):
        return result
    listings = state.get("listings")
    if not isinstance(listings, list):
        return result
    result.total = _integer(state.get("numberOfResults"))
    result.pages = _integer(state.get("numberOfPages"))
    result.page = _integer(query.get("page", "1"))
    if (
        result.page is None
        or not 1 <= result.page <= PAGE_CAP
        or request.get("page", ["1"]) != [str(result.page)]
    ):
        return Page([])
    build_route = re.fullmatch(r"/_next/data/([^/]+)/lst/[^?]+\.json", parsed.path)
    if build_route:
        build_id = build_route[1]
    result.search_filters = {
        k: query[k]
        for k in (
            "cat",
            "mcat",
            "mmmv",
            "atype",
            "pricefrom",
            "priceto",
            "fregfrom",
            "fregto",
            "damaged_listing",
            "ocs_listing",
            "pricetype",
            "cy",
        )
        if k in query
    }
    result.search_filters.update(
        country="DE", cy="D", build_id=build_id, page_size=PAGE_SIZE, page_cap=PAGE_CAP
    )
    seen = set()
    for own in listings:
        if not isinstance(own, dict) or not isinstance(own.get("url"), str):
            continue
        item = _listing(own, urljoin(url, own["url"]), envelope)
        if item and item.external_id not in seen:
            seen.add(item.external_id)
            result.listings.append(item)
    result.links = [item.url for item in result.listings]
    return result
