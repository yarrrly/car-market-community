"""12Gebrauchtwagen and Quoka: frontier and verified publication evidence.

The contract uses tests/fixtures/germany_discovery from 07.10.2026.
Result geography is unknown; areaServed, domain and language do not prove country.
"""

import hashlib
import html
import json
import math
import re
from dataclasses import asdict
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .db import now
from .germany_kleinanzeigen import GERMAN_STATES
from .identity import normalized_vin
from .parse import Listing, Page, fuel, gearbox
from .parse import number as parse_number

SOURCES = {"gebrauchtwagen_de", "quoka_de"}
GEBRAUCHTWAGEN_SEARCH = "https://www.12gebrauchtwagen.de/auto/bmw/2er-active-tourer"
QUOKA_SEARCH = (
    "https://www.quoka.de/anzeigen/auto-motorrad/automarkt/?q=bmw%20218i%20active%20tourer"
)


def _public(source, url):
    try:
        parsed = urlsplit(url)
        hosts = {
            "gebrauchtwagen_de": {"www.12gebrauchtwagen.de", "12gebrauchtwagen.de"},
            "quoka_de": {"www.quoka.de", "quoka.de"},
        }
        return (
            parsed.scheme == "https"
            and parsed.hostname in hosts.get(source, set())
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )
    except (TypeError, ValueError):
        return False


def discovery_id(source, url):
    if not _public(source, url):
        return None
    parsed = urlsplit(url)
    if source == "gebrauchtwagen_de" and parsed.path == "/c/partner":
        values = parse_qs(parsed.query).get("offer_id", [])
        return values[0] if len(values) == 1 and re.fullmatch(r"[1-9]\d*", values[0]) else None
    if source == "quoka_de":
        match = re.fullmatch(
            r"/anzeigen/auto-motorrad/automarkt/anzeige/[^/]+/([a-z0-9]{20,64})\.html", parsed.path
        )
        return match[1] if match else None
    return None


def _search_route(source, url):
    if not _public(source, url) or discovery_id(source, url):
        return False
    path = urlsplit(url).path
    if source == "quoka_de":
        return path.startswith("/anzeigen/auto-motorrad/automarkt/") and "/anzeige/" not in path
    return path.startswith(("/auto/bmw/", "/kraftstoff/benzin/bmw/")) or path == "/suchen"


def discovery_page_url(source, url, page):
    if (
        not _search_route(source, url)
        or not isinstance(page, int)
        or isinstance(page, bool)
        or not 1 <= page <= 200
    ):
        raise ValueError("invalid_discovery_page")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["pag" if source == "quoka_de" else "page"] = [str(page)]
    return urlunsplit(parsed._replace(query=urlencode(query, doseq=True), fragment=""))


def discovery_queries():
    """Ready bounded seeds: all prices/years/transmissions, without a condition filter."""
    return [
        {
            "source": source,
            "key": source + "_public_discovery",
            "url": url,
            "pagination": "discovery_de",
            "coverage": "discovery",
            "max_pages": 2,
            "detail_limit": 20,
            "request_limit": 60,
            "infer_missing": False,
        }
        for source, url in [
            ("gebrauchtwagen_de", GEBRAUCHTWAGEN_SEARCH),
            ("quoka_de", QUOKA_SEARCH),
        ]
    ]


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def _amount(value):
    if isinstance(value, bool):
        return None
    parsed = parse_number(value)
    return parsed if parsed is not None and math.isfinite(parsed) and parsed >= 0 else None


def _year(value):
    match = re.search(r"\b(19\d{2}|20\d{2})\b", str(value or ""))
    return int(match[1]) if match else None


def _price_kind(text):
    if re.search(r"leasing", text, re.IGNORECASE):
        return "lease_price"
    if re.search(r"monat|mtl", text, re.IGNORECASE):
        return "monthly_price"
    if re.search(r"netto|zzgl\.?\s*MwSt", text, re.IGNORECASE):
        return "net_price"
    return "advertised_price"


def _documents(soup):
    documents = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            # The latest private Quoka listing page contains a literal newline in description.
            # strict=False accepts control characters inside strings, not JS or malformed JSON.
            own = json.loads(script.get_text(), strict=False)
            if isinstance(own, dict):
                documents.append(own)
        except ValueError:
            continue
    return documents


