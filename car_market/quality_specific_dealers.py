"""Bounded own-VDP adapters for retained dealer responses of 2026-10-07.

No catalogue coverage is inferred. Unsupported or mismatched VDPs return None.
"""

import json
import re
from datetime import date
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .parse import Listing, Page, fuel, gearbox, json_documents, number, plate, walk


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def _one(nodes):
    if len(nodes) != 1:
        raise ValueError("missing or ambiguous own block")
    return nodes[0]


def _plate(*values):
    normalized = {plate(value) for value in values if plate(value)}
    if len(normalized) > 1 or any(
        str(value or "").strip() not in {"", "-"} and plate(value) is None for value in values
    ):
        raise ValueError("conflicting or malformed own registration")
    return next(iter(normalized), None)


def _city(value):
    match = re.search(r"\b\d{4}\s*[A-Z]{2}\s+([^,]+)$", value)
    return match.group(1).strip() if match else None


def _photos(values):
    return list(
        dict.fromkeys(
            v for v in values if isinstance(v, str) and urlsplit(v).scheme in {"http", "https"}
        )
    )


def _finish(item, adapter, identity, specs, description_node=None):
    description = _text(description_node) or item.description
    item.description = description
    truncated = bool(description and re.search(r"(?:\.{3,}|…)\s*$", description))
    if description_node:
        truncated = truncated or bool(
            description_node.select('[class*="clamp"], [data-truncated="true"]')
            or any("clamp" in c for c in description_node.get("class", []))
        )
    item.raw.update(
        detail_adapter=adapter,
        identity_evidence=identity,
        specs=specs,
        description_source="own_seller_dom" if description_node else "own_next_excerpt",
        description_truncated=truncated,
        detail_complete=bool(
            description_node and description and len(description.split()) >= 8 and not truncated
        ),
    )
    if item.price is None:
        item.price_type = "unknown"
    return Page([item.qualify()], total=1, pages=1, page=1)


def _flevo_id(url):
    parsed = urlsplit(url)
    if parsed.hostname not in {"www.flevomobiel.nl", "flevomobiel.nl"}:
        return None
    match = re.fullmatch(r"/(\d+)/(\d+)/?", parsed.path)
    if match:
        return match.group(1), match.group(2)
    query = parse_qs(parsed.query)
    if (
        parsed.path == "/occasions.aspx"
        and len(query.get("aid", [])) == len(query.get("did", [])) == 1
        and all(re.fullmatch(r"\d+", query[k][0]) for k in ("aid", "did"))
    ):
        return query["aid"][0], query["did"][0]
    return None


def _flevo(soup, url):
    identity = _flevo_id(url)
    if not identity:
        return None
    root = _one(soup.select('.advertisement[itemscope][itemtype="https://schema.org/Car"]'))
    contact_ids = {
        _flevo_id(n.get("value", "")) for n in root.select('input[name="ad_contact_adurl"]')
    }
    if contact_ids != {identity}:
        return None
    specs_root = _one(root.select('[data-section="specificaties"]'))
    specs = {}
    for row in specs_root.select(":scope > .data-list-item[data-section]"):
        key, value = row.get("data-section"), _text(row.select_one(".data-list-item-value"))
        if key in specs and specs[key] != value:
            raise ValueError("conflicting own specs")
        specs[key] = value
    contact_plates = []
    for control in root.select('input[name="ad_contact_advertentie"]'):
        match = re.search(r"\bKENTEKEN:\s*([A-Z0-9-]+)\s*,\s*PRIJS:", control.get("value", ""))
        if match:
            contact_plates.append(match.group(1))
    own_plate = _plate(
        specs.get("kenteken", "").removeprefix("Kenteken "),
        *(n.get("value") for n in root.select('input[name="ad_kenteken"]')),
        *contact_plates,
    )
    company = root.select_one("#company-section")
    location = (
        company.select_one(
            ".flevo-locatie1:not(.hidden) strong, .flevo-locatie2:not(.hidden) strong"
        )
        if company
        else None
    )
    item = Listing(
        "dealer",
        url,
        url,
        _text(_one(root.select('h2[itemprop="name"]'))),
        price=number(_text(specs_root.select_one(".prijs [itemprop=price]"))),
        mileage=number(specs.get("kilometerstand")),
        year=number(specs.get("bouwjaar")),
        fuel=fuel(specs.get("brandstof")),
        transmission=gearbox(specs.get("transmissie")),
        kenteken=own_plate,
        seller=_text(company.select_one("h2")) if company else None,
        city=_city(_text(location)),
        images=_photos(
            [n["src"] for n in root.select('[data-section="fotos"] #main-carousel img[src]')]
        ),
    )
    return _finish(
        item,
        "flevomobiel_own_vdp",
        {"aid": identity[0], "did": identity[1]},
        specs,
        root.select_one('[data-section="opmerkingen"] .opmerking-tekst'),
    )


