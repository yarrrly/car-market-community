"""Public detail adapters verified against the dealer audit of 2026-10-07.

This wrapper is intentionally opt-in until the collector integration is reviewed.
"""

import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, TemplateString

from .parse import (
    Listing,
    Page,
    fuel,
    gearbox,
    json_documents,
    number,
    parse_page,
    plate,
    plate_from_text,
    schema_listing,
    walk,
)

SUPPORTED_SOURCES = frozenset(("autofirst", "vakgarage", "carteam", "autokopen", "autouncle"))


def _text(node):
    return node.get_text(" ", strip=True, types=(NavigableString, TemplateString)) if node else ""


def _vin(value):
    value = str(value or "").upper().strip()
    return value if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", value) and len(set(value)) > 1 else None


def _price_guard(item):
    if item.source == "autouncle" and item.fuel is None:
        text = item.title + " " + (item.description or "")
        if (
            re.search(r"(?i)\b218\s*i?\b", text)
            and re.search(r"(?i)active\s*[- ]?\s*tour", text)
            and not re.search(
                r"(?i)\b(?:216|220|225|223|230)\s*(?:i|e|xe)\b|\b218\s*d\b|gran\s*(?:tour|coup)",
                text,
            )
        ):
            item.scope = "needs_confirmation"
    # AutoKopen 11537797 advertises 11950 as 50%, not a confirmed full price.
    if re.search(r"(?i)betaal\s+nu\s+\d+\s*%|aanbetaling|(?:per|/)\s*m(?:aand|nd)\b", item.title):
        item.raw = dict(item.raw, price_warning="conditional_or_instalment_price")
        item.price = None
        item.price_type = "unknown"
    return item


def _specs(soup):
    result = {}
    for row in soup.select("tr.od__speclist-item, .row.gutters-1"):
        cells = row.find_all(["td", "div"], recursive=False)
        if len(cells) == 2:
            result[_text(cells[0])] = _text(cells[1])
    for label in soup.select("dt"):
        result[_text(label)] = _text(label.find_next_sibling("dd"))
    return result


def _description_node(soup, source):
    if source == "autofirst":
        return soup.select_one("#extrainformatie")
    if source == "vakgarage" and soup.select_one("#vehicle-remarks"):
        return soup.select_one("#vehicle-remarks")
    node = soup.select_one("#omschrijving")
    if node:
        return node
    label = soup.find(["h2", "div"], string=lambda x: x and x.strip() == "Omschrijving")
    return label.find_next_sibling("p") if label else None


def _description_quality(item, node, source):
    text = _text(node) if node else ""
    if text:
        item.description = text
    # A schema summary is not proof that the seller's entire description was captured.
    isolated = node is not None and source in ("autofirst", "vakgarage", "carteam", "autokopen")
    truncated = bool(re.search(r"(?:\.{3,}|…)\s*$|(?i:lees meer|read more|bekijk alles)\s*$", text))
    if node:
        truncated = truncated or any(
            re.search(
                r"line-clamp|truncate|text-overflow\s*:\s*ellipsis",
                " ".join(n.get("class", [])) + " " + n.get("style", ""),
            )
            or n.get("data-truncated") in ("true", "1")
            for n in [node, *node.select("[class], [style], [data-truncated]")]
        )
    portal_summary = source == "autokopen" and bool(
        re.match(r"Deze BMW .* uit \d{4} heeft een kilometerstand .* wordt aangeboden door", text)
    )
    complete = bool(isolated and len(text.split()) >= 8 and not truncated and not portal_summary)
    item.raw = dict(
        item.raw,
        detail_complete=complete,
        description_source="seller_main"
        if isolated and not portal_summary
        else "portal_or_schema_summary",
        description_truncated=bool(truncated),
    )
    return item