def publisher_reference(url):
    """Only the native VDP of a known source; a homepage is insufficient."""
    from .germany import canonical_de_id, source_for_url

    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            return None
        source = source_for_url(url)
        if parsed.hostname == "wellergruppe.de" and re.fullmatch(
            r"/fahrzeuge/bmw/2er/[a-z0-9-]+-[1-9]\d*/", parsed.path
        ):
            clean = urlunsplit(parsed._replace(query="", fragment=""))
            return {
                "source": source,
                "url": clean,
                "external_id": canonical_de_id(source, clean),
                "kind": "explicit_own_publication_link",
            }
        if parsed.hostname in {"autoland.de", "www.autoland.de"} and re.fullmatch(
            r"/bmw/2er-reihe/[a-z-]+/[1-9]\d*/[^/]+/?", parsed.path
        ):
            clean = urlunsplit(parsed._replace(query="", fragment=""))
            return {
                "source": source,
                "url": clean,
                "external_id": canonical_de_id(source, clean),
                "kind": "explicit_own_publication_link",
            }
        if parsed.hostname == "angebote.carwow.de" and re.fullmatch(
            r"/deals/[0-9a-f]{32}", parsed.path
        ):
            clean = urlunsplit(parsed._replace(query="", fragment=""))
            return {
                "source": source,
                "url": clean,
                "external_id": canonical_de_id(source, clean),
                "kind": "explicit_own_publication_link",
            }
        if source not in {"autoscout24_de", "mobile_de", "pkw_de", "autohero_de", "bmw_de"}:
            return None
        if source == "bmw_de" and not re.fullmatch(
            r"/de-de/sl/gebrauchtwagen/details/[0-9a-f-]{36}/?", parsed.path
        ):
            return None
        identity = canonical_de_id(source, url)
        if not identity:
            return None
        return {
            "source": source,
            "url": urlunsplit(parsed._replace(fragment="")),
            "external_id": identity,
            "kind": "explicit_own_publication_link",
        }
    except (TypeError, ValueError):
        return None


def parse_discovery_publisher(source, body, url):
    """Carwow: native facts and ID; locality without country remains UNKNOWN."""
    reference = publisher_reference(url)
    if source == "dealer:angebote.carwow.de" and reference and reference["source"] == "carwow_de":
        reference = {**reference, "source": source, "external_id": reference["url"]}
    if not reference or reference["source"] != source:
        return Page([])
    if source == "dealer:wellergruppe.de":
        return _parse_weller(body, reference)
    if source != "dealer:angebote.carwow.de":
        from .germany import parse_germany_page

        return parse_germany_page(source, body, url)
    soup = BeautifulSoup(body or "", "html.parser")
    ids, titles = soup.select(".deal-id"), soup.select("h1.deal-title__main")
    native_id = urlsplit(reference["url"]).path.rsplit("/", 1)[1]
    if len(ids) != 1 or _text(ids[0]) != "Angebotsnummer: " + native_id or len(titles) != 1:
        return Page([])
    own = soup.select(".deal-details__key-information .key-details")
    if len(own) != 1:
        return Page([])
    facts = {}
    for row in own[0].select(".summary-list__item"):
        label, value = _text(row.select_one("dt")), _text(row.select_one("dd"))
        if label in facts:
            return Page([])
        facts[label] = value
    prices = soup.select("#pricing .deal-pricing-info__cash")
    cash_links = soup.select('#pricing [data-interaction-element="Cash"][data-deal-path]')
    cash_bound = len(cash_links) == 1 and cash_links[0]["data-deal-path"] == "/deals/" + native_id
    price = None
    label = ""
    if len(prices) == 1 and cash_bound:
        amounts = prices[0].select(".deal-pricing__price--main .deal-pricing__carwow-price")
        if len(amounts) == 1:
            label = _text(amounts[0])
            price = _amount(label)
    mileage_label = facts.get("Kilometerstand", "")
    mileage = _amount(mileage_label) if re.fullmatch(r"[\d., ]+\s*km", mileage_label) else None
    city = _text(soup.select_one(".dealership-info__location span")) or None
    title_city = _text(titles[0].select_one("strong.deal-title__sub-part"))
    raw = {
        "discovery_only": True,
        "discovery_country": "UNKNOWN",
        "own_details": facts,
        "own_deal_id": native_id,
        "own_location": {"dealer_city": city, "title_city": title_city},
        "country_status": "own_locality_without_explicit_country",
        "publication_reference": reference,
        "price_currency": "EUR",
        "price_label": label,
        "cash_price_bound": cash_bound,
        "cash_tax_label": _text(prices[0].select_one(".deal-pricing__tax-rate")) if prices else "",
        "leasing_label": _text(soup.select_one("#pricing .deal-pricing-info__pch")),
        "mileage_label": mileage_label,
    }
    item = Listing(
        source,
        reference["external_id"],
        reference["url"],
        _text(titles[0]),
        price=price,
        price_type=_price_kind(raw["cash_tax_label"] + " " + label),
        mileage=int(mileage) if mileage is not None else None,
        year=_year(facts.get("Jahr")),
        transmission=gearbox(facts.get("Getriebe")),
        fuel=fuel(facts.get("Kraftstoff")),
        seller=_text(soup.select_one(".dealership-info__title")) or None,
        city=city,
        description=_text(soup.select_one(".dealer-notes__content")),
        raw=raw,
    )
    return Page([_qualify(item)], search_filters={"country": "UNKNOWN", "discovery_only": True})


