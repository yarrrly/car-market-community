import dataclasses
import html
import json
import math
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def json_documents(body):
    """Decode JSON-LD, Next state and React Flight without evaluating JavaScript."""
    for attrs, text in re.findall(
        r"<script([^>]*)>(.*?)</script>", body, re.DOTALL | re.IGNORECASE
    ):
        if "json" in attrs.lower():
            try:
                yield json.loads(text)
            except ValueError:
                pass
    chunks = []
    for match in re.finditer(
        r"self\.__next_f\.push\((\[.*?\])\)</script>", body, re.DOTALL
    ):
        try:
            part = json.loads(match.group(1))
            if len(part) > 1 and isinstance(part[1], str):
                chunks.append(part[1])
        except ValueError:
            pass
    decoder = json.JSONDecoder()
    stream = "".join(chunks).encode("utf-8")
    header = re.compile(rb"[0-9a-f]+:")
    text_header = re.compile(rb"T([0-9a-f]+),")
    offset = 0
    while offset < len(stream):
        match = header.match(stream, offset)
        if match:
            start = match.end()
            text_record = text_header.match(stream, start)
            if text_record:
                # Flight text records use a UTF-8 byte count and need not end
                # with a newline. Their contents are text, never JSON state.
                offset = text_record.end() + int(text_record.group(1), 16)
                if offset > len(stream):
                    break
                if stream[offset : offset + 1] == b"\n":
                    offset += 1
                continue
        else:
            start = offset
        end = stream.find(b"\n", start)
        if end < 0:
            end = len(stream)
        if match:
            try:
                yield decoder.raw_decode(stream[start:end].decode("utf-8"))[0]
            except (ValueError, UnicodeDecodeError):
                pass
        offset = end + 1


def number(value):
    if isinstance(value, (float, int)):
        return value
    if isinstance(value, dict):
        return number(value.get("value"))
    match = re.search(r"\d[\d.,\s]*", str(value or ""))
    if not match:
        return None
    text = match.group().strip().replace(" ", "")
    # NL whole-price/mileage typography: 12.950 and 12,950.
    text = re.sub(r"(?<=\d)[.,](?=\d{3}(?:\D|$))", "", text)
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def plate(value):
    text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return (
        text
        if 5 <= len(text) <= 8 and re.search("[A-Z]", text) and re.search("[0-9]", text)
        else None
    )


def model_year(value):
    if isinstance(value, dict):
        value = value.get("value")
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}(?:\.0)?", text):
        year = int(float(text))
    else:
        match = re.fullmatch(
            r"(\d{4})-\d{2}-\d{2}(?:[T ].*)?|\d{2}[-/]\d{2}[-/](\d{4})", text
        )
        year = int(match.group(1) or match.group(2)) if match else None
    return year if year is not None and 1886 <= year <= 2100 else None


def plate_from_text(text):
    # Avoid interpreting arbitrary six-letter words as Dutch registrations.
    for pattern in [
        r"(?i)kenteken\s*[:=\-]?\s*([A-Z0-9]{1,3}-[A-Z0-9]{1,3}-[A-Z0-9]{1,3}|[A-Z0-9]{6})",
        r"\[([A-Z0-9]{1,3}-[A-Z0-9]{1,3}-[A-Z0-9]{1,3})\]",
    ]:
        match = re.search(pattern, text)
        if match:
            result = plate(match.group(1))
            if result:
                return result


def gearbox(value):
    text = str(value or "").lower()
    if any(x in text for x in ["hand", "manual", "schalt"]):
        return "manual"
    if any(x in text for x in ["auto", "dct", "steptronic"]):
        return "automatic"
    return None


def fuel(value):
    text = str(value or "").lower()
    parts = set(re.split(r"[\s/,+()-]+", text.strip())) - {"", "en"}
    petrol_terms = {"benzine", "benzin", "petrol", "gasoline"}
    electric_terms = {
        "elektro",
        "elektrisch",
        "elektriciteit",
        "electric",
        "electricity",
    }
    mixed_fuel = bool(parts & petrol_terms and parts & electric_terms) and parts <= (
        petrol_terms | electric_terms
    )
    if "hybr" in text or mixed_fuel:
        return "hybrid"
    if parts & {"lpg", "autogas"} and parts <= petrol_terms | {"lpg", "autogas"}:
        return "lpg"
    if any(x in text for x in ["benz", "petrol", "gasoline"]):
        return "petrol"
    if "diesel" in text:
        return "diesel"
    if text.strip() in {
        "electric",
        "electricity",
        "elektrisch",
        "elektriciteit",
        "elektro",
        "ev",
        "bev",
    }:
        return "electric"
    if text.strip() in {"lpg", "autogas"}:
        return "lpg"
    if text.strip() in {"hydrogen", "wasserstoff", "waterstof"}:
        return "hydrogen"
    return None