def _hedin(soup, body, url):
    match = re.fullmatch(r"/occasion/p/(\d+)/[^/]+/?", urlsplit(url).path)
    if not match:
        return None
    own_id = match.group(1)
    cars = [
        node
        for doc in json_documents(body)
        for node in walk(doc)
        if node.get("@type") == "Car" and str(node.get("mpn")) == own_id
    ]
    car = _one(cars)
    offer = car.get("offers", {})
    offer_url = urlsplit(offer.get("url", ""))
    if offer_url.hostname != urlsplit(url).hostname or offer_url.path.rstrip("/") != urlsplit(
        url
    ).path.rstrip("/"):
        return None
    intro = _one(soup.select("#vehicle-intro"))
    payload = json.loads(_one(intro.select(".compare-toggle[data-vehicle]"))["data-vehicle"])
    if not car.get("sku") or payload.get("id") != car["sku"]:
        return None
    attributes = payload.get("attributes", {})
    specs = {}
    for term in soup.select("#vehicle-specs dt, #vehicle-specs-modal-specs-pane dt"):
        key, value = _text(term), _text(term.find_next_sibling("dd"))
        if key in specs and specs[key] != value:
            raise ValueError("conflicting own specs")
        specs[key] = value
    kenteken = _plate(specs.get("Kenteken"), attributes.get("kenteken", {}).get("value"))
    if number(payload.get("price")) != number(offer.get("price")):
        return None
    vin = str(car.get("vehicleIdentificationNumber") or "").upper()
    vin = vin if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) and len(set(vin)) > 1 else None
    location = soup.select_one("#vehicle-location .card-body")
    address = location.select_one("p.mb-3") if location else None
    item = Listing(
        "dealer",
        url,
        url,
        car.get("description") or payload.get("attributes", {}).get("title", ""),
        price=number(offer.get("price")),
        mileage=number(specs.get("Kilometerstand") or car.get("mileageFromOdometer")),
        year=number(specs.get("Bouwjaar") or car.get("vehicleModelDate")),
        fuel=fuel(specs.get("Brandstofsoort")),
        transmission=gearbox(specs.get("Transmissie")),
        kenteken=kenteken,
        vin=vin,
        seller=_text(location.select_one("p.mb-1")) if location else None,
        city=_city(_text(address)),
        images=_photos([n["ci-src"] for n in intro.select(".vehicle-image img[ci-src]")]),
    )
    return _finish(
        item,
        "hedin_own_vdp",
        {"mpn": own_id, "sku": car["sku"], "offer_url": offer["url"]},
        specs,
        soup.select_one("#vehicle-specs-modal-info-pane"),
    )