def _parse_weller(body, reference):
    """Observed 12G redirect: own Next vehicle ID + canonical + Car offer seller."""
    soup = BeautifulSoup(body or "", "html.parser")
    canonical = soup.select('head link[rel="canonical"][href]')
    states, headings = (
        soup.select('script#__NEXT_DATA__[type="application/json"]'),
        soup.select("h1"),
    )
    if (
        len(canonical) != 1
        or canonical[0]["href"] != reference["url"]
        or len(states) != 1
        or len(headings) != 1
    ):
        return Page([])
    try:
        state = json.loads(states[0].get_text())
        own = state["props"]["pageProps"]["vehicle"]
        identity = str(own["vehicleId"])
        manufacturer, condition = own["manufacturer"], own["condition"]
        native = own["meta"]
        if (
            not urlsplit(reference["url"]).path.endswith("-" + identity + "/")
            or state["query"].get("vehicleId") != identity
            or manufacturer.get("brand") != "BMW"
            or not _text(headings[0]).startswith(manufacturer["displayName"])
        ):
            return Page([])
    except (ValueError, KeyError, TypeError):
        return Page([])
    vehicles = [x for x in _documents(soup) if x.get("@type") == "Car"]
    if len(vehicles) != 1:
        return Page([])
    vehicle = vehicles[0]
    if (
        vehicle.get("url") != reference["url"]
        or vehicle.get("@id") != reference["url"]
        or vehicle.get("name") != manufacturer.get("name")
        or str(vehicle.get("identifier")) != str(native.get("sourceId"))
    ):
        return Page([])
    offers = vehicle.get("offers", [])
    offers = offers if isinstance(offers, list) else [offers]
    if not offers or any(
        not isinstance(x, dict) or x.get("url") != reference["url"] for x in offers
    ):
        return Page([])
    sellers = [x.get("seller") for x in offers]
    if any(not isinstance(x, dict) or not isinstance(x.get("address"), dict) for x in sellers):
        return Page([])
    addresses = [x["address"] for x in sellers]
    countries = {str(x.get("addressCountry", "")).upper() for x in addresses}
    locations = own.get("locations", [])
    if len(locations) != 1 or any(
        x.get("addressLocality") != locations[0].get("city")
        or str(x.get("postalCode")) != str(locations[0].get("zip"))
        or x.get("streetAddress") != locations[0].get("street")
        for x in addresses
    ):
        return Page([])
    vins = {
        normalized_vin(x)
        for x in (vehicle.get("vehicleIdentificationNumber"), native.get("vin"))
        if x
    }
    if len(vins) > 1:
        return Page([])
    from .germany_dealers import _own_listing

    item = _own_listing(vehicle, offers, sellers, addresses, reference["url"], reference["source"])
    if item is None:
        return Page([])
    item.external_id = reference["external_id"]
    # Retain original payloads and VAT flags; do not represent sourceId as a VIN.
    item.title = manufacturer["displayName"]
    item.year = _year(condition.get("registrationDate"))
    item.raw.update(
        own_inventory_record=own,
        dealer_stock_id=str(native.get("sourceId")),
        publication_reference=reference,
        model_family=manufacturer["displayName"],
        discovery_only=True,
    )
    cash = own.get("purchaseOptions", {}).get("price", {})
    if isinstance(cash, dict):
        if cash.get("vatInclusive") is False and cash.get("vatType") != "USt":
            item.raw["price_is_net"] = True
        native_price = _amount(cash.get("value"))
        if native_price is not None and native_price != item.price:
            item.raw["field_conflicts"] = {
                "price": {"structured": item.price, "native": native_price}
            }
            item.raw["price_warning"] = "own_price_conflict"
    item.raw.pop("market_country", None)
    if len(countries) == 1 and next(iter(countries)):
        item.raw["market_country"] = next(iter(countries))
        item.raw["discovery_only"] = item.raw["market_country"] != "DE"
    else:
        item.raw["geography_conflict"] = True
    return Page([_qualify(item)])


def quoka_native_partitions(body, url):
    """Only the two disjoint commercial links actually displayed by the native UI."""
    if not _search_route("quoka_de", url):
        return []
    soup = BeautifulSoup(body or "", "html.parser")
    root = parse_qs(urlsplit(url).query)
    root.pop("pag", None)
    root.pop("commercial", None)
    found = {}
    for node in soup.select("a[href]"):
        link = urljoin(url, node["href"])
        if not _search_route("quoka_de", link) or urlsplit(link).path != urlsplit(url).path:
            continue
        query = parse_qs(urlsplit(link).query)
        mode = query.pop("commercial", [])
        if query != root or mode not in (["false"], ["true"]):
            continue
        label = _text(node)
        counter = re.fullmatch(r"(?:Privat|Gewerblich)\s+([\d.]+)", label)
        if not counter:
            continue
        entry = {
            "url": link,
            "commercial": mode[0],
            "native_count": int(counter[1].replace(".", "")),
        }
        if mode[0] in found and found[mode[0]] != entry:
            return []
        found[mode[0]] = entry
    return [found[x] for x in ("false", "true")] if len(found) == 2 else []


