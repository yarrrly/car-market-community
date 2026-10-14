"""Public API/detail helpers; integration belongs to the collector's caller."""

import dataclasses
import json
import math
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .parse import fuel, gearbox, json_documents, model_year, plate, schema_listing, walk


def major_price(value):
    """Keep unknown prices unknown; never substitute a monthly installment."""
    if not isinstance(value, dict):
        return None
    amount, divisor = value.get("amountMinorUnits"), value.get("conversionMajor")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, (int, float))
        or isinstance(divisor, bool)
        or not isinstance(divisor, (int, float))
        or not math.isfinite(amount)
        or not math.isfinite(divisor)
        or amount < 0
        or divisor <= 0
        or value.get("currency") != "EUR"
    ):
        return None
    return amount / divisor


def enrich_api_listing(item):
    """Return a copy, retaining the API's identity and source."""
    item = dataclasses.replace(item)
    if item.source == "anwb":
        item.images = [
            urljoin("https://cdn.autotrack.nl/", path)
            for path in item.raw.get("media", {}).get("photos", [])
            if isinstance(path, str) and path
        ]
    elif item.source == "autohero":
        item.price = major_price(item.raw.get("offerPrice"))
        # registration is a first-registration timestamp, not a kenteken.
    elif item.source == "autotrack":
        photos = item.raw.get("media", {}).get("photos", [])
        if photos:
            item.images = [
                urljoin("https://cdn.autotrack.nl/", p) for p in photos if isinstance(p, str)
            ]
    return item


def validate_inventory(page, requested_page, expected_total=None, final_url=""):
    """A 200 response can silently reset filters and pagination."""
    issues = []
    if "other.toast=" in final_url:
        issues.append("filter_validation_redirect")
    if page.page != requested_page:
        issues.append("page_number_mismatch")
    if expected_total is not None and page.total != expected_total:
        issues.append("counter_changed")
    if page.total is None or page.pages is None:
        issues.append("counter_unknown")
    return issues


def _label(soup, label):
    values = []
    for node in soup.find_all(string=lambda text: bool(text and text.strip() == label)):
        row = node.find_parent("li")
        spec_key = node.parent.get("data-testid", "")
        if spec_key.startswith("specKey-"):
            spec_values = node.parent.parent.select(
                '[data-testid="specVal-text-' + spec_key.removeprefix("specKey-") + '"]'
            )
            value = spec_values[0].get_text(" ", strip=True) if len(spec_values) == 1 else None
        elif row:
            text = row.get_text(" ", strip=True)
            value = text[len(label) :].strip() if text.startswith(label) else None
        else:
            parent = node.parent.parent
            parts = [n.get_text(" ", strip=True) for n in parent.find_all("p", recursive=False)]
            value = parts[-1] if len(parts) > 1 else None
        if value:
            values.append(value)
    return values[0] if len(set(values)) == 1 else None


