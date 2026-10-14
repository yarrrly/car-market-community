"""Strict dealer VDP evidence: canonical + JSON-LD + seller address.

Catalogs, recommendations and country inferred from domain/language are not evidence.
The caller handles network access, database operations and page traversal.
"""

import json
import re
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .identity import normalized_vin
from .parse import Listing, Page, fuel, gearbox, number


def _types(node):
    value = node.get("@type", [])
    return {t for t in (value if isinstance(value, list) else [value]) if isinstance(t, str)}


def _uri(value, base):
    if not isinstance(value, str) or not value:
        return None
    try:
        p = urlsplit(urljoin(base, value))
        if (
            p.scheme not in {"https", "http"}
            or not p.hostname
            or p.username
            or p.password
            or p.port not in {None, 80 if p.scheme == "http" else 443}
        ):
            return None
    except ValueError:
        return None
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), p.query, ""))


def _text(value):
    return BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)


def _country(value):
    if isinstance(value, dict):
        value = value.get("identifier") or value.get("name") or value.get("@id")
    return (
        "DE"
        if str(value or "").strip().upper() in {"DE", "D", "DEU", "GERMANY", "DEUTSCHLAND"}
        else None
    )


def _roots(soup):
    nodes = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for node in value if isinstance(value, list) else [value]:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            if isinstance(graph, list):
                nodes.extend(x for x in graph if isinstance(x, dict))
            else:
                nodes.append(node)
    return nodes


def _registration(value):
    value = str(value or "")
    for pattern in [
        r"(\d{4})-(\d{2})-(\d{2})",
        r"(\d{2})\.(\d{2})\.(\d{4})",
        r"(\d{1,2})[/.](\d{4})",
    ]:
        match = re.fullmatch(pattern, value)
        if not match:
            continue
        try:
            if pattern.startswith(r"(\d{4})"):
                return date(*map(int, match.groups())).isoformat()
            if len(match.groups()) == 3:
                day, month, year = map(int, match.groups())
                return date(year, month, day).isoformat()
            month, year = map(int, match.groups())
            date(year, month, 1)
            return f"{year:04d}-{month:02d}"
        except ValueError:
            return None
    return None


def _offers(item, resolve):
    offers = item.get("offers") or []
    offers = offers if isinstance(offers, list) else [offers]
    return [
        o
        for value in offers
        if isinstance(o := resolve(value), dict) and "AggregateOffer" not in _types(o)
    ]


def _money(offers):
    cash, other = [], []
    for offer in offers:
        label = " ".join(
            str(offer.get(k, "")) for k in ["name", "category", "businessFunction"]
        ).lower()
        specs = offer.get("priceSpecification") or []
        specs = specs if isinstance(specs, list) else [specs]
        monthly = bool(re.search(r"leas|leaseout|rent|monat|monthly|installment", label)) or any(
            isinstance(s, dict)
            and (
                s.get("unitCode") == "MON"
                or s.get("billingIncrement")
                or "Installment" in str(s.get("priceComponentType", ""))
            )
            for s in specs
        )
        amount = number(offer.get("price"))
        currency = offer.get("priceCurrency")
        if monthly:
            other.append("monthly_price")
            continue
        if amount is None or currency is None:
            continue
        kind = "advertised_price"
        if currency != "EUR":
            other.append("foreign_currency")
            continue
        if any(
            isinstance(s, dict) and s.get("valueAddedTaxIncluded") is False for s in specs
        ) or re.search(r"\bnetto(?:preis)?\b|\bnet\b", label):
            kind = "net_price"
        if amount < 0 or isinstance(amount, bool):
            continue
        cash.append((float(amount), kind))
    gross = {v for v in cash if v[1] == "advertised_price"}
    values = gross or set(cash)
    if len(values) == 1:
        return next(iter(values))
    if values:
        return None, "ambiguous_price"
    return None, other[0] if other else "unknown"