def parse_quality_page(source, body, url):
    """Use primary detail data, excluding recommendations and unrelated schemas."""
    if source not in SUPPORTED_SOURCES:
        return parse_page(source, body, url)
    soup = BeautifulSoup(body, "html.parser")
    specs = _specs(soup)
    description_node = _description_node(soup, source)
    description = _text(description_node) or None
    if source == "autofirst" and soup.select_one("contact-modal[car-id]"):
        modal = soup.select_one("contact-modal[car-id]")
        seller = json.loads(modal.get(":establishment", "{}"))
        for row in soup.select("li.flex.justify-between"):
            cells = row.find_all("div", recursive=False)
            if len(cells) == 2:
                specs[_text(cells[0])] = _text(cells[1])
        heading = soup.select_one("h1")
        price_node = soup.select_one(".font-header.space-x-1 span")
        slider = soup.select_one("slider")
        slides = json.loads(slider.get(":slides", "[]")) if slider else []
        item = Listing(
            source,
            modal["car-id"],
            url,
            _text(heading) + " " + _text(heading.find_next_sibling("p")),
            price=number(_text(price_node)),
            year=number(specs.get("Bouwjaar")),
            mileage=number(specs.get("Kilometerstand")),
            fuel=fuel(specs.get("Brandstof")),
            transmission=gearbox(specs.get("Transmissie")),
            kenteken=plate(modal.get("vehicle-license-plate")),
            seller=seller.get("title"),
            city=seller.get("city"),
            description=_text(soup.select_one("#extrainformatie")),
            images=[
                x["attributes"]["image"] for x in slides if x.get("attributes", {}).get("image")
            ],
            raw={"detail_adapter": "autofirst_primary_dom", "specs": specs},
        ).qualify()
        return Page([_price_guard(_description_quality(item, description_node, source))])
    if source == "carteam" and soup.select_one("h1.od__title"):
        company = soup.select_one(".company-info__content p")
        company_text = company.get_text("\n", strip=True).splitlines() if company else []
        city_match = (
            re.search(r"\b\d{4}\s*[A-Z]{2}\s+(.+)", company_text[-1]) if company_text else None
        )
        item = Listing(
            source,
            url,
            url,
            _text(soup.select_one("h1.od__title")),
            price=number(_text(soup.select_one(".od__price .price"))),
            year=number(specs.get("Bouwjaar")),
            mileage=number(specs.get("Kilometerstand") or specs.get("Tellerstand")),
            fuel=fuel(specs.get("Brandstof")),
            transmission=gearbox(specs.get("Transmissie")),
            kenteken=plate(specs.get("Kenteken")),
            vin=_vin(specs.get("Chassisnummer")),
            seller=_text(company.select_one("b")) if company else None,
            city=city_match.group(1) if city_match else None,
            description=description,
            images=list(
                dict.fromkeys(
                    urljoin(url, a["href"]) for a in soup.select('a[data-fancybox="gallery"][href]')
                )
            ),
            raw={"detail_adapter": "carteam_primary_dom", "specs": specs},
        ).qualify()
        return Page([_price_guard(_description_quality(item, description_node, source))])
    if source == "vakgarage" and soup.select_one("[data-vehicle-data]"):
        ad = json.loads(soup.select_one("[data-vehicle-data]")["data-vehicle-data"])
        if ad.get("is_vehicle_detail") is True:
            prices = [x.get("price") for x in ad.get("prices", []) if x.get("type") == "catalog"]
            identity = re.search(r"/p/(\d+)/", urlsplit(url).path)
            item = Listing(
                source,
                identity.group(1) if identity else url,
                url,
                " ".join(str(ad.get(k) or "") for k in ("brand", "model", "type")),
                price=number(prices[0]) if len(prices) == 1 else None,
                year=number(specs.get("Bouwjaar")),
                mileage=number(specs.get("Tellerstand")),
                fuel=fuel(specs.get("Brandstof")),
                transmission=gearbox(specs.get("Transmissie")),
                kenteken=plate(ad.get("licensePlate")),
                seller=ad.get("location"),
                description=description,
                images=[x["path"] for x in ad.get("images", []) if x.get("path")],
                raw={"detail_adapter": "vakgarage_vehicle_data", "data": ad, "specs": specs},
            ).qualify()
            return Page([_price_guard(_description_quality(item, description_node, source))])
    primary = []
    for doc in json_documents(body):
        for node in walk(doc):
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if not any(t in types for t in ("Car", "Vehicle", "Product")) or not node.get("offers"):
                continue
            offers = node["offers"]
            offer = offers[0] if isinstance(offers, list) and offers else offers
            if not isinstance(offer, dict):
                continue
            declared = node.get("url") or node.get("@id") or offer.get("url")
            # Never accept a recommendation merely because there is just one schema.
            if not declared or urljoin(url, declared).split("#")[0].rstrip("/") != url.split("#")[
                0
            ].rstrip("/"):
                continue
            item = schema_listing(source, node, url)
            item.title += " " + str(node.get("vehicleConfiguration") or "")
            item.vin = _vin(node.get("vehicleIdentificationNumber"))
            image = node.get("image", [])
            image = image if isinstance(image, list) else [image]
            item.images = [
                urljoin(url, x.get("url", "") if isinstance(x, dict) else x)
                for x in image
                if (x.get("url") if isinstance(x, dict) else x)
            ]
            item.description = description or item.description
            item.kenteken = item.kenteken or plate_from_text(item.description or "")
            place = offer.get("availableAtOrFrom", {})
            if isinstance(place, dict) and isinstance(place.get("address"), dict):
                item.city = item.city or place["address"].get("addressLocality")
            primary.append(
                _price_guard(_description_quality(item.qualify(), description_node, source))
            )
    if len(primary) == 1:
        return Page(primary)
    if (
        source == "autokopen"
        and "/auto/detail/" in urlsplit(url).path
        and specs
        and soup.select_one("h1")
    ):
        heading = soup.select_one("h1")
        title = _text(heading) + " " + _text(heading.find_next_sibling("p"))
        # A price-on-request detail intentionally has no Offer schema. Do not
        # substitute fiscal value, original new price, or a monthly payment.
        item = Listing(
            source,
            url,
            url,
            title,
            price_type="unknown",
            year=number(specs.get("Bouwjaar")),
            mileage=number(specs.get("Kilometerstand")),
            fuel=fuel(specs.get("Brandstof")),
            transmission=gearbox(specs.get("Transmissie")),
            kenteken=plate_from_text(description or ""),
            description=description,
            raw={"detail_adapter": "autokopen_price_on_request", "specs": specs},
        ).qualify()
        return Page([_price_guard(_description_quality(item, description_node, source))])
    page = parse_page(source, body, url)
    if source == "autouncle" and "/gebruikte-auto/" in urlsplit(url).path:
        own_items = {item.url: item for item in page.listings}
        counters = {
            int(match.group(1).replace(".", ""))
            for node in soup.select('[data-testid="car-search-submit-button"]')
            if (match := re.fullmatch(r"([\d.]+)\s+auto['’]s", _text(node)))
        }
        if not page.listings and len(counters) == 1:
            page.total = counters.pop()
            page.pages = (page.total + 24) // 25
        # Live 07.10.2026 cards retain their own article even without ItemList JSON-LD.
        for article in soup.find_all("article"):
            anchor = article.find("a", href=re.compile(r"^/nl-nl/d/\d+-"))
            if not anchor or not anchor.find("h3"):
                continue
            heading = anchor.find("h3")
            title = _text(heading) + " " + _text(heading.find_next_sibling("p"))
            specs_text = [_text(node) for node in anchor.select("li")]
            details = " ".join(specs_text)
            year = re.search(r"Occasion\s*\((\d{4})\)", _text(heading))
            mileage = re.search(r"([\d.]+)\s*km\b", details)
            # Rating/discount/history amounts live outside the immediate price block.
            price_block = anchor.find_next_sibling("div")
            prices = (
                price_block.find_all(string=re.compile(r"^\s*€\s*[\d.,]+\s*$"))
                if price_block
                else []
            )
            price = next((number(x) for x in prices if not x.find_parent(["del", "s"])), None)
            source_label = article.select_one('[data-testid="source-label"]')
            city_node = source_label.find_next_sibling("div") if source_label else None
            own_url = urljoin(url, anchor["href"])
            item = Listing(
                source,
                own_url,
                own_url,
                title,
                price=price,
                year=int(year.group(1)) if year else None,
                mileage=number(mileage.group(1)) if mileage else None,
                transmission=gearbox(details),
                fuel=fuel(details),
                city=_text(city_node) or None,
                images=[
                    node["src"]
                    for node in article.select("img[src]")
                    if "images.autouncle.com/" in node["src"]
                ],
                raw={
                    "search_adapter": "autouncle_article",
                    "source_portal": _text(source_label),
                    "detail_complete": False,
                },
            ).qualify()
            own_items[own_url] = item
        page.listings = list(own_items.values())
    page.listings = [
        _price_guard(_description_quality(item, None, source)) for item in page.listings
    ]
    return page
