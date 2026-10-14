import ast
import base64
import json
import math
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from .parse import Listing, Page, fuel, gearbox, number, plate, plate_from_text


def text(node):
    return node.get_text(" ", strip=True) if node else ""


def autowereld_build_year(value):
    """The publisher's own build field is YYYY or MM-YYYY, never a mileage."""
    match = re.fullmatch(r"(?:(?:0?[1-9]|1[0-2])[-/])?(\d{4})", value.strip())
    return int(match.group(1)) if match else None


def explicit_gearbox(content):
    # The words "auto", "autobedrijf", "handig" and climate control do not describe transmission.
    match = re.search(
        r"\b(handgeschakeld|handgeschakelde|manual|automaat|automatische transmissie|steptronic|dct)\b",
        content,
        re.IGNORECASE,
    )
    return gearbox(match.group(1)) if match else None


def mobiel_detail_identity(body, url):
    from bs4 import BeautifulSoup

    def identity(uri):
        parsed = urlsplit(uri)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
            "nederlandmobiel.nl",
            "www.nederlandmobiel.nl",
        }:
            return None
        match = re.fullmatch(r"/tweedehands-auto/.+/(\d+)/?", parsed.path)
        return match.group(1) if match else None

    expected = identity(url)
    if expected is None:
        return None
    soup = BeautifulSoup(body, "html.parser")
    headings = {text(node).casefold() for node in soup.select("h1.detail-title")}
    if len(headings) != 1 or not next(iter(headings)):
        return None
    links = soup.select('link[rel~="canonical"][href]')
    if links and (
        len(links) != 1 or identity(urljoin(url, links[0]["href"])) != expected
    ):
        return None
    affiliate_ids = set()
    for link in soup.select("a[href]"):
        declared = urlsplit(urljoin(url, link["href"]))
        if (
            declared.netloc.lower()
            not in {"nederlandmobiel.nl", "www.nederlandmobiel.nl"}
            or declared.path != "/redirect.php"
        ):
            continue
        query = parse_qs(declared.query)
        if (
            query.get("module") != ["affiliate"]
            or not {"vehicle_id", "car_url"} <= query.keys()
        ):
            continue
        if len(query["vehicle_id"]) != 1 or len(query["car_url"]) != 1:
            return None
        own_id = identity(query["car_url"][0])
        if own_id is None or query["vehicle_id"][0] != own_id:
            return None
        affiliate_ids.add(own_id)
    if affiliate_ids and affiliate_ids != {expected}:
        return None
    return expected if links or affiliate_ids == {expected} else None


def parse_autoweek_detail(body, url):
    """The VDP card URL and data-occasion-id must match."""
    from bs4 import BeautifulSoup

    result = Page([])
    identity = re.search(r"/occasions/[^/]+/[^/]+/(\d+)(?:/|$)", urlsplit(url).path)
    if not identity:
        return result
    identity = identity.group(1)
    soup = BeautifulSoup(body, "html.parser")
    sidebar = soup.select_one(".occasions-vdp-sidebar")
    if not sidebar:
        return result
    ids = {
        node.get("data-occasion-id") for node in sidebar.select("[data-occasion-id]")
    }
    if ids != {identity}:
        return result
    attributes = {}
    for tile in sidebar.select(".spec-tile__content"):
        label, value = (
            text(tile.select_one(".spec-tile__label")),
            text(tile.select_one(".spec-tile__value")),
        )
        if label and value:
            attributes[label] = value
    registrations = set()
    for form in soup.select("form"):
        own = form.select_one('input[name="vehicle_id"][value]')
        if not own or own["value"] != identity:
            continue
        for node in form.select(
            'input[name="kenteken"][value], input[name="car"][value], textarea'
        ):
            value = node.get("value") or text(node)
            candidate = (
                plate(value)
                if node.get("name") == "kenteken"
                else plate_from_text(value)
            )
            if candidate:
                registrations.add(candidate)
    description = (
        text(soup.select_one("#description .occasion-description__text")) or None
    )
    title = " ".join(
        filter(
            None,
            (
                text(sidebar.select_one(".occasions-vdp-sidebar__title")),
                text(sidebar.select_one(".occasions-vdp-sidebar__subtitle")),
            ),
        )
    )
    result.listings = [
        Listing(
            "autoweek",
            identity,
            url,
            title,
            price=number(text(sidebar.select_one(".occasions-vdp-sidebar__price"))),
            mileage=number(attributes.get("Kilometerstand")),
            year=number(attributes.get("Bouwjaar")),
            fuel=fuel(attributes.get("Brandstof")),
            transmission=gearbox(attributes.get("Transmissie")),
            kenteken=next(iter(registrations)) if len(registrations) == 1 else None,
            seller=text(sidebar.select_one(".occasions-vdp-sidebar__dealer-name"))
            or None,
            city=text(sidebar.select_one(".occasions-vdp-sidebar__location-text"))
            or None,
            description=description,
            images=list(
                dict.fromkeys(
                    urljoin(url, node["src"])
                    for node in soup.select("img[src]")
                    if re.search(r"/" + identity + r"/", urlsplit(node["src"]).path)
                )
            ),
            raw={
                "detail_complete": True,
                "attributes": attributes,
                "identity_evidence": identity,
            },
        ).qualify()
    ]
    return result