def parse_dealer(body, url, *legacy_url):
    """parse_dealer(body, url) -> Page; also accepts legacy (source, body, url)."""
    expected_source = None
    if legacy_url:
        if len(legacy_url) != 1:
            raise TypeError("Expected body, url or source, body, url")
        expected_source, body, url = body, url, legacy_url[0]
    page = Page([], search_filters={"country": "DE", "coverage_status": "unverified"})

    def reject(reason):
        page.search_filters["parse_status"] = reason
        return page

    requested = _uri(url, url)
    if not requested or urlsplit(requested).path in {"", "/"}:
        return reject("invalid_detail_url")
    source = "dealer:" + urlsplit(requested).hostname
    if expected_source is not None and expected_source != source:
        return reject("source_host_mismatch")
    if source == "dealer:wiveda.de":
        text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
        if isinstance(text, str) and text.lstrip().startswith("{"):
            try:
                bundle = json.loads(text)
            except ValueError:
                bundle = None
            if isinstance(bundle, dict) and bundle.get("kind") == "wiveda_native":
                from .germany_dealer_inventory import parse_wiveda_bundle

                return parse_wiveda_bundle(bundle, requested)
    if (
        urlsplit(requested).hostname == "autoland.de"
        and urlsplit(requested).path == "/bmw/2er-reihe"
    ):
        from .germany_dealer_inventory import parse_dealer_inventory

        return parse_dealer_inventory(body, requested)
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    soup = BeautifulSoup(body, "html.parser")
    head = soup.head
    canonicals = head.select('link[rel~="canonical"][href]') if head else []
    if len(canonicals) != 1 or _uri(canonicals[0]["href"], url) != requested:
        return reject("canonical_mismatch_or_missing")
    canonical = requested
    nodes = _roots(soup)
    index = {}
    for node in nodes:
        if isinstance(node.get("@id"), str):
            index.setdefault(urljoin(url, node["@id"]), []).append(node)

    def resolve(value):
        if isinstance(value, dict) and set(value) == {"@id"}:
            matches = index.get(urljoin(url, value["@id"]), [])
            return matches[0] if len(matches) == 1 else None
        return value

    candidates = []
    for node in nodes:
        if (
            _types(node) & {"WebPage", "ItemPage"}
            and _uri(node.get("url") or node.get("@id"), url) == canonical
        ):
            main = node.get("mainEntity")
            values = main if isinstance(main, list) else [main]
            candidates.extend(resolve(v) for v in values if isinstance(resolve(v), dict))
        if _types(node) & {"Vehicle", "Car", "Product"}:
            candidates.append(node)
    headings = [_text(h) for h in soup.select("h1")]
    bound = []
    for item in candidates:
        if not _types(item) & {"Vehicle", "Car", "Product"}:
            continue
        name = _text(item.get("name"))
        if not name or not any(h.casefold().startswith(name.casefold()) for h in headings):
            continue
        offers = _offers(item, resolve)
        declarations = [item.get("url"), *(o.get("url") for o in offers)]
        declarations = [v for v in declarations if v]
        if declarations and any(
            _uri(v, url) != canonical or urlsplit(v).fragment for v in declarations
        ):
            continue
        if item.get("@id") and _uri(item["@id"], url) != canonical:
            continue
        own_id = item.get("sku") or item.get("productID")
        sku_in_path = bool(
            own_id and re.search(r"(?:/|-|=)" + re.escape(str(own_id)) + r"(?:-|/|$|&)", canonical)
        )
        main_bound = any(
            _types(n) & {"WebPage", "ItemPage"}
            and _uri(n.get("url") or n.get("@id"), url) == canonical
            and resolve(n.get("mainEntity")) == item
            for n in nodes
        )
        if not declarations and not sku_in_path and not main_bound:
            continue
        bound.append(item)
    unique = {json.dumps(item, sort_keys=True): item for item in bound}
    if len(unique) != 1:
        return reject("own_vehicle_missing_or_ambiguous")
    item = next(iter(unique.values()))
    offers = _offers(item, resolve)
    sellers = [resolve(item.get("seller")), *(resolve(o.get("seller")) for o in offers)]
    sellers = [s for s in sellers if isinstance(s, dict)]
    addresses = [resolve(s.get("address")) for s in sellers]
    addresses = [a for a in addresses if isinstance(a, dict)]
    countries = [a.get("addressCountry") for a in addresses]
    if not countries or any(_country(c) != "DE" for c in countries):
        return reject("own_seller_country_not_DE")
    names = {s.get("name") for s in sellers if s.get("name")}
    if len(names) > 1:
        return reject("ambiguous_own_seller")
    listing = _own_listing(item, offers, sellers, addresses, canonical, source)
    if listing is None:
        return reject("conflicting_own_vin")
    # The native ID comes from the VDP block and is confirmed by the URI.
    if urlsplit(canonical).hostname == "autoland.de":
        native = soup.select(".product--details[data-article-id]")
        ids = {v.get("data-article-id") for v in native}
        from .germany_dealer_inventory import autoland_stock_id

        stock = autoland_stock_id(canonical)
        if ids and ids != {stock}:
            return reject("native_stock_id_mismatch")
        if ids == {stock} and stock:
            listing.raw["dealer_stock_id"] = stock
            listing.raw["own_identifiers"]["native_article_id"] = stock
            listing.raw["identity_evidence"]["native_article_id"] = stock
            listing.raw["identity_evidence"]["kind"] = "own_detail_native_id_and_uri"
    page.listings = [listing]
    page.total, page.pages, page.page = 1, 1, 1
    page.search_filters.update(
        parse_status="parsed_own_detail", country_proof_verified=True, coverage_status="detail_only"
    )
    return page