@dataclasses.dataclass
class Listing:
    source: str
    external_id: str
    url: str
    title: str
    price: float | None = None
    price_type: str = "advertised_price"
    mileage: int | None = None
    year: int | None = None
    transmission: str | None = None
    fuel: str | None = None
    kenteken: str | None = None
    vin: str | None = None
    seller: str | None = None
    city: str | None = None
    description: str | None = None
    images: list = dataclasses.field(default_factory=list)
    links: list = dataclasses.field(default_factory=list)
    scope: str = "unknown"
    raw: dict = dataclasses.field(default_factory=dict)

    def qualify(self):
        from .search_profile import is_configured, qualify

        if is_configured(self):
            return qualify(self)
        from .model_profiles import is_mercedes, qualify_mercedes

        if is_mercedes(self):
            return qualify_mercedes(self)
        if (
            self.source.endswith("_de")
            or self.raw.get("market_context_country") == "DE"
        ):
            from .germany import qualify_german_item

            return qualify_german_item(self)
        text = (self.title + " " + (self.description or "")).lower()
        active = bool(re.search(r"active\s*[- ]?\s*tour", text))
        exact = bool(re.search(r"\b218\s*i\b", text))
        other = bool(
            re.search(
                r"\b(?:216|220|225|223|230)\s*(?:i|e|xe)\b|\b218\s*d\b|gran\s*(?:tour|coup)",
                text,
            )
        )
        if other or self.fuel in ["diesel", "hybrid"]:
            self.scope = "excluded"
        elif active and exact:
            self.scope = "target" if self.fuel == "petrol" else "needs_confirmation"
        elif (
            active
            and self.fuel == "petrol"
            and (
                re.search(r"\b218\b", text)
                or (
                    re.search(r"\bbmw\b", text)
                    and re.search(r"\b1[.,]5\b", text)
                    and not re.search(
                        r"\b(?:216|220|225|223|230)\s*(?:i|d|e|xe)?\b",
                        self.title,
                        re.IGNORECASE,
                    )
                )
            )
        ):
            self.scope = "needs_confirmation"
        else:
            self.scope = "excluded"
        return self


@dataclasses.dataclass
class Page:
    listings: list
    total: int | None = None
    pages: int | None = None
    page: int | None = None
    links: list = dataclasses.field(default_factory=list)
    search_filters: dict = dataclasses.field(default_factory=dict)


def schema_listing(source, item, base):
    offer = item.get("offers", {})
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    seller = offer.get("seller") or item.get("seller") or {}
    address = seller.get("address") or {}
    if not isinstance(address, dict):
        address = {}
    image = item.get("image", [])
    if isinstance(image, dict):
        image = image.get("url", "")
    if isinstance(image, str):
        image = [image]
    name = item.get("name", "")
    url = item.get("url") or item.get("@id") or offer.get("url") or base
    url = urljoin(base, url)
    # Preserve Gaspedaal's fragment identity; strip schema #product/#offer elsewhere.
    if source != "gaspedaal":
        url = url.split("#")[0]
    external = url.rsplit("#", 1)[-1] if source == "gaspedaal" else url
    engine = item.get("vehicleEngine") or {}
    engine = engine if isinstance(engine, dict) else {}
    transmission = item.get("vehicleTransmission")
    if isinstance(transmission, dict):
        transmission = transmission.get("value")
    vin = str(item.get("vehicleIdentificationNumber") or "").upper().strip()
    vin = (
        vin if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) and len(set(vin)) > 1 else None
    )
    description = item.get("description", "")
    return Listing(
        source,
        external,
        url,
        name,
        price=number(offer.get("price")),
        mileage=number(item.get("mileageFromOdometer")),
        year=model_year(item.get("productionDate") or item.get("vehicleModelDate")),
        transmission=gearbox(transmission),
        fuel=fuel(
            item.get("fuelType") or engine.get("fuelType") or engine.get("engineType")
        ),
        vin=vin,
        kenteken=plate(item.get("licensePlate"))
        or plate_from_text(name + " " + description),
        seller=seller.get("name"),
        city=address.get("addressLocality"),
        description=description,
        images=image,
        raw=item,
    ).qualify()