def _references(description):
    own = BeautifulSoup(html.unescape(description or ""), "html.parser")
    found = {}
    for link in own.select("a[href]"):
        ref = publisher_reference(link["href"])
        if ref:
            found[(ref["source"], ref["external_id"])] = ref
    return list(found.values())


def _qualify(item):
    from .germany import qualify_german_item

    return qualify_german_item(item)


def _quoka_geography(soup, own):
    proofs = []
    location = own.get("location") or own.get("address") or {}
    if isinstance(location, dict):
        address = location.get("address", location)
        if isinstance(address, dict) and address.get("addressCountry"):
            country = address["addressCountry"]
            if isinstance(country, dict):
                country = country.get("identifier") or country.get("name")
            country = str(country or "").upper()
            proofs.append(
                {
                    "kind": "own_vehicle_address",
                    "value": country,
                    "country": "DE"
                    if country in {"DE", "D", "DEU", "DEUTSCHLAND", "GERMANY"}
                    else country,
                }
            )
    own_zip = soup.select_one(".detail-info .zip-code")
    own_geo = own_zip.parent if own_zip else None
    city = None
    if own_geo:
        own_links = [
            (x, urljoin("https://www.quoka.de", x["href"])) for x in own_geo.select("a[href]")
        ]
        for node, link in own_links:
            if (
                _public("quoka_de", link)
                and _text(node) in GERMAN_STATES
                and urlsplit(link).path.startswith("/anzeigen/auto-motorrad/automarkt/")
            ):
                prefix = urlsplit(link).path.rstrip("/") + "/"
                towns = [
                    (x, u)
                    for x, u in own_links
                    if _public("quoka_de", u)
                    and urlsplit(u).path.startswith(prefix)
                    and urlsplit(u).path.rstrip("/") != prefix.rstrip("/")
                ]
                if len(towns) == 1:
                    city = _text(towns[0][0])
                    proofs.append(
                        {
                            "kind": "own_detail_location_row",
                            "state": _text(node),
                            "state_url": link,
                            "city": city,
                            "city_url": towns[0][1],
                            "postcode": _text(own_zip),
                            "country": "DE",
                        }
                    )
    countries = {proof["country"] for proof in proofs}
    return next(iter(countries)) if len(countries) == 1 else None, city, proofs


def _quoka_item(own, url, soup=None):
    identity = discovery_id("quoka_de", url)
    if not identity:
        return None
    offer = own.get("offers", {})
    offer = offer if isinstance(offer, dict) else {}
    properties = {
        x.get("name"): x.get("value")
        for x in own.get("additionalProperty", [])
        if isinstance(x, dict)
    }
    description = own.get("description") or ""
    references = _references(description)
    raw = {
        "discovery_only": True,
        "discovery_country": "UNKNOWN",
        "own_payload": own,
        "own_details": properties,
        "price_currency": offer.get("priceCurrency"),
        "publisher_candidates": references,
        "own_card_sha256": hashlib.sha256(json.dumps(own, sort_keys=True).encode()).hexdigest(),
    }
    if len(references) == 1:
        raw["publication_reference"] = references[0]
    elif len(references) > 1:
        raw["publication_reference_status"] = "ambiguous"
    country, city, proofs = _quoka_geography(soup, own) if soup else (None, None, [])
    if country:
        raw.update(market_country=country, country_evidence=proofs, discovery_only=country != "DE")
    elif proofs:
        raw["country_evidence"] = proofs
        raw["geography_conflict"] = True
    if soup and not references:
        raw["publication_reference"] = {
            "source": "quoka_de",
            "url": url,
            "external_id": identity,
            "kind": "explicit_own_publication_link",
        }
    mileage = _amount(own.get("mileageFromOdometer"))
    item = Listing(
        "quoka_de",
        identity,
        url,
        html.unescape(own.get("name") or ""),
        price=_amount(offer.get("price")),
        mileage=int(mileage) if mileage is not None else None,
        year=_year(own.get("vehicleModelDate")),
        transmission=gearbox(properties.get("Getriebe")),
        fuel=fuel(own.get("fuelType")),
        vin=normalized_vin(
            own.get("vehicleIdentificationNumber")
            or properties.get("VIN")
            or properties.get("FIN")
            or properties.get("Chassis-Serie")
        )
        if soup
        else None,
        seller=(offer.get("seller") or {}).get("name")
        if isinstance(offer.get("seller"), dict)
        else None,
        city=city,
        description=BeautifulSoup(html.unescape(description), "html.parser").get_text(
            " ", strip=True
        ),
        links=[x["url"] for x in references],
        raw=raw,
    )
    return _qualify(item)