def _linkerbaan(soup, url):
    match = re.fullmatch(r"/voertuig/([^/]+)/?", urlsplit(url).path)
    if not match:
        return None
    doc = json.loads(_one(soup.select('script#__NEXT_DATA__[type="application/json"]')).string)
    if doc.get("page") != "/voertuig/[slug]" or doc.get("query", {}).get("slug") != match.group(1):
        return None
    own = doc.get("props", {}).get("pageProps", {}).get("vehicle", {})
    own_id = own.get("id")
    if not isinstance(own_id, str) or not own_id:
        return None
    schema_node = _one(soup.find_all("script", id="vehicle-schema-" + own_id))
    schema = json.loads(schema_node.string)
    schema_url = urlsplit(schema.get("url", ""))
    if (
        schema.get("@type") != "Vehicle"
        or schema_url.hostname not in {"linkerbaan.com", "www.linkerbaan.com"}
        or unquote(schema_url.path) != "/auto/" + own_id
    ):
        return None
    visible_plates = soup.select('[class*="LicensePlate_value"]')
    if len(visible_plates) > 1:
        return None
    kenteken = _plate(own.get("licensePlate"), _text(visible_plates[0]) if visible_plates else None)
    offer = schema.get("offers", {})
    if number(own.get("price")) != number(offer.get("price")):
        return None
    dealer = next(
        (
            json.loads(n.string)
            for n in soup.select('script[type="application/ld+json"]')
            if n.string and '"AutoDealer"' in n.string
        ),
        {},
    )
    if urlsplit(dealer.get("url", "")).hostname not in {"linkerbaan.com", "www.linkerbaan.com"}:
        dealer = {}
    item = Listing(
        "dealer",
        url,
        url,
        own.get("title", ""),
        price=number(own.get("price")),
        mileage=number(own.get("mileage")),
        year=number(own.get("yearOfBuild")),
        fuel=fuel(own.get("fueltype")),
        transmission=gearbox(own.get("transmission")),
        kenteken=kenteken,
        seller=dealer.get("name"),
        city=dealer.get("address", {}).get("addressLocality"),
        description=own.get("excerpt") or None,
        images=_photos(own.get("images", [])),
        raw={
            "description_is_excerpt": True,
            "sold": own.get("sold"),
            "reserved": own.get("reserved"),
            "schema_mileage": schema.get("mileageFromOdometer"),
            "mileage_source": "own_next_vehicle.mileage",
        },
    )
    return _finish(
        item,
        "linkerbaan_own_vdp",
        {"id": own_id, "slug": match.group(1)},
        {
            "mileage": own.get("mileage"),
            "licensePlate": own.get("licensePlate"),
            "transmission": own.get("transmission"),
            "yearOfBuild": own.get("yearOfBuild"),
        },
    )


def _stock_route(url):
    parsed = urlsplit(url)
    match = re.fullmatch(r"/occasions-kopen/(\d+)-[^/]+/?", parsed.path)
    return (parsed.hostname, parsed.path.rstrip("/"), match[1]) if match else None


def _stock_year(value):
    text = str(value or "").strip()
    if re.fullmatch(r"(?:19|20)\d{2}", text):
        return int(text)
    day_first = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", text)
    if day_first:
        text = f"{day_first[3]}-{day_first[2]}-{day_first[1]}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        year = date.fromisoformat(text).year
        return year if 1886 <= year <= 2100 else None
    except ValueError:
        return None