def parse_dom(source, body, url):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(body, "html.parser")
    result = Page([])
    result.links = list(
        dict.fromkeys(urljoin(url, x["href"]) for x in soup.select("a[href]"))
    )
    if source == "carteam":
        count = re.search(r"([\d.]+)\s+resultaten", text(soup))
        if count:
            result.total = int(count.group(1).replace(".", ""))
        result.page = int(parse_qs(urlsplit(url).query).get("page", [1])[0])
        for card in soup.select("div.card:has(a.card__title-link)"):
            link = card.select_one("a.card__title-link")
            specs = card.select(".card__specs li")
            item = Listing(
                source,
                urljoin(url, link["href"]),
                urljoin(url, link["href"]),
                text(link),
                price=number(text(card.select_one(".card__price")))
                if "Prijs op aanvraag" not in text(card)
                else None,
                year=number(text(specs[1])) if len(specs) > 1 else None,
                mileage=number(text(specs[3])) if len(specs) > 3 else None,
                transmission=gearbox(text(specs[2])) if len(specs) > 2 else None,
                fuel=fuel(text(specs[0])) if specs else None,
                city=text(card.select_one(".is-location")),
                images=[i["src"] for i in card.select("img[src]")],
                raw={"card_text": text(card)},
            ).qualify()
            if "Verkocht" in text(card):
                item.scope = "excluded"
            result.listings.append(item)
        result.pages = 1 if result.total == len(result.listings) else None
    elif source == "autoweek":
        if re.search(r"/occasions/[^/]+/[^/]+/\d+(?:/|$)", urlsplit(url).path):
            return parse_autoweek_detail(body, url)
        counter = re.search(r"([\d.]+)\s+resultaten", text(soup))
        if counter:
            result.total = int(counter.group(1).replace(".", ""))
            result.pages = math.ceil(result.total / 25)
        result.page = int(parse_qs(urlsplit(url).query).get("pagina", [1])[0])
        for card in soup.select("a.aw-tile:has(article.occasion)"):
            ad_url = urljoin(url, card["href"])
            identity = re.search(r"/occasions/[^/]+/[^/]+/(\d+)/", ad_url)
            specs = card.select(".tile-car-meta .meta")
            result.listings.append(
                Listing(
                    source,
                    identity.group(1) if identity else ad_url,
                    ad_url,
                    text(card.select_one("h3")) + " " + text(card.select_one("h4")),
                    price=number(text(card.select_one(".tile-price"))),
                    year=number(text(specs[0])) if specs else None,
                    mileage=number(text(specs[1])) if len(specs) > 1 else None,
                    fuel=fuel(text(specs[2])) if len(specs) > 2 else None,
                    transmission=gearbox(text(specs[3])) if len(specs) > 3 else None,
                    city=text(card.select_one(".supplier-type-city")),
                    images=[x["src"] for x in card.select("img[src]")],
                    raw={"card_text": text(card)},
                ).qualify()
            )
    elif source == "autokopen":
        from .autokopen_scope import qualify_search, search_records

        native_records = search_records(body)
        content = text(soup)
        counter = re.search(r"\d+\s+van\s+([\d.]+)\s+resultaten geladen", content)
        if counter:
            result.total = int(counter.group(1).replace(".", ""))
            result.pages = math.ceil(result.total / 24)
        result.page = int(parse_qs(urlsplit(url).query).get("page", [1])[0])
        for card in soup.select(".vehicle-card[data-vehicle-id]"):
            link = card.select_one('a[href*="/auto/detail/"]')
            if not link:
                continue
            heading = text(card.select_one("h3"))
            variant = text(card.select_one("h3 + p"))
            content = text(card)
            km = re.search(r"([\d.]+)\s*km", content)
            year = re.search(r"\b(20\d{2})\b", heading)
            price = re.search(r"€\s*([\d.,]+)", content)
            item = Listing(
                source,
                card["data-vehicle-id"],
                urljoin(url, link["href"]),
                heading + " " + variant,
                price=number(price.group(1))
                if price and "Prijs op aanvraag" not in content
                else None,
                price_type="advertised_price"
                if price and "Prijs op aanvraag" not in content
                else "unknown",
                mileage=number(km.group(1)) if km else None,
                year=int(year.group(1)) if year else None,
                transmission=gearbox(content),
                fuel=fuel(content),
                description=content,
                images=[urljoin(url, x["src"]) for x in card.select("img[src]")],
                raw={"card_text": content},
            ).qualify()
            records = native_records.get(item.external_id, [])
            if len(records) == 1:
                item.raw["autokopen_search"] = {
                    "record": records[0],
                    "dom_id": item.external_id,
                    "dom_uri": item.url,
                    "dom_title": item.title,
                    "dom_fuel": item.fuel,
                }
                qualify_search(item)
            result.listings.append(item)
    elif source == "autowereld":
        counter = text(soup.select_one("#filter-tree-apply"))
        match = re.search(r"([\d.]+)\s+occasions", counter)
        if match:
            result.total = int(match.group(1).replace(".", ""))
            result.pages = math.ceil(result.total / 15)
        result.page = int(parse_qs(urlsplit(url).query).get("p", [1])[0])
        for card in soup.select("#mainlist article.item"):
            link = card.select_one("a.frame[href]")
            if not link:
                continue
            ad_url = urljoin(url, link["href"])
            match = re.search(r"/(\d+)/details", ad_url)
            specs = text(card.select_one(".specs"))
            item = Listing(
                source,
                match.group(1) if match else ad_url,
                ad_url,
                text(card.select_one("h3")),
                price=number(text(card.select_one(".price"))),
                mileage=number(text(card.select_one(".mileage"))),
                year=autowereld_build_year(text(card.select_one(".build"))),
                transmission=gearbox(specs),
                fuel=fuel(specs),
                seller=text(card.select_one(".seller .name")),
                city=text(card.select_one(".seller .location")),
                images=[
                    x["src"]
                    for x in card.select("img[src]")
                    if "autowereld" in x["src"]
                ],
                raw={"card_text": text(card)},
            ).qualify()
            result.listings.append(item)
    elif source == "nederlandmobiel":
        if "/tweedehands-auto/" in urlsplit(url).path:
            if not soup.select_one("h1.detail-title") or not mobiel_detail_identity(
                body, url
            ):
                return result
            attributes = {}
            for row in soup.select("tr"):
                cells = row.find_all("td", recursive=False)
                if len(cells) == 2:
                    attributes[text(cells[0])] = (
                        " ".join(
                            x.strip()
                            for x in cells[1].find_all(string=True, recursive=False)
                            if x.strip()
                        )
                        if text(cells[0]) == "Kenteken"
                        else text(cells[1])
                    )
            year = re.search(r"\b(19\d{2}|20\d{2})\b", attributes.get("Bouwjaar", ""))
            identity = re.search(r"/(\d+)$", urlsplit(url).path)
            result.listings = [
                Listing(
                    source,
                    identity.group(1) if identity else url,
                    url,
                    text(soup.select_one("h1.detail-title")),
                    price=number(text(soup.select_one(".detail-price"))),
                    mileage=number(attributes.get("Kmstand")),
                    year=int(year.group(1)) if year else None,
                    fuel=fuel(attributes.get("Brandstof")),
                    transmission=gearbox(attributes.get("Transmissie")),
                    kenteken=plate(attributes.get("Kenteken")),
                    seller=attributes.get("Bedrijfsnaam"),
                    city=attributes.get("Plaats"),
                    description=text(
                        soup.select_one('[id^="detail-"][id$="-description"]')
                    )
                    or None,
                    images=list(
                        dict.fromkeys(
                            x["src"]
                            for x in soup.select("img[src]")
                            if "images.nederlandmobiel.nl/auto/" in x["src"]
                        )
                    ),
                    raw={"detail_complete": True, "attributes": attributes},
                ).qualify()
            ]
            return result
        cards = soup.select("div.card")
        for card in cards:
            link = card.select_one('h4 a[href*="tweedehands-auto"]')
            if not link:
                continue
            ad_url = urljoin(url, link["href"])
            match = re.search(r"/(\d+)(?:\?|$)", ad_url)
            content = text(card)
            km = re.search(r"([\d.]+)\s*km\s*-\s*(\d{4})", content)
            if any(x.startswith("special-") for x in card.get("class", [])):
                query = parse_qs(urlsplit(url).query)
                own_title = text(link)
                active_family = "/bmw/2-serie-active-tour" in urlsplit(
                    url
                ).path or "2699" in query.get("model[]", [])
                series_family = "2178" in query.get("model[]", [])
                if (
                    "special-2" not in card.get("class", [])
                    or not (active_family or series_family)
                    or not re.search(r"(?i)\bbmw\s+2[- ]serie\b", own_title)
                    or (
                        active_family
                        and not re.search(r"(?i)active\s*[- ]?\s*tour", own_title)
                    )
                ):
                    continue
                bounds = [
                    ("bouwjaar_van", lambda y, b: y >= b),
                    ("bouwjaar_tot", lambda y, b: y <= b),
                ]
                if any(
                    key in query
                    and (
                        not km
                        or not query[key][0].isdigit()
                        or not compare(int(km.group(2)), int(query[key][0]))
                    )
                    for key, compare in bounds
                ):
                    continue
            money = re.search(r"€\s*([\d.,]+)", content)
            item = Listing(
                source,
                match.group(1) if match else ad_url,
                ad_url,
                text(link),
                price=number(money.group(1)) if money else None,
                mileage=number(km.group(1)) if km else None,
                year=number(km.group(2)) if km else None,
                fuel=fuel(content),
                transmission=explicit_gearbox(content),
                description=content,
                images=[urljoin(url, x["src"]) for x in card.select("img[src]")],
                raw={"card_text": content},
            ).qualify()
            result.listings.append(item)
        result.page = int(parse_qs(urlsplit(url).query).get("p-page", [1])[0])
        total = re.search(
            r"([\d.]+)\s+(?:auto.s gevonden|resultaten|occasions gevonden)",
            text(soup),
            re.IGNORECASE,
        )
        if total:
            result.total = int(total.group(1).replace(".", ""))
            result.pages = math.ceil(result.total / 25)
    elif source == "trovit":
        counter = re.search(r"\d+\s*[-–]\s*\d+\s+van de\s+([\d.]+)\s+auto", text(soup))
        if counter:
            result.total = int(counter.group(1).replace(".", ""))
            result.pages = math.ceil(result.total / 25)
        for link in soup.select('a[href*="rd.clk.thribee.com"]'):
            heading = link.select_one("h4.item-title")
            if not heading:
                continue
            content = text(link)
            ad_url = link["href"]
            identity = re.search(r"/id\.([^/]+)", ad_url)
            km = re.search(r"([\d.]+)\s*[Kk]m", content)
            year = re.search(r"\b(20\d{2})\b", content)
            result.listings.append(
                Listing(
                    source,
                    identity.group(1) if identity else ad_url,
                    ad_url,
                    text(heading),
                    price=number(text(link.select_one(".actual-price"))),
                    mileage=number(km.group(1)) if km else None,
                    year=number(year.group(1)) if year else None,
                    fuel=fuel(content),
                    transmission=explicit_gearbox(content),
                    description=content,
                    city=text(link.select_one(".item-address")),
                    kenteken=plate_from_text(content),
                    images=[
                        urljoin(url, x.get("data-src") or x["src"])
                        for x in link.select("img[src]")
                    ],
                    raw={"card_text": content},
                ).qualify()
            )
        match = re.search(r"/(\d+)/?$", urlsplit(url).path)
        result.page = int(match.group(1)) if match else 1
    elif source == "marktnet":
        heading = text(soup.select_one("h1"))
        if re.search(r"218\s*i", heading, re.IGNORECASE) and urlsplit(
            url
        ).path.endswith(".htm"):
            content = text(soup)
            description = content.split("Beschrijving", 1)[-1].split(
                "Andere advertenties", 1
            )[0]
            km = re.search(r"Tellerstand:\s*([\d.]+)\s*km", description, re.IGNORECASE)
            year = re.search(
                r"Bouwjaar:\s*(?:[a-z.]+\s*)?(\d{4})", description, re.IGNORECASE
            )
            identity = re.search(r"-(\d+)\.htm", url)
            result.listings = [
                Listing(
                    source,
                    identity.group(1) if identity else url,
                    url,
                    heading,
                    price=number(text(soup.select_one('[itemprop="price"]'))),
                    mileage=number(km.group(1)) if km else None,
                    year=number(year.group(1)) if year else None,
                    kenteken=plate_from_text(description),
                    fuel=fuel(description),
                    transmission=gearbox(description),
                    description=description,
                    raw={"detail_complete": True},
                ).qualify()
            ]
            return result
        total = re.search(r"([\d.]+)\s+advertenties gevonden", text(soup))
        if total:
            result.total = int(total.group(1).replace(".", ""))
            result.pages = math.ceil(result.total / 20)
        result.page = int(parse_qs(urlsplit(url).query).get("p", [1])[0])
        seen = set()
        for link in soup.select('a[href$=".htm"]'):
            title = text(link)
            if (
                not title
                or len(title) > 500
                or not re.search(r"218\s*i", title, re.IGNORECASE)
            ):
                continue
            ad_url = urljoin(url, link["href"])
            if ad_url in seen:
                continue
            seen.add(ad_url)
            card = link
            for _ in range(4):
                if card.parent:
                    card = card.parent
                if "€" in text(card):
                    break
            content = text(card)
            money = re.search(r"€\s*([\d.,]+)", content)
            identity = re.search(r"-(\d+)\.htm", ad_url)
            km = re.search(r"Tellerstand:\s*([\d.]+)\s*km", content, re.IGNORECASE)
            year = re.search(
                r"Bouwjaar:\s*(?:[a-z.]+\s*)?(\d{4})", content, re.IGNORECASE
            )
            result.listings.append(
                Listing(
                    source,
                    identity.group(1) if identity else ad_url,
                    ad_url,
                    title,
                    price=number(money.group(1)) if money else None,
                    fuel=fuel(content),
                    transmission=explicit_gearbox(content),
                    mileage=number(km.group(1)) if km else None,
                    year=number(year.group(1)) if year else None,
                    kenteken=plate_from_text(content),
                    description=content,
                    raw={"card_text": content},
                ).qualify()
            )
    elif source == "bmw":
        identity = re.search(r"/details/id/(\d+)", urlsplit(url).path)
        if identity and soup.select_one(".property-bmwm-total .price"):
            attributes = {}
            for row in soup.select("tr"):
                heading, value = row.find("th"), row.find("td")
                if heading and value:
                    attributes[text(heading)] = text(value)
            year = re.search(
                r"\b(19\d{2}|20\d{2})\b", attributes.get("Datum deel 1", "")
            )
            location = soup.select_one(".table-opening-map-left")
            address = location.select("p") if location else []
            city = (
                re.sub(r"^\d{4}\s*[A-Z]{2}\s*", "", text(address[1]))
                if len(address) > 1
                else None
            )
            description = (
                "\n".join(
                    text(section)
                    for section in soup.select("#infoBlock section.detailsSpecial")
                    if text(section)
                )
                or None
            )
            result.listings = [
                Listing(
                    source,
                    identity.group(1),
                    url,
                    text(soup.select_one("h2")),
                    price=number(text(soup.select_one(".property-bmwm-total .price"))),
                    year=int(year.group(1)) if year else None,
                    mileage=number(attributes.get("Kilometerstand")),
                    kenteken=plate(attributes.get("Kenteken")),
                    fuel=fuel(attributes.get("Brandstof")),
                    transmission=gearbox(attributes.get("Transmissie")),
                    seller=text(soup.select_one("nobr")),
                    city=city,
                    description=description,
                    images=list(
                        dict.fromkeys(
                            x["src"]
                            for x in soup.select("img[src]")
                            if "carImage.php" in x["src"]
                        )
                    ),
                    raw={"detail_complete": True, "attributes": attributes},
                ).qualify()
            ]
            return result
        match = re.search(
            r"Action\.loadVehicles\(\$\.parseJSON\(\x27((?:\\.|[^\x27])*)\x27\)",
            body,
            re.DOTALL,
        )
        if match:
            # Decode a JS string's JSON escaping only; never evaluate JS.
            encoded = match.group(1)
            try:
                data = json.loads(
                    ast.literal_eval("'" + encoded.replace("\\/", "/") + "'")
                )
            except ValueError:
                data = {}
            for item in data.get("vehicles", []):
                identity = str(item.get("vehicleId"))
                ad_url = (
                    item.get("url")
                    or item.get("detailUrl")
                    or "/bmw/zoeken/resultaten/details/id/" + identity
                )
                title = " ".join(
                    str(item.get(k) or "") for k in ["name", "serie", "description"]
                )
                result.listings.append(
                    Listing(
                        source,
                        identity,
                        urljoin(url, ad_url),
                        title,
                        price=number(item.get("price")),
                        mileage=number(item.get("mileage")),
                        year=number(str(item.get("datePartOne", ""))[:4]),
                        transmission=gearbox(item.get("transmission")),
                        fuel=fuel(item.get("fuel")),
                        kenteken=plate(item.get("licensePlateScreen")),
                        seller=item.get("dealerName"),
                        description=item.get("description"),
                        images=item.get("thumbnails")
                        or [
                            urljoin(url, x)
                            for x in str(item.get("images") or "").split("|")
                            if x
                        ],
                        raw=item,
                    ).qualify()
                )
    if source == "bmw":
        counter = soup.select_one("#carCounter[value]")
        if counter:
            count = number(counter.get("value"))
            if count is not None:
                result.total = int(count)
                result.pages = math.ceil(result.total / 18)

        def page_number(link):
            value = parse_qs(urlsplit(link).query).get("filters", [""])[0]
            try:
                decoded = base64.b64decode(value).decode()
                match = re.search(r's:4:"page";i:(\d+)', decoded)
                return int(match.group(1)) if match else 1
            except (ValueError, UnicodeError):
                return 1

        result.page = page_number(url)
        result.pages = max([page_number(x) for x in result.links] + [result.pages or 1])
    return result