def _parse_quoka(soup, url):
    documents = _documents(soup)
    identity = discovery_id("quoka_de", url)
    if identity:
        vehicles = [x for x in documents if x.get("@type") == "Vehicle"]
        canonical = soup.select('link[rel="canonical"][href]')
        title = soup.select("h1")
        if (
            len(vehicles) != 1
            or len(canonical) != 1
            or len(title) != 1
            or discovery_id("quoka_de", urljoin(url, canonical[0]["href"])) != identity
            or _text(title[0]) != html.unescape(vehicles[0].get("name") or "")
            or vehicles[0].get("url")
            and discovery_id("quoka_de", urljoin(url, vehicles[0]["url"])) != identity
        ):
            return Page([])
        item = _quoka_item(vehicles[0], urljoin(url, canonical[0]["href"]), soup)
        return Page(
            [item] if item else [],
            1,
            1,
            1,
            search_filters={
                "country": item.raw.get("market_country", "UNKNOWN") if item else "UNKNOWN",
                "discovery_only": not item or item.raw.get("market_country") != "DE",
            },
        )
    if not _search_route("quoka_de", url):
        return Page([])
    lists = [x for x in documents if x.get("@type") == "ItemList"]
    counter = soup.select_one('meta[name="search-result-total-items"]')
    total = int(counter["content"]) if counter and counter.get("content", "").isdigit() else None
    pages = math.ceil(total / 20) if total is not None else None
    requested = parse_qs(urlsplit(url).query).get("pag", ["1"])
    current = soup.select_one(".pagination .current a")
    native_page = int(_text(current)) if current and _text(current).isdigit() else None
    result = Page(
        [],
        total,
        pages,
        native_page,
        search_filters={
            "country": "UNKNOWN",
            "discovery_only": True,
            "q": parse_qs(urlsplit(url).query).get("q", [None])[0],
            "commercial": parse_qs(urlsplit(url).query).get("commercial", [None])[0],
            "page_size": 20,
            "route_page": int(requested[0])
            if len(requested) == 1 and requested[0].isdigit()
            else None,
        },
    )
    if len(lists) != 1:
        return result
    seen = set()
    for own in lists[0].get("itemListElement", []):
        if not isinstance(own, dict) or own.get("@type") != "Vehicle":
            continue
        item = _quoka_item(own, urljoin("https://www.quoka.de/", own.get("url", "")))
        if item and item.external_id not in seen:
            seen.add(item.external_id)
            result.listings.append(item)
    result.links = [
        urljoin(url, x["href"])
        for x in soup.select(".pagination a[href]")
        if _search_route("quoka_de", urljoin(url, x["href"]))
    ]
    return result


def _parse_twelve(soup, url):
    if not _search_route("gebrauchtwagen_de", url):
        return Page([])
    heading = _text(soup.select_one("h1"))
    match = re.search(r"([\d.]+)\s+Angebote", heading)
    total = int(match[1].replace(".", "")) if match else None
    narrative = next(
        (
            re.search(r"Aktuell sind\s+([\d.]+)\s+gebrauchte", _text(x))
            for x in soup.select("h2")
            if _text(x).startswith("Aktuell sind")
        ),
        None,
    )
    counters = {
        "heading": total,
        "narrative": int(narrative[1].replace(".", "")) if narrative else None,
    }
    for own in _documents(soup):
        if own.get("@type") == "Product" and isinstance(own.get("offers"), dict):
            counters["aggregate_offer"] = own["offers"].get("offerCount")
    selected = soup.select_one('.pagy [aria-current="page"]')
    page = int(_text(selected)) if selected and _text(selected).isdigit() else None
    pages = math.ceil(total / 20) if total is not None else None
    result = Page(
        [],
        total,
        pages,
        page,
        search_filters={
            "country": "UNKNOWN",
            "discovery_only": True,
            "source_counters": counters,
            "counter_conflict": len({x for x in counters.values() if x is not None}) > 1,
            "page_size": 20,
            "promotion_policy": "top_cards_are_not_counter_proof",
        },
    )
    seen = set()
    for card in soup.select(".offer-card"):
        anchors = card.select('a[href*="/c/partner?"]')
        own_urls = {
            urljoin(url, x["href"])
            for x in anchors
            if discovery_id("gebrauchtwagen_de", urljoin(url, x["href"]))
        }
        attribute_ids = {
            x[k]
            for x in card.select("[data-car-favorite-offer-id],[data-car-comparison-offer-id]")
            for k in ("data-car-favorite-offer-id", "data-car-comparison-offer-id")
            if x.has_attr(k)
        }
        if len(own_urls) != 1:
            continue
        own_url = next(iter(own_urls))
        identity = discovery_id("gebrauchtwagen_de", own_url)
        if identity in seen or attribute_ids != {identity}:
            continue
        seen.add(identity)
        title = _text(card.select_one(".offer-card-title"))
        text = _text(card)
        price_label = _text(card.select_one(".offer-card-price"))
        kind_label = _text(card.select_one(".offer-type-pill")) + " " + price_label
        mileage = re.search(r"([\d.]+)\s*km\b", text)
        year = re.search(r"\bEZ\s+[^\s]+", text)
        raw = {
            "discovery_only": True,
            "discovery_country": "UNKNOWN",
            "card_text": text,
            "own_card_sha256": hashlib.sha256(str(card).encode()).hexdigest(),
            "price_label": price_label,
            "price_currency": "EUR",
            "financing_label": _text(card.select_one(".offer-card-financing")),
            "publisher_label": _text(card.select_one(".offer-card-footer-cta")),
            "publisher_redirect": own_url,
            "placement": "promoted"
            if re.search(r"\bTOP\b", _text(card.select_one(".offer-card-badges")))
            else "unmarked",
            "publication_reference": {
                "source": "gebrauchtwagen_de",
                "url": own_url,
                "external_id": identity,
                "kind": "explicit_own_publication_link",
            },
        }
        item = Listing(
            "gebrauchtwagen_de",
            identity,
            own_url,
            title,
            price=_amount(price_label),
            price_type=_price_kind(kind_label),
            mileage=int(mileage[1].replace(".", "")) if mileage else None,
            year=_year(year[0]) if year else None,
            transmission=gearbox(text),
            fuel=fuel(
                "Diesel"
                if re.search(r"\bDiesel\b", text)
                else "Benzin"
                if re.search(r"\bBenzin\b", text)
                else None
            ),
            links=[own_url],
            raw=raw,
        )
        result.listings.append(_qualify(item))
    result.links = [
        urljoin(url, x["data-gtm-event-url-value"])
        for x in soup.select(".pagy [data-gtm-event-url-value]")
        if _search_route("gebrauchtwagen_de", urljoin(url, x["data-gtm-event-url-value"]))
    ]
    return result