def _own_listing(item, offers, sellers, addresses, canonical, source):
    """Only already bound source data; no network access or discovery here."""
    names = {s.get("name") for s in sellers if s.get("name")}
    if len(names) > 1:
        return None
    price, price_type = _money(offers)
    registration = _registration(item.get("dateVehicleFirstRegistered"))
    year = int(registration[:4]) if registration else None
    if year is None and re.fullmatch(
        r"(?:19|20)\d{2}", str(item.get("productionDate") or item.get("vehicleModelDate") or "")
    ):
        year = int(item.get("productionDate") or item.get("vehicleModelDate"))
    mileage = item.get("mileageFromOdometer")
    mileage_value = None
    if isinstance(mileage, dict) and (
        mileage.get("unitCode") in {"KMT", "KM"}
        or str(mileage.get("unitText", "")).lower() in {"km", "kilometer", "kilometres"}
    ):
        mileage_value = number(mileage.get("value"))
        if isinstance(mileage_value, bool) or (mileage_value is not None and mileage_value < 0):
            mileage_value = None
    images = item.get("image") or []
    images = images if isinstance(images, list) else [images]
    images = [v.get("url") or v.get("contentUrl") if isinstance(v, dict) else v for v in images]
    images = list(
        dict.fromkeys(
            urljoin(canonical, v) for v in images if isinstance(v, str) and _uri(v, canonical)
        )
    )
    own_identifiers = {
        k: item[k]
        for k in ["sku", "productID", "mpn", "identifier", "vehicleIdentificationNumber", "vin"]
        if k in item
    }
    vins = {
        normalized_vin(v)
        for k, v in own_identifiers.items()
        if k in {"vin", "vehicleIdentificationNumber"} and normalized_vin(v)
    }
    if len(vins) > 1:
        return None
    vin = next(iter(vins), None)
    vin_warning = None
    # Autoland 07.10: schema identifier has 17 hex digits; an actual VIN is not proven.
    if (
        urlsplit(canonical).hostname in {"autoland.de", "www.autoland.de"}
        and vin
        and re.fullmatch(r"[0-9A-F]{17}", vin)
    ):
        vin_warning, vin = "hex_schema_identifier_unverified", None
    links = []
    references = []
    explicit = item.get("sameAs") or []
    explicit = explicit if isinstance(explicit, list) else [explicit]
    for link in explicit:
        if not _uri(link, canonical):
            continue
        absolute = urljoin(canonical, link)
        links.append(absolute)
        from .germany_mobile import mobile_ad_id

        identity = mobile_ad_id(absolute)
        if identity:
            references.append(
                {
                    "source": "mobile_de",
                    "url": absolute,
                    "external_id": identity,
                    "kind": "explicit_own_publication_link",
                }
            )
    raw = {
        "market_country": "DE",
        "country_evidence": {
            "kind": "own_offer_seller_address",
            "addressCountry": [a.get("addressCountry") for a in addresses],
            "addresses": addresses,
        },
        "identity_evidence": {
            "canonical": canonical,
            "own_schema_url": item.get("url"),
            "identifiers": own_identifiers,
        },
        "own_identifiers": own_identifiers,
        "first_registration": registration,
        "seller_address": addresses[0],
        "price_currency": "EUR" if price is not None else None,
        "price_components": offers,
        "options": item.get("additionalProperty") or [],
        "detail_complete": True,
        "vehicle": item,
        "description_source": "own_json_ld",
        "vin_warning": vin_warning,
    }
    brand = item.get("brand")
    raw["model_brand"] = brand.get("name") if isinstance(brand, dict) else brand
    model = item.get("model")
    raw["model_family"] = model.get("name") if isinstance(model, dict) else model
    raw["model_variant"] = item.get("vehicleConfiguration")
    # The joined native 218iA remains a variant requiring confirmation, not 218i.
    if source == "dealer:autoland.de" and re.match(r"^218iA(?:\s|$)", str(raw["model_variant"])):
        raw["model_variant_native"] = raw["model_variant"]
        raw["model_variant"] = "218 " + raw["model_variant"][3:]
        raw["model_variant_status"] = "native_218iA_requires_confirmation"
    if item.get("sku"):
        raw["dealer_stock_id"] = item["sku"]
    if len(references) == 1:
        raw["publication_reference"] = references[0]
    elif len(references) > 1:
        raw["publication_reference_status"] = "ambiguous"
    engine = item.get("vehicleEngine") if isinstance(item.get("vehicleEngine"), dict) else {}
    transmission = item.get("vehicleTransmission")
    transmission = (
        "manual"
        if re.search(r"\bmanuell\b", str(transmission), re.IGNORECASE)
        else gearbox(transmission)
    )
    listing = Listing(
        source,
        canonical,
        canonical,
        _text(item.get("name")),
        price=price,
        price_type=price_type,
        mileage=int(mileage_value) if mileage_value is not None else None,
        year=year,
        transmission=transmission,
        fuel=fuel(item.get("fuelType") or engine.get("fuelType")),
        kenteken=None,
        vin=vin,
        seller=next(iter(names), None),
        city=addresses[0].get("addressLocality"),
        description=_text(item.get("description")) or None,
        images=images,
        links=links,
        raw=raw,
    )
    description, listing.description = listing.description, None
    title = listing.title
    listing.title = " ".join(
        str(v) for v in [title, raw["model_brand"], raw["model_family"], raw["model_variant"]] if v
    )
    listing.qualify()
    listing.title = title
    listing.description = description
    return listing