def dutch_listing(source, item, base):
    auto = item["autogegevens"]
    general, history = auto.get("algemeen", {}), auto.get("geschiedenis", {})
    title = " ".join(
        str(general.get(x) or "") for x in ["merknaam", "modelnaam", "uitvoering"]
    )
    seller = item.get("aanbieder", {}).get("aanbiedergegevens", {})
    identity = str(item["advertentieId"])
    url = item.get("url") or (base.split("?")[0] + "#" + identity)
    photos = item.get("gpFotos", {})
    image = photos.get("fotoGroot") or photos.get("fotoKlein")
    links = [
        {"url": x["klikUrl"], "label": x.get("portaalBeschrijving", "portal")}
        for x in item.get("portalen", [])
        if x.get("klikUrl")
    ]
    if seller.get("website"):
        links.append({"url": seller["website"], "label": "dealer"})
    return Listing(
        source,
        identity,
        url,
        title,
        price=number(item.get("prijs", {}).get("totaalInclusiefBtw")),
        mileage=number(history.get("kilometerstand")),
        year=number(history.get("bouwjaar")),
        fuel=fuel(general.get("brandstofsoort")),
        transmission=gearbox(general.get("transmissietype")),
        kenteken=plate(item.get("kenteken")),
        seller=seller.get("naam") or seller.get("naamsvermelding"),
        city=seller.get("plaatsnaam"),
        images=[image] if image else [],
        links=links,
        raw=item,
    ).qualify()


def with_query(url, **kwargs):
    p = urlsplit(url)
    params = parse_qs(p.query)
    params.update({k: [str(v)] for k, v in kwargs.items()})
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(params, doseq=True), ""))