def _stock_dealer(soup, body, url):
    identity = _stock_route(url)
    if not identity:
        return None
    canonical = _one(soup.select('link[rel="canonical"]')).get("href", "")
    if urlsplit(canonical).scheme not in {"http", "https"} or _stock_route(canonical) != identity:
        return None
    top = _one(soup.select("main > .aanbod-top-sec"))
    tabs = _one(soup.select("main > .dtl-tab-sec"))
    heading = _one(top.select(".aanbod-car-dtls-title, .aanbod-top-left"))
    title = " ".join((_text(_one(heading.select("h3"))), _text(_one(heading.select(":scope > p")))))
    own_specs = _one(tabs.select(".tab-popup-area .tab-popup-content"))
    specs = {}
    for row in own_specs.select(":scope > ul > li"):
        value = _one(row.select(":scope > strong"))
        key = " ".join(str(n).strip() for n in row.contents if isinstance(n, str)).strip(": ")
        text = _text(value)
        if key in specs and specs[key] != text:
            raise ValueError("conflicting own specs")
        specs[key] = text
    own_title = " ".join(specs.get(k, "") for k in ("Merk", "Model", "Type"))

    def normalize(value):
        return " ".join(str(value or "").split())

    if normalize(own_title) != normalize(title):
        return None
    cars = [n for doc in json_documents(body) for n in walk(doc) if n.get("@type") == "Car"]
    car = _one(cars)
    if normalize(car.get("name")) != normalize(title):
        return None
    image = car.get("image")
    image_url = urlsplit(image) if isinstance(image, str) else None
    if (
        not image_url
        or image_url.scheme not in {"http", "https"}
        or image_url.hostname != identity[0]
        or not re.fullmatch(
            rf"/webservices/feed_images/{identity[2]}/{identity[2]}-\d+\.jpg", image_url.path
        )
    ):
        return None
    schema_url = urlsplit(car.get("url", ""))
    if schema_url.hostname != identity[0] or schema_url.scheme not in {"http", "https"}:
        return None
    if schema_url.path not in {"", "/"} and _stock_route(car["url"]) != identity:
        return None
    if schema_url.query or schema_url.fragment:
        return None
    offer = car.get("offers") or {}
    price = number(specs.get("Prijs"))
    mileage = number(specs.get("Tellerstand"))
    year = _stock_year(specs.get("Bouwjaar"))
    gear, propellant = gearbox(specs.get("Transmissie")), fuel(specs.get("Brandstof"))
    if not all(
        k in specs for k in ("Prijs", "Tellerstand", "Bouwjaar", "Transmissie", "Brandstof")
    ):
        return None
    if (
        price != number(offer.get("price"))
        or mileage != number(car.get("mileageFromOdometer"))
        or gear != gearbox(car.get("vehicleTransmission"))
        or propellant != fuel(car.get("fuelType"))
    ):
        return None
    for key in ("productionDate", "vehicleModelDate"):
        schema_year = _stock_year(car.get(key))
        if schema_year is not None and schema_year != year:
            return None
    kenteken = _plate(specs.get("Kenteken"), car.get("licensePlate"))
    vin = str(car.get("vehicleIdentificationNumber") or "").strip().upper()
    vin = vin if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) and len(set(vin)) > 1 else None
    photos = []
    for node in soup.select("main > .aanbod-top-sec img[src], main > .aanbod-car-sec img[src]"):
        candidate = urljoin(url, node["src"])
        parsed = urlsplit(candidate)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname == identity[0]
            and re.fullmatch(
                rf"/webservices/feed_images/{identity[2]}/{identity[2]}-\d+\.jpg", parsed.path
            )
        ):
            photos.append(candidate)
    if not photos:
        return None
    seller = offer.get("seller") or {}
    item = Listing(
        "dealer",
        url,
        url,
        title,
        price=price,
        mileage=mileage,
        year=year,
        transmission=gear,
        fuel=propellant,
        kenteken=kenteken,
        vin=vin,
        seller=seller.get("name"),
        city=(seller.get("address") or {}).get("addressLocality"),
        images=_photos(photos),
        raw={
            "productionDate": car.get("productionDate"),
            "vehicleModelDate": car.get("vehicleModelDate"),
        },
    )
    descriptions = tabs.select("#tab3")
    description = _one(descriptions) if descriptions else None
    page = _finish(
        item,
        "stock_dealer_own_vdp",
        {"stock_id": identity[2], "canonical": canonical, "schema_image": image},
        specs,
        description,
    )
    if description is None:
        item.raw["description_source"] = None
    return page


def parse_specific_dealer_detail(body, url):
    """Return an own VDP only for evidenced public hosts and routes."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = parsed.hostname
    if host not in {
        "flevomobiel.nl",
        "www.flevomobiel.nl",
        "www.hedinautomotive.nl",
        "linkerbaan.com",
        "www.linkerbaan.com",
        "www.schalkautomotive.nl",
        "www.drive-works.nl",
    }:
        return None
    soup = BeautifulSoup(body, "html.parser")
    try:
        if host in {"flevomobiel.nl", "www.flevomobiel.nl"}:
            return _flevo(soup, url)
        if host == "www.hedinautomotive.nl":
            return _hedin(soup, body, url)
        if host in {"www.schalkautomotive.nl", "www.drive-works.nl"}:
            return _stock_dealer(soup, body, url)
        return _linkerbaan(soup, url)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