def parse_discovery(source, body, url):
    if source not in SOURCES or not _public(source, url):
        return Page([])
    soup = BeautifulSoup(body or "", "html.parser")
    return _parse_quoka(soup, url) if source == "quoka_de" else _parse_twelve(soup, url)


def collect_discovery(collector, run, query):
    """Retain the frontier first, then native details/DE publisher evidence, without fuzzy matching."""
    source, key = query["source"], query["key"]
    if source not in SOURCES or getattr(collector, "country", None) != "DE":
        raise ValueError("discovery_requires_german_collector")
    config = getattr(collector, "config", {})
    page_limit = query.get("max_pages", config.get("discovery_max_pages", 2))
    detail_limit = query.get("detail_limit", 20)
    request_limit = query.get("request_limit", page_limit + 2 * detail_limit)
    own_quoka = query.get("quoka_own_details", True)
    if (
        any(
            not isinstance(x, int) or isinstance(x, bool) or not 0 <= x <= 200
            for x in (page_limit, detail_limit)
        )
        or not isinstance(request_limit, int)
        or isinstance(request_limit, bool)
        or not 1 <= request_limit <= 1000
        or not isinstance(own_quoka, bool)
    ):
        raise ValueError("invalid_discovery_budget")
    started = now()
    ledger = {
        "version": 1,
        "source": source,
        "query_key": key,
        "run": run,
        "started_at": started,
        "pages": [],
        "cards": {},
        "detail_results": {},
        "errors": [],
        "country_policy": "own_detail_or_native_publisher_required",
        "infer_missing": False,
        "query_url": query["url"],
    }
    requested, attempted, expected, last_page = 0, 0, None, 1
    observations, targets, checked_publishers = set(), set(), {}
    blocked_hosts = getattr(collector, "_discovery_blocked_hosts", None)
    if blocked_hosts is None:
        blocked_hosts = set()
        collector._discovery_blocked_hosts = blocked_hosts

    def persist():
        collector.db.conn.execute(
            "INSERT OR REPLACE INTO monitor_state(key,value) VALUES (?,?)",
            (
                "germany_discovery:" + source + ":" + key,
                json.dumps(ledger, ensure_ascii=False, sort_keys=True),
            ),
        )
        collector.db.conn.commit()

    def fetch(url, capture_source=source, *, entry=None, identity=None, replay=False):
        nonlocal requested
        policy = getattr(collector, "specialized_detail_policy", None)
        if entry is not None and callable(policy):
            try:
                result = policy(run).fetch(
                    capture_source, url, identity=identity or entry["id"], replay=replay,
                    query_remaining=request_limit - requested,
                    facts={field: entry.get(field) for field in ("price", "year", "mileage", "fuel", "transmission")} if capture_source == source and not replay else None,
                )
            except BaseException as error:
                requested += getattr(error, "detail_policy_requests", 0)
                raise
            requested += result.requests
            response, fetch_id = result.response, result.fetch_id
            if response.error or response.status != 200 or not response.body:
                raise ValueError(response.error or f"http_{response.status}")
            return response, fetch_id
        if urlsplit(url).hostname in blocked_hosts:
            raise ValueError("discovery_origin_blocked")
        if requested >= request_limit:
            raise ValueError("discovery_request_limit")
        requested += 1
        response = collector.fetcher.get(url)
        fetch_id = collector.fetch_record(run, capture_source, response, retain_raw=True)
        if response.status in {403, 429}:
            blocked_hosts.update((urlsplit(url).hostname, urlsplit(response.final_url).hostname))
        if response.error or response.status != 200 or not response.body:
            raise ValueError(response.error or f"http_{response.status}")
        return response, fetch_id

    for number in range(1, page_limit + 1):
        if number > last_page:
            break
        try:
            response, fetch_id = fetch(discovery_page_url(source, query["url"], number))
            page = parse_discovery(source, response.body, response.final_url)
            if page.page != number or page.total is None or page.pages is None:
                raise ValueError("discovery_counter_or_route_unverified")
            if expected is None:
                expected, last_page = page.total, page.pages
            elif page.total != expected:
                ledger["errors"].append(f"counter_changed:{expected}:{page.total}")
            if page.search_filters.get("counter_conflict"):
                ledger["errors"].append("source_counter_conflict")
            ledger["pages"].append(
                {
                    "page": number,
                    "fetch_id": fetch_id,
                    "url": response.final_url,
                    "ids": [x.external_id for x in page.listings],
                    "search_filters": page.search_filters,
                }
            )
            for item in page.listings:
                entry = ledger["cards"].setdefault(
                    item.external_id,
                    {
                        "id": item.external_id,
                        "url": item.url,
                        "title": item.title,
                        "model_scope": item.scope,
                        "market_country": "UNKNOWN",
                        "price": item.price,
                        "price_type": item.price_type,
                        "mileage": item.mileage,
                        "year": item.year,
                        "transmission": item.transmission,
                        "fuel": item.fuel,
                        "raw": item.raw,
                        "seen_on_pages": [],
                    },
                )
                entry["seen_on_pages"].append({"page": number, "fetch_id": fetch_id})
            persist()  # The frontier exists before the first attempt to create an observation.
        except Exception as exc:  # noqa: BLE001 — a public block does not prove an empty result set
            ledger["errors"].append(f"search:{number}:{exc}")
            break
    if last_page > page_limit:
        ledger["errors"].append("discovery_page_limit")
    relevant = [x for x in ledger["cards"].values() if x["model_scope"] != "excluded"]
    for entry in relevant[:detail_limit]:
        if not callable(getattr(collector, "specialized_detail_policy", None)) and (requested >= request_limit or urlsplit(entry["url"]).hostname in blocked_hosts):
            ledger["errors"].append(
                "discovery_request_limit"
                if requested >= request_limit
                else "discovery_origin_blocked"
            )
            break
        attempted += 1
        try:
            reference = entry["raw"].get("publication_reference")
            if not own_quoka and entry["raw"].get("publication_reference_status") == "ambiguous":
                raise ValueError("ambiguous_publication_links")
            if source == "quoka_de" and (
                own_quoka or not reference or reference["source"] == source
            ):
                response, fetch_id = fetch(entry["url"], entry=entry)
                if getattr(response, "detail_policy_accepted", False):
                    own_item = response.detail_policy_item
                    observations.add((source, own_item["external_id"]))
                    if own_item["scope"] == "target":
                        targets.add((source, own_item["external_id"]))
                    ledger["detail_results"][entry["id"]] = {"status": "observed_own_detail", "cached": True, "fetch_id": fetch_id, "fetched_at": response.timestamp, "own_item": own_item}
                    persist()
                    continue
                detail = parse_discovery(source, response.body, response.final_url)
                if (
                    discovery_id(source, response.final_url) != entry["id"]
                    or len(detail.listings) != 1
                    or detail.listings[0].external_id != entry["id"]
                ):
                    raise ValueError("own_detail_identity_unverified")
                item = detail.listings[0]
                reference = item.raw.get("publication_reference")
                if own_quoka or reference and reference["source"] == source:
                    if item.raw.get("market_country") != "DE":
                        ledger["detail_results"][entry["id"]] = {
                            "own_item": asdict(item),
                            "fetch_id": fetch_id,
                        }
                        raise ValueError("own_detail_country_unverified_or_foreign")
                    item.raw["own_publication_reference"] = {
                        "source": source,
                        "external_id": item.external_id,
                        "url": item.url,
                        "kind": "explicit_own_publication_link",
                    }
                    state = "excluded_model"
                    listing_id = (
                        collector._observe(run, item, response.timestamp, fetch_id, key)
                        if item.scope != "excluded"
                        else None
                    )
                    if callable(getattr(collector, "specialized_detail_policy", None)):
                        listing_id = collector.specialized_detail_policy(
                            run
                        ).accept_observation(
                            item, response.timestamp, fetch_id, listing_id, key
                        )
                    if listing_id:
                        observations.add((source, item.external_id))
                        if item.scope == "target":
                            targets.add((source, item.external_id))
                        state = "observed_own_detail"
                    ledger["detail_results"][entry["id"]] = {
                        "status": state,
                        "fetch_id": fetch_id,
                        "country_evidence": item.raw.get("country_evidence"),
                        "own_item": asdict(item),
                    }
                    persist()
                    continue
            if source == "gebrauchtwagen_de":
                response, fetch_id = fetch(entry["url"], entry=entry, replay=True)
                reference = publisher_reference(response.final_url)
                if not reference:
                    raise ValueError("unsupported_publisher_or_no_native_own_url")
                # The redirect proves the link; a separate native GET proves the publisher
                # payload and fetch provenance without replacing the requested URL.
                response, fetch_id = fetch(reference["url"], reference["source"], entry=entry, identity=reference["external_id"])
            elif reference:
                native_key = (reference["source"], reference["external_id"])
                if native_key in checked_publishers:
                    ledger["detail_results"][entry["id"]] = {
                        **checked_publishers[native_key],
                        "reused_native_proof": True,
                    }
                    persist()
                    continue
                response, fetch_id = fetch(reference["url"], reference["source"], entry=entry, identity=reference["external_id"])
            else:
                raise ValueError("no_explicit_own_publication_link")
            from .germany import canonical_de_id, source_for_url

            native_source = source_for_url(response.final_url)
            expected_id = reference["external_id"]
            if getattr(response, "detail_policy_accepted", False):
                own_item = response.detail_policy_item
                observations.add((native_source, expected_id))
                if own_item["scope"] == "target":
                    targets.add((native_source, expected_id))
                ledger["detail_results"][entry["id"]] = {"status": "observed_native_publisher", "cached": True, "fetch_id": fetch_id, "fetched_at": response.timestamp, "publication_reference": reference, "native_source": native_source, "native_id": expected_id}
                persist()
                continue
            if (
                native_source != reference["source"]
                or canonical_de_id(native_source, response.final_url) != expected_id
            ):
                raise ValueError("publisher_redirect_identity_mismatch")
            own = parse_discovery_publisher(native_source, response.body, response.final_url)
            own_adapter = native_source in {"dealer:angebote.carwow.de", "dealer:wellergruppe.de"}
            if (
                len(own.listings) != 1
                or own.listings[0].external_id != expected_id
                or not own_adapter
                and not collector._detail_matches(
                    native_source, reference["url"], response.final_url, response.body, expected_id
                )
            ):
                raise ValueError("native_own_identity_unverified")
            item = own.listings[0]
            if item.raw.get("market_country") != "DE":
                ledger["detail_results"][entry["id"]] = {
                    "own_item": asdict(item),
                    "fetch_id": fetch_id,
                    "publication_reference": reference,
                }
                raise ValueError("native_country_unverified_or_foreign")
            state = "excluded_model"
            item.raw["discovery_origin"] = {
                "source": source,
                "external_id": entry["id"],
                "url": entry["url"],
                "publication_reference": reference,
            }
            if item.scope != "excluded" and collector._observe(
                run, item, response.timestamp, fetch_id, key
            ):
                observations.add((native_source, item.external_id))
                if item.scope == "target":
                    targets.add((native_source, item.external_id))
                state = "observed_native_publisher"
            result = {
                "status": state,
                "fetch_id": fetch_id,
                "publication_reference": reference,
                "native_source": native_source,
                "native_id": item.external_id,
                "market_country": "DE",
                "own_item": asdict(item),
            }
            checked_publishers[(native_source, item.external_id)] = result
            ledger["detail_results"][entry["id"]] = result
        except Exception as exc:  # noqa: BLE001 — unknown country/publication remains in the frontier
            from .bmw_detail_policy import DetailDeferred, deferred_record

            if isinstance(exc, DetailDeferred):
                ledger["detail_results"][entry["id"]] = deferred_record(exc, entry["url"])
            else:
                ledger["detail_results"].setdefault(entry["id"], {}).update(status="unconfirmed", error=str(exc))
            ledger["errors"].append(f"detail:{entry['id']}:{exc}")
        persist()
    if len(relevant) > detail_limit:
        ledger["errors"].append("discovery_detail_limit")
    ledger["pending_details"] = {
        entry["id"]: ledger["detail_results"].get(entry["id"], {"status": "deferred", "url": entry["url"], "reason": "query_detail_limit"})
        for entry in relevant
        if ledger["detail_results"].get(entry["id"], {}).get("status") not in {"observed_own_detail", "observed_native_publisher", "excluded_model"}
    }
    received = len(ledger["cards"])
    if expected is None or received != expected:
        ledger["errors"].append(f"source_unique_count_not_reconciled:{received}:{expected}")
    if query.get("pagination_certified") is not True:
        ledger["errors"].append("pagination_not_certified")
    status = "complete" if not ledger["errors"] and len(ledger["pages"]) == last_page else "partial"
    ledger.update(
        expected_count=expected,
        received_count=received,
        target_count=len(targets),
        observed_count=len(observations),
        detail_attempts=attempted,
        requests=requested,
        status=status,
        finished_at=now(),
    )
    persist()
    collector.db.finish_source(
        run,
        source,
        key,
        started,
        status,
        expected,
        received,
        len(targets),
        len(ledger["pages"]),
        ";".join(ledger["errors"]) or None,
        infer_missing=False,
    )
    if ledger["errors"]:
        collector.failures.append("discovery_partial:" + source + ":" + key)
    return ledger