class _MarktplaatsDescription(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.parts = []
        self.blocks = 0
        self.closed_blocks = 0

    def handle_starttag(self, tag, attrs):
        if tag == "div" and (
            self.depth or dict(attrs).get("data-collapsable") == "description"
        ):
            if not self.depth:
                self.blocks += 1
            self.depth += 1
        if self.depth and tag in ("br", "p", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.depth and tag == "div":
            self.depth -= 1
            if not self.depth:
                self.closed_blocks += 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def _marktplaats_identity(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "marktplaats.nl", "www.marktplaats.nl"
    }:
        return None
    match = re.fullmatch(r"/v/auto-s/[^/]+/(m\d+)(?:-[^/]*)?/?", parsed.path)
    return match.group(1) if match else None


def marktplaats_detail(body, url, nodes):
    identity = _marktplaats_identity(url)
    if not identity:
        return None
    for _, text in re.findall(
        r"<script([^>]*)>(.*?)</script>", body, re.DOTALL | re.IGNORECASE
    ):
        match = re.search(r"window\.__CONFIG__\s*=\s*", text)
        if not match:
            continue
        try:
            config = json.JSONDecoder().raw_decode(text[match.end() :])[0]
        except ValueError:
            continue
        item = config.get("listing", {})
        if str(item.get("itemId")) != identity:
            continue
        attributes = {
            node["key"]: node.get("value")
            for node in walk(item.get("carAttributes", {}))
            if "key" in node and "value" in node
        }
        schemas = [
            node for node in nodes
            if node.get("offers")
            and isinstance(node.get("url"), str)
            and _marktplaats_identity(urljoin(url, node["url"])) == identity
        ]
        schema = schemas[0] if len(schemas) == 1 else {}
        description = _MarktplaatsDescription()
        description.feed(body)
        full_description = "".join(description.parts).strip()
        detail_complete = bool(
            full_description and description.blocks == description.closed_blocks == 1
        )
        desc = full_description or schema.get("description")
        images = schema.get("image") or []
        images = images if isinstance(images, list) else [images]
        images = [
            image.get("url") if isinstance(image, dict) else image for image in images
        ]
        images = [image for image in images if isinstance(image, str) and image]
        info = item.get("priceInfo", {})
        price_cents = number(info.get("priceCents"))
        seller = item.get("seller") or {}
        return Listing(
            "marktplaats",
            identity,
            url,
            item.get("title") or schema.get("name") or "",
            price=price_cents / 100 if price_cents is not None else None,
            price_type=info.get("priceType", "unknown"),
            mileage=number(attributes.get("mileage")),
            year=number(attributes.get("constructionYear")),
            transmission=gearbox(attributes.get("transmission")),
            fuel=fuel(attributes.get("fuel")),
            kenteken=plate(item.get("licensePlate") or attributes.get("licensePlate")),
            seller=seller.get("name"),
            city=(seller.get("location") or {}).get("cityName"),
            description=desc,
            images=images,
            raw={**item, "detail_complete": detail_complete},
        ).qualify()
    return None


def autoscout_detail(source, detail, url):
    v = detail["vehicle"]
    make = v.get("make")
    make = make.get("name") if isinstance(make, dict) else make
    from .search_profile import is_configured

    make = (
        make
        if isinstance(make, str) and make.strip()
        else ("" if is_configured() else "BMW")
    )
    title = (
        make
        + " "
        + " ".join(
            str(v.get(k) or "")
            for k in ["motorTypeName", "variant", "modelVersionInput"]
        )
    )
    desc = html.unescape(re.sub("<[^>]*>", " ", detail.get("description") or ""))
    item = Listing(
        source,
        str(detail["id"]),
        url,
        title,
        price=number(
            (detail.get("price") or {}).get("priceRaw")
            or (detail.get("price") or {}).get("priceFormatted")
        ),
        year=number(str(v.get("firstRegistrationDateRaw", ""))[:4]),
        mileage=number(v.get("mileageInKmRaw")),
        transmission=gearbox(v.get("transmissionType")),
        fuel=fuel((v.get("fuelCategory") or {}).get("formatted")),
        kenteken=plate(v.get("licensePlate")),
        seller=(detail.get("seller") or {}).get("companyName"),
        city=(detail.get("location") or {}).get("city"),
        description=desc,
        images=detail.get("images") or [],
        raw=detail,
    ).qualify()
    if make.strip().upper() != "BMW" and not is_configured():
        item.scope = "excluded"
    return item


def parse_page(source, body, url):
    if source == "autotrack":
        from .autotrack_search import parse_native_search

        native = parse_native_search(body, url)
        if native is not None:
            return native
    if source == "nederlandmobiel" and "/tweedehands-auto/" in urlsplit(url).path:
        from .dom import parse_dom

        return parse_dom(source, body, url)
    if source == "trovit" and urlsplit(url).path.startswith("/listing/"):
        from .trovit_details import parse_trovit_detail

        return parse_trovit_detail(body, url) or Page([])
    if source == "autowereld" and urlsplit(url).path.endswith(
        ("/details.html", "/foto.html")
    ):
        from .autowereld_details import parse_autowereld_detail

        return parse_autowereld_detail(body, url) or Page([])
    from .quality_api_adapters import enrich_api_listing, parse_public_detail

    detail = parse_public_detail(source, body, url)
    if detail is not None:
        return Page([detail])
    if source == "autotrack" and urlsplit(url).path.startswith("/a/"):
        return Page([])
    if source == "autoweek" and re.search(
        r"/occasions/[^/]+/[^/]+/\d+(?:/|$)", urlsplit(url).path
    ):
        from .dom import parse_autoweek_detail

        return parse_autoweek_detail(body, url)
    if source == "carteam" and 'class="card__title-link"' in body:
        from .dom import parse_dom

        return parse_dom(source, body, url)
    if source == "autofirst" and body.lstrip().startswith("{"):
        data = json.loads(body)
        meta = data.get("meta", {})
        result = Page(
            [],
            total=meta.get("total"),
            pages=meta.get("last_page"),
            page=meta.get("current_page"),
        )
        for ad in data.get("data", []):
            match = re.search(r"/occasions/(\d+)-", ad["url"])
            item = Listing(
                source,
                match.group(1) if match else ad["uuid"],
                ad["url"],
                (ad.get("name") or "") + " " + (ad.get("version") or ""),
                price=number(ad.get("price")),
                mileage=number(ad.get("mileage")),
                year=number(ad.get("build_year")),
                fuel=fuel(ad.get("fuel_type")),
                transmission=gearbox(ad.get("version")),
                kenteken=plate(ad.get("license_plate")),
                seller=ad.get("seller", {}).get("title"),
                city=ad.get("seller", {}).get("city"),
                images=[ad["image"]] if ad.get("image") else [],
                raw=ad,
            ).qualify()
            if ad.get("is_sold"):
                item.scope = "excluded"
            result.listings.append(item)
        return result
    if source == "vakgarage":
        match = re.search(
            r'<script[^>]*id="vehicle-overview-bootstrap-[^"]+"[^>]*>(.*?)</script>',
            body,
            re.DOTALL,
        )
        if match:
            data = json.loads(match.group(1))
            paging = data.get("pages", {})
            result = Page(
                [],
                total=data.get("count"),
                pages=paging.get("total"),
                page=paging.get("current"),
            )
            for ad in data.get("items", []):
                attributes = {
                    k: v.get("value") for k, v in ad.get("attributes", {}).items()
                }
                match_id = re.search(r"(?:^|/)p/(\d+)/", ad["url"])
                result.listings.append(
                    Listing(
                        source,
                        match_id.group(1) if match_id else ad["id"],
                        urljoin(
                            urlsplit(url).scheme + "://" + urlsplit(url).netloc + "/",
                            ad["url"],
                        ),
                        " ".join(
                            str(ad.get(k) or "") for k in ("brand", "model", "type")
                        ),
                        price=number(ad.get("price")),
                        mileage=number(attributes.get("tellerstand")),
                        year=number(attributes.get("bouwjaar")),
                        transmission=gearbox(attributes.get("transmissie")),
                        fuel=fuel(attributes.get("brandstof")),
                        kenteken=plate(attributes.get("kenteken")),
                        seller=attributes.get("vestiging"),
                        city=attributes.get("plaats"),
                        images=[i["path"] for i in ad.get("images", [])],
                        raw=ad,
                    ).qualify()
                )
            return result
    if source == "marktnet" and body.lstrip("\ufeff \r\n\t").startswith(
        ("<?xml", "<rss", "<ad ")
    ):
        from .marktnet import parse_xml

        return parse_xml(body)
    if (source == "autokopen" and "/auto/detail/" not in urlsplit(url).path) or (
        source == "autoweek" and 'class="aw-tile' in body
    ):
        from .dom import parse_dom

        return parse_dom(source, body, url)
    if source == "autohero" and body.lstrip().startswith("{"):
        payload = json.loads(body)
        data = payload.get("data", {}).get("searchAdV9AdsV2")
        if not data:
            raise ValueError("Autohero inventory missing from API response")
        data = json.loads(data) if isinstance(data, str) else data
        total = data.get("total")
        result = Page(
            [],
            total=total,
            pages=math.ceil(total / 24) if total is not None else None,
            page=int(parse_qs(urlsplit(url).query).get("pageNumber", [1])[0]),
        )
        for ad in data.get("data", []):
            title = " ".join(
                str(ad.get(k) or "")
                for k in ("manufacturer", "model", "subType", "subTypeExtra")
            )
            price = ad.get("offerPrice") or {}
            ad_url = (
                f"https://www.autohero.com/nl/{ad['carUrlTitle']}/id/{ad['id']}/"
                if ad.get("carUrlTitle")
                else "https://www.autohero.com/nl/search/#" + ad["id"]
            )
            # Verified against UI cards on 06.10.2026: V40 Benzine; Micra Handgeschakeld; 3-serie Automaat.
            result.listings.append(
                Listing(
                    source,
                    ad["id"],
                    ad_url,
                    title,
                    price=price.get("amountMinorUnits", 0)
                    / price.get("conversionMajor", 100)
                    if price
                    else None,
                    mileage=(ad.get("mileage") or {}).get("distance"),
                    year=ad.get("firstRegistrationYear"),
                    fuel={1039: "petrol", 1044: "electric", 1046: "hybrid"}.get(
                        ad.get("fuelType")
                    ),
                    transmission={1138: "manual", 1139: "automatic"}.get(
                        ad.get("gearType")
                    ),
                    seller="Autohero",
                    images=[ad["ahMainImageUrl"]] if ad.get("ahMainImageUrl") else [],
                    raw=ad,
                ).qualify()
            )
        result.listings = [enrich_api_listing(item) for item in result.listings]
        return result
    if source == "anwb" and body.lstrip().startswith("{"):
        data = json.loads(body)
        result = Page([], total=data.get("total"), page=data.get("pageNumber"))
        result.pages = (
            math.ceil(result.total / max(1, data.get("pageSize", 30)))
            if result.total is not None
            else None
        )
        for ad in data.get("hits", []):
            general = ad.get("carData", {}).get("general", {})
            history = ad.get("carData", {}).get("history", {})
            seller = ad.get("seller", {}).get("sellerData", {})
            title = " ".join(
                str(general.get(k) or "")
                for k in ["brandName", "modelName", "editon", "commercialEdition"]
            )
            result.listings.append(
                Listing(
                    source,
                    str(ad["advertisementId"]),
                    ad["url"],
                    title,
                    price=number(ad.get("price", {}).get("totalIncludingTax")),
                    year=history.get("constructionYear"),
                    mileage=history.get("mileage"),
                    transmission=gearbox(general.get("transmissionType")),
                    fuel=fuel(general.get("fuelType")),
                    kenteken=plate(ad.get("licencePlate")),
                    seller=seller.get("name"),
                    city=seller.get("city"),
                    raw=ad,
                ).qualify()
            )
        result.listings = [enrich_api_listing(item) for item in result.listings]
        return result
    if source == "marktplaats" and body.lstrip().startswith("{"):
        state = json.loads(body)
        if state.get("hasErrors") or not isinstance(state.get("searchRequest"), dict):
            raise ValueError("Marktplaats search response unavailable")
        docs = [{"props": {"pageProps": {"searchRequestAndResponse": state}}}]
    else:
        docs = list(json_documents(body))
    if source in {"autoscout24", "autotrader"}:
        requested = urlsplit(url)
        own_uuid = re.search(
            r"(?:/|-)([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?$",
            requested.path,
            re.IGNORECASE,
        )
        if own_uuid:
            if requested.scheme not in {"http", "https"} or requested.hostname not in {
                source + ".nl",
                "www." + source + ".nl",
            }:
                return Page([])
            own_details = []
            for doc in docs:
                props = doc.get("props") if isinstance(doc, dict) else None
                state = props.get("pageProps") if isinstance(props, dict) else None
                detail = (
                    state.get("listingDetails") if isinstance(state, dict) else None
                )
                if isinstance(detail, dict):
                    own_details.append(detail)
            if len(own_details) != 1:
                return Page([])
            detail = own_details[0]
            if str(detail.get("id", "")).lower() != own_uuid.group(
                1
            ).lower() or not isinstance(detail.get("vehicle"), dict):
                return Page([])
            web_page = detail.get("webPage")
            if web_page:
                publisher_url = (
                    urlsplit(web_page) if isinstance(web_page, str) else None
                )
                publisher_id = (
                    re.search(
                        r"(?:/|-)([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?$",
                        publisher_url.path,
                        re.IGNORECASE,
                    )
                    if publisher_url
                    else None
                )
                if (
                    not publisher_url
                    or publisher_url.scheme not in {"http", "https"}
                    or publisher_url.hostname
                    not in {
                        "autoscout24.nl",
                        "www.autoscout24.nl",
                        "autotrader.nl",
                        "www.autotrader.nl",
                    }
                    or not publisher_id
                    or publisher_id.group(1).lower() != own_uuid.group(1).lower()
                ):
                    return Page([])
            return Page([autoscout_detail(source, detail, url)])
    nodes = [node for doc in docs for node in walk(doc)]
    links = list(
        dict.fromkeys(
            urljoin(url, html.unescape(x))
            for x in re.findall(r"href=[\"\']([^\"\']+)", body, re.IGNORECASE)
        )
    )
    result = Page([], links=links)
    items = {}

    def add(item):
        items[item.external_id] = enrich_api_listing(item)

    if source in ["autoscout24", "autotrader"]:
        for doc in docs:
            state = (
                doc.get("props", {}).get("pageProps", {})
                if isinstance(doc, dict)
                else {}
            )
            detail = state.get("listingDetails")
            if detail:
                add(autoscout_detail(source, detail, url))
            if "listings" not in state:
                continue
            result.search_filters = {
                k: state.get("pageQuery", {}).get(k)
                for k in ("cy", "mmmv", "cat", "atype")
                if k in state.get("pageQuery", {})
            }
            result.total, result.pages = (
                state.get("numberOfResults"),
                state.get("numberOfPages"),
            )
            result.page = int(state.get("pageQuery", {}).get("page", 1))
            for item in state["listings"]:
                v = item["vehicle"]
                from .search_profile import is_configured, normalized

                brand = normalized(v.get("make")) if is_configured() else "BMW"
                title = (
                    brand
                    + " "
                    + " ".join(
                        str(v.get(k) or "")
                        for k in ["motorTypeName", "variant", "modelVersionInput"]
                    )
                )
                year = next(
                    (
                        number(x["data"].split("/")[-1])
                        for x in item.get("vehicleDetails", [])
                        if x.get("ariaLabel") == "Bouwjaar"
                    ),
                    None,
                )
                ad = Listing(
                    source,
                    str(item["id"]),
                    urljoin(url, item["url"]),
                    title,
                    price=number(item.get("price", {}).get("priceRaw")),
                    year=year,
                    mileage=number(v.get("mileageInKm")),
                    transmission=gearbox(v.get("transmission")),
                    fuel=fuel(v.get("fuel")),
                    seller=item.get("seller", {}).get("companyName"),
                    city=item.get("location", {}).get("city"),
                    images=item.get("images", []),
                    raw=item,
                ).qualify()
                add(ad)
    elif source == "marktplaats":
        detail = marktplaats_detail(body, url, nodes)
        if urlsplit(url).path.startswith("/v/"):
            return Page([detail] if detail else [])
        for doc in docs:
            state = (
                doc.get("props", {})
                .get("pageProps", {})
                .get("searchRequestAndResponse", {})
                if isinstance(doc, dict)
                else {}
            )
            if "listings" not in state:
                continue
            result.total = state.get("totalResultCount")
            pagination = state.get("searchRequest", {}).get("pagination", {})
            page_size = pagination.get("limit", 30)
            page_cap = state.get("maxAllowedPageNumber")
            if type(page_size) is not int or page_size <= 0:
                raise ValueError("Invalid Marktplaats native page size")
            count_pages = (
                max(1, math.ceil(result.total / page_size))
                if type(result.total) is int and result.total >= 0
                else None
            )
            result.pages = (
                min(count_pages, page_cap)
                if count_pages is not None and type(page_cap) is int and page_cap > 0
                else count_pages
            )
            request = state.get("searchRequest", {})
            result.search_filters = {
                k: request[k]
                for k in (
                    "categories",
                    "searchQuery",
                    "attributesById",
                    "attributeRanges",
                )
                if k in request
            }
            result.search_filters.update(page_size=page_size, page_cap=page_cap)
            result.page = (
                int(pagination.get("offset", 0) / max(1, pagination.get("limit", 30)))
                + 1
            )
            for item in state["listings"]:
                attributes = {
                    x["key"]: x.get("value") for x in item.get("attributes", [])
                }
                desc = item.get("categorySpecificDescription") or item.get(
                    "description", ""
                )
                info = item.get("priceInfo", {})
                ad = Listing(
                    source,
                    str(item["itemId"]),
                    urljoin(url, item.get("vipUrl") or ""),
                    item["title"],
                    price=number(info.get("priceCents")) / 100
                    if info.get("priceCents")
                    else None,
                    price_type=info.get("priceType", "unknown"),
                    year=number(attributes.get("constructionYear")),
                    mileage=number(attributes.get("mileage")),
                    fuel=fuel(attributes.get("fuel")),
                    transmission=gearbox(attributes.get("transmission")),
                    kenteken=plate_from_text(desc),
                    seller=item.get("sellerInformation", {}).get("sellerName"),
                    city=item.get("location", {}).get("cityName"),
                    description=desc,
                    images=item.get("imageUrls", []),
                    raw=item,
                ).qualify()
                add(ad)
    elif source in ["gaspedaal", "autotrack"]:
        allowed = None
        for node in nodes:
            if node.get("@type") == "ItemList" and node.get("itemListElement"):
                allowed = (
                    {
                        str(x["item"].get("@id", "")).rsplit("#", 1)[-1]
                        for x in node["itemListElement"]
                    }
                    if source == "gaspedaal"
                    else None
                )
                for x in node["itemListElement"]:
                    ad = schema_listing(source, x["item"], url)
                    if source == "autotrack":
                        m = re.search(r"-(\d+)$", ad.url)
                        if m:
                            ad.external_id = m.group(1)
                    add(ad)
            if source == "gaspedaal" and isinstance(
                node.get("commonPortalURLProps"), dict
            ):
                p = node["commonPortalURLProps"]
                result.total, result.page = p.get("totalOccasions"), p.get("pageNumber")
                size = p.get("pageSize")
                result.pages = (
                    max(1, math.ceil(result.total / size))
                    if type(result.total) is int and result.total >= 0
                    and type(size) is int and size > 0
                    else None
                )
        for node in nodes:
            if "advertentieId" in node and "autogegevens" in node:
                if allowed is not None and str(node["advertentieId"]) not in allowed:
                    continue
                add(dutch_listing(source, node, url))
        if source == "autotrack":
            inventory = next(
                (
                    n
                    for n in nodes
                    if isinstance(n.get("hits"), list)
                    and "pageSize" in n
                    and "total" in n
                    and "pageNumber" in n
                ),
                None,
            )
            if inventory:
                result.total = inventory["total"]
                result.pages = math.ceil(result.total / max(1, inventory["pageSize"]))
                result.page = inventory["pageNumber"]
            title = re.search(
                r"<title[^>]*>(.*?)</title>", body, re.DOTALL | re.IGNORECASE
            )
            match = re.search(r"Bekijk\s+([\d.]+)", title.group(1)) if title else None
            if match and not inventory:
                result.total = int(match.group(1).replace(".", ""))
            pages = [
                int(x)
                for link in links
                for x in re.findall(r"[?&]pageNumber=(\d+)", link)
            ]
            if not inventory:
                result.pages = max(pages, default=1)
                result.page = int(
                    parse_qs(urlsplit(url).query).get("pageNumber", [1])[0]
                )
    elif source == "viabovag":
        for node in nodes:
            if (
                isinstance(node.get("searchResults"), dict)
                and "results" in node["searchResults"]
            ):
                result.total = node["searchResults"].get("count")
                result.pages = math.ceil(result.total / 24) if result.total else 1
                request = node.get("serverSearchRequest", {})
                m = re.search(r"/pagina-(\d+)", url)
                result.page = int(m.group(1)) if m else int(request.get("page", 1))
            if (
                "vehicle" in node
                and isinstance(node.get("vehicle"), dict)
                and "company" in node
                and node.get("url")
            ):
                v = node["vehicle"]
                add(
                    Listing(
                        source,
                        str(node.get("id") or node["url"]),
                        urljoin(url, node["url"]),
                        node.get("title", ""),
                        price=number(node.get("price")),
                        mileage=number(v.get("mileage")),
                        year=number(v.get("year")),
                        transmission=gearbox(v.get("transmissionType")),
                        fuel=fuel(v.get("fuelType")),
                        kenteken=plate(v.get("licensePlate")),
                        seller=node.get("company", {}).get("name"),
                        city=node.get("company", {}).get("city"),
                        images=[node.get("imageUrl")] if node.get("imageUrl") else [],
                        links=[
                            {"url": node["externalAdvertisementUrl"], "label": "dealer"}
                        ]
                        if node.get("externalAdvertisementUrl")
                        else [],
                        raw=node,
                    ).qualify()
                )
        # Counter located in srp state varies. Never assume completion from one page.
    else:
        for node in nodes:
            if node.get("@type") == "ItemList":
                for element in node.get("itemListElement", []):
                    item = element.get("item", {})
                    if isinstance(item, dict) and item.get("offers"):
                        add(schema_listing(source, item, url))
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            types = (
                [value for value in types if isinstance(value, str)]
                if isinstance(types, list)
                else []
            )
            if (
                any(x in types for x in ["Car", "Vehicle", "Product"])
                and node.get("offers")
                and node.get("name")
            ) and source != "autouncle":
                add(schema_listing(source, node, url))
        if source == "autouncle":
            title = re.search(
                r"<title[^>]*>(.*?)</title>", body, re.DOTALL | re.IGNORECASE
            )
            text = html.unescape(title.group(1)) if title else ""
            m = re.search(
                r"(?:van|Vergelijk prijzen van)\s+(\d+)\s*(?:occasions|\|)", text
            )
            if m:
                result.total = int(m.group(1))
                result.pages = math.ceil(result.total / 25)
            inventory = next(
                (
                    node
                    for node in nodes
                    if node.get("@type") == "ItemList"
                    and isinstance(node.get("numberOfItems"), int)
                    and isinstance(node.get("itemListElement"), list)
                    and isinstance(node.get("mainEntityOfPage"), dict)
                    and node["mainEntityOfPage"].get("url", "").split("?", 1)[0]
                    == url.split("?", 1)[0]
                ),
                None,
            )
            if inventory is not None:
                # 07.10.2026: own inventory/button count=270, stale SEO title=271.
                result.total = inventory["numberOfItems"]
                result.pages = math.ceil(result.total / 25)
            result.page = int(parse_qs(urlsplit(url).query).get("page", [1])[0])
    result.listings = list(items.values())
    if (
        source in ["autowereld", "nederlandmobiel", "trovit", "marktnet", "bmw"]
        and not result.listings
    ):
        from .dom import parse_dom

        result = parse_dom(source, body, url)
    return result