class _ClosedDivBlocks(HTMLParser):
    """Locate explicit closing tags; BeautifulSoup repairs incomplete HTML."""

    def __init__(self, body, element):
        super().__init__(convert_charrefs=False)
        self.body = body
        self.element = element
        self.blocks = []
        self.depth = 0
        self.start = None
        self.offsets = [0]
        for line in body.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))
        self.feed(body)

    def _offset(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        if self.depth:
            self.depth += 1
            return
        attributes = dict(attrs)
        if "class" in attributes:
            attributes["class"] = " ".join(attributes["class"].split())
        expected = {
            key: " ".join(value) if isinstance(value, list) else value
            for key, value in self.element.attrs.items()
        }
        if attributes == expected:
            self.start = self._offset()
            self.depth = 1

    def handle_endtag(self, tag):
        if tag != "div" or not self.depth:
            return
        self.depth -= 1
        if not self.depth:
            end = self.body.find(">", self._offset())
            if end != -1:
                self.blocks.append(self.body[self.start : end + 1])


def _comments_complete(body, content):
    if content is None or content.name != "div" or not content.get_text(" ", strip=True):
        return None
    text = content.get_text(" ", strip=True)
    for block in _ClosedDivBlocks(body, content).blocks:
        original = BeautifulSoup(block, "html.parser").find("div")
        if original and original.get_text(" ", strip=True) == text:
            if original.select('[data-truncated="true"], [data-loading="true"]'):
                return None
            if original.get("data-truncated") == "true" or original.get("data-loading") == "true":
                return None
            return True
    return None


def _autotrack_uuid_detail(body, url, identity):
    """Bind the new public layout to its URL, canonical, offer and own Next ID."""
    from .parse import autoscout_detail, number

    canonical = url.split("?", 1)[0].rstrip("/")
    soup = BeautifulSoup(body, "html.parser")
    links = soup.select('link[rel="canonical"]')
    states = soup.select("script#__NEXT_DATA__")
    if len(links) != 1 or links[0].get("href", "").rstrip("/") != canonical or len(states) != 1:
        return None
    try:
        state = json.loads(states[0].string or "")
    except (ValueError, TypeError):
        return None
    if state.get("page") != "/details/[...slug]" or state.get("query", {}).get("slug") != [
        urlsplit(canonical).path.rsplit("/", 1)[-1]
    ]:
        return None
    own = state.get("props", {}).get("pageProps", {}).get("listingDetails")
    if (
        not isinstance(own, dict)
        or own.get("id") != identity
        or not isinstance(own.get("vehicle"), dict)
    ):
        return None
    products = [
        node
        for doc in json_documents(body)
        for node in walk(doc)
        if node.get("@type") == "Product"
        and isinstance(node.get("offers"), dict)
        and str(node["offers"].get("url", "")).rstrip("/") == canonical
    ]
    if len(products) != 1:
        return None
    offer = products[0]["offers"]
    car = offer.get("itemOffered", {})
    if not isinstance(car, dict):
        return None
    if (
        car.get("@type") != "Car"
        or car.get("identifier") != identity
        or offer.get("priceCurrency") != "EUR"
    ):
        return None
    item = autoscout_detail("autotrack", own, canonical)
    # Preserve the established URL namespace used by existing AutoTrack imports.
    item.external_id = canonical
    item.raw = {
        **item.raw,
        "adapter": "autotrack_uuid_own_next",
        "own_uuid": identity,
        "detail_complete": None,
    }
    conflicts = []
    for name, value, expected in (
        ("price", item.price, number(offer.get("price"))),
        ("mileage", item.mileage, number(car.get("mileageFromOdometer"))),
        ("transmission", item.transmission, gearbox(car.get("vehicleTransmission"))),
    ):
        if value is not None and expected is not None and value != expected:
            setattr(item, name, None)
            conflicts.append(name)
    stated_plate = plate(car.get("licensePlate"))
    if stated_plate and item.kenteken and stated_plate != item.kenteken:
        return None
    item.raw["field_conflicts"] = conflicts
    return item.qualify()


def parse_public_detail(source, body, url):
    """Parse only the canonical car, excluding recommended/related inventory."""
    if source not in {"autotrack", "anwb", "viabovag"}:
        return None
    if source == "viabovag" and "/auto/aanbod/" not in urlsplit(url).path:
        return None
    if source in {"autotrack", "anwb"} and "/a/" not in urlsplit(url).path:
        return None
    if source == "autotrack" and (
        urlsplit(url).scheme not in {"http", "https"}
        or urlsplit(url).hostname not in {"autotrack.nl", "www.autotrack.nl"}
    ):
        return None
    uuid = re.search(r"-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?$", urlsplit(url).path)
    if source == "autotrack" and uuid:
        if urlsplit(url).hostname != "www.autotrack.nl":
            return None
        return _autotrack_uuid_detail(body, url, uuid.group(1))
    nodes = [node for doc in json_documents(body) for node in walk(doc)]
    canonical = url.split("?", 1)[0].rstrip("/")
    schemas = [
        node for node in nodes
        if node.get("@type") in ("Car", "Vehicle")
        and str(node.get("url", "")).rstrip("/") == canonical
        and isinstance(node.get("offers"), dict)
    ]
    if len(schemas) != 1:
        return None
    schema = schemas[0]
    schema = dict(schema)
    schema["offers"] = dict(schema["offers"])
    schema["offers"]["seller"] = schema["offers"].get("seller") or schema["offers"].get("offeredBy")
    schema["fuelType"] = schema.get("fuelType") or schema.get("vehicleEngine", {}).get("fuelType")
    # productionDate can be 02-2026; number() would return the false year 2.
    schema.pop("productionDate", None)
    item = schema_listing(source, schema, url)
    soup = BeautifulSoup(body, "html.parser")
    headings = soup.select('h1[data-testid~="vehicle-header-level-1-heading"]')
    if not headings:
        headings = soup.find_all("h1")
    heading = headings[0] if len(headings) == 1 else None
    content = None
    if source == "viabovag":
        subtitle = soup.select_one('[data-testid="vdp-all-in-deal-title"]')
        if heading and subtitle:
            item.title = (
                heading.get_text(" ", strip=True) + " " + subtitle.get_text(" ", strip=True)
            )
        item.external_id = str(schema.get("sku") or canonical)
        comments = soup.select_one(
            '[data-testid="vdp-dealer-comments-mobile"] [data-testid="animated-height-wrapper"]'
        )
        content = comments
        item.description = comments.get_text(" ", strip=True) if comments else None
    else:
        match = re.search(r"-(\d+)$", urlsplit(url).path)
        item.external_id = match.group(1) if match else canonical
        own_cars = [
            n["pageInfo"]["car"] for n in nodes
            if isinstance(n.get("pageInfo"), dict)
            and n["pageInfo"].get("pageType") == "detail"
            and str(n["pageInfo"].get("componentId")) == item.external_id
            and isinstance(n["pageInfo"].get("car"), dict)
        ]
        vehicle = own_cars[0] if len(own_cars) == 1 else {}
        if heading:
            # The commercial edition follows the heading's containing row.
            subtitle = heading.parent.find_next_sibling("p")
            item.title = heading.get_text(" ", strip=True)
            if subtitle:
                item.title += " " + subtitle.get_text(" ", strip=True)
        native_plate = plate(vehicle.get("licensePlate"))
        if native_plate and item.kenteken and native_plate != item.kenteken:
            return None
        item.year = model_year(vehicle.get("buildYear")) or item.year
        item.kenteken = native_plate or item.kenteken
        if vehicle:
            item.raw["own_detail_vehicle"] = vehicle
        sellers = [
            p.get_text(" ", strip=True) for p in soup.find_all("p")
            if re.search(r"\|\s*\d{4}\s*[A-Z]{2}\s*,", p.get_text(" ", strip=True))
        ]
        seller = sellers[0] if len(set(sellers)) == 1 else None
        if seller:
            item.seller, address = seller.split("|", 1)
            item.seller = item.seller.strip()
            item.city = address.split(",", 1)[1].strip()
        comments = soup.find("h3", string="Wat vertelt de aanbieder over deze auto?")
        if comments:
            content = comments.find_next_sibling("div")
            item.description = content.get_text(" ", strip=True) if content else None
    year = _label(soup, "Bouwjaar")
    if year:
        match = re.search(r"\b(?:19|20)\d{2}\b", year)
        item.year = int(match.group()) if match else item.year
    item.kenteken = plate(_label(soup, "Kenteken")) or item.kenteken
    item.fuel = fuel(_label(soup, "Brandstof")) or item.fuel
    item.transmission = gearbox(_label(soup, "Transmissie")) or item.transmission
    item.raw["detail_complete"] = _comments_complete(body, content)
    return item.qualify()
