"""Public Mercedes DE contracts; actual captures from 08.10.2026.

The adapter retains all native records. The calling profile context qualifies
ONLY C205; search filters are not vehicle facts.
No network access, database writes or operational management here.
"""

import copy
import hashlib
import json
import math
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .identity import normalized_vin
from .parse import Listing, Page, fuel, gearbox, model_year, number

PROFILE = "mercedes_c180_c205"
AUTOHERO_API = (
    "https://www.autohero.com/v1/retail-customer-gateway/graphql/searchAdV9AdsV2"
)
PKW_SEARCH = (
    "https://www.pkw.de/api/v1/cars/search/basic?brand_model=49_522&source=autokatalog"
)
HOSTS = {
    "kleinanzeigen_de": {"www.kleinanzeigen.de", "kleinanzeigen.de"},
    "pkw_de": {"www.pkw.de", "suche.pkw.de"},
    "autohero_de": {"www.autohero.com"},
    "instamotion_de": {"www.instamotion.com"},
    "carwow_de": {"angebote.carwow.de"},
    "gebrauchtwagen_de": {"www.12gebrauchtwagen.de"},
    "quoka_de": {"www.quoka.de"},
    "markt_de": {"www.markt.de"},
    "dealer:wiveda.de": {"wiveda.de"},
    "dealer:wellergruppe.de": {"wellergruppe.de"},
    "dealer:autoland.de": {"autoland.de"},
}


def _public(source, url):
    try:
        p = urlsplit(url)
        return (
            p.scheme == "https"
            and p.hostname in HOSTS.get(source, set())
            and not p.username
            and not p.password
            and p.port in (None, 443)
            and not p.fragment
        )
    except (ValueError, TypeError):
        return False


def _int(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return int(n) if math.isfinite(n) and n >= 0 and n.is_integer() else None
    except (ValueError, TypeError):
        return None


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def _country(value):
    if isinstance(value, dict):
        value = value.get("identifier") or value.get("name")
    text = str(value or "").strip().upper()
    return {
        "D": "DE",
        "DEU": "DE",
        "GERMANY": "DE",
        "DEUTSCHLAND": "DE",
        "NEDERLAND": "NL",
        "NIEDERLANDE": "NL",
        "ÖSTERREICH": "AT",
    }.get(text, text or "UNKNOWN")


def _own_fields(
    item,
    own,
    *,
    body=None,
    registration=None,
    production=None,
    built_year=None,
    generation=None,
    dealer=None,
):
    """Normalize only native fields; do not infer facts from the seed URL."""
    item.scope = "unknown"
    raw = item.raw
    if raw.get("market_country") in {"UNKNOWN", "", None}:
        raw.pop("market_country", None)
    if raw.get("country_evidence"):
        raw["source_country_evidence"] = copy.deepcopy(raw["country_evidence"])
    if generation is None:
        classification = (
            own.get("classification")
            if isinstance(own.get("classification"), dict)
            else {}
        )
        generation = own.get("modelGeneration") or classification.get("modelGeneration")
    raw["model_profile"] = PROFILE
    raw["own_inventory_record"] = own
    raw["qualification_policy"] = "caller_profile_context_only"
    for key, value in {
        "body_type": body,
        "first_registration": registration,
        "production_date": production,
        "built_year": built_year,
        "generation": generation,
        "own_dealer_address": dealer,
    }.items():
        if value is not None:
            raw[key] = value
    raw["mercedes_own_fields"] = {
        "body": body,
        "first_registration": registration,
        "production_date": production,
        "model_year": own.get("modelYear")
        or own.get("model_year")
        or own.get("vehicleModelDate"),
        "built_year": built_year,
        "generation": generation,
        "dealer_address": dealer,
    }
    # Even native EZ>=2016 does not prove production year/generation.
    return item


def _pkw(body, url):
    from .germany_secondary import _pkw_item

    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        soup = BeautifulSoup(body, "html.parser")
        rows = [json.loads(n["car"]) for n in soup.select("car-card[car]")]
        doc = {"results": rows}
    rows = doc.get("results", [doc] if "id" in doc else [])
    items = []
    for own in rows:
        if not isinstance(own, dict):
            continue
        item = _pkw_item(own)
        if not item:
            continue
        if "results" not in doc and str(own.get("id")) not in urlsplit(url).path.split(
            "/"
        ):
            continue
        owner = own.get("owner") or {}
        items.append(
            _own_fields(
                item,
                own,
                body=(own.get("bodytype") or {}).get("name"),
                registration=own.get("initial_registration"),
                production=own.get("production_date"),
                built_year=own.get("built_year"),
                generation=own.get("generation") or own.get("model_series"),
                dealer={
                    k: owner.get(k)
                    for k in ["name", "country", "zip", "city", "street"]
                },
            )
        )
    total = doc.get("total") or {}
    return Page(
        items,
        _int(total.get("count")),
        _int(total.get("pages")),
        _int(doc.get("page")),
        search_filters={
            "brand_model": parse_qs(urlsplit(url).query).get("brand_model"),
            "country_policy": "own_location_country_required",
        },
    )


def _autohero(body, url):
    from .germany_secondary import _apollo, _autohero_item, _own_id

    root, request = _apollo(body), {}
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        doc = {}
    detail_id = _own_id(url, "autohero_de")
    if detail_id:
        own = root.get(
            f'getCarDetailsStoreAd({{"adId":"{detail_id}","locale":"de-DE"}})', {}
        )
        rows, detail = ([own] if own.get("adId") == detail_id else []), True
        payload = {}
    else:
        detail = False
        payload = (doc.get("data") or {}).get("searchAdV9AdsV2")
        if payload is None:
            searches = [
                (k, v) for k, v in root.items() if k.startswith("searchAdV9AdsV2:")
            ]
            if len(searches) == 1:
                key, payload = searches[0]
                request = json.loads(key.split(":", 1)[1]).get("search", {})
        payload = payload if isinstance(payload, dict) else {}
        rows = payload.get("data", []) if not doc.get("errors") else []
    items = []
    for own in rows:
        if not isinstance(own, dict):
            continue
        item = _autohero_item(own, {}, detail=detail)
        if item:
            items.append(
                _own_fields(
                    item,
                    own,
                    body=own.get("bodyType") or own.get("body"),
                    registration=own.get("registration"),
                    production=own.get("manufacturedAt"),
                    built_year=own.get("builtYear"),
                    generation=own.get("generation"),
                    dealer=own.get("branch") or own.get("esBranch"),
                )
            )
    total = _int(payload.get("total"))
    return Page(
        items,
        total,
        math.ceil(total / 24) if total else 1 if total == 0 else None,
        (_int(request.get("offset", 0)) or 0) // 24 + 1,
        search_filters={
            "own_search": request,
            "country_policy": "own_countryCode_required",
            "own_detail": detail,
        },
    )


def _klein(body, url):
    from .germany_kleinanzeigen import parse_kleinanzeigen

    page = parse_kleinanzeigen(body, url)
    for item in page.listings:
        own = item.raw.get("own_details") or item.raw.get("native_preview") or {}
        own = own if isinstance(own, dict) else {}
        _own_fields(
            item,
            own,
            body=own.get("Fahrzeugtyp"),
            registration=item.raw.get("first_registration"),
            production=own.get("Produktionsdatum") or own.get("Baujahr"),
            generation=own.get("Baureihe") or own.get("Generation"),
            dealer={"location": item.raw.get("own_location")},
        )
    return page


def _instamotion(body, url):
    from .germany_additional import _instamotion_item

    soup = BeautifulSoup(body, "html.parser")
    node = soup.select_one("script#__NEXT_DATA__")
    doc = json.loads(node.get_text()) if node else json.loads(body)
    props = (doc.get("props") or {}).get("pageProps") or doc.get("pageProps") or {}
    route = (props.get("routerState") or {}).get("query") or {}
    detail = re.fullmatch(r"/de/auto/(\d+)/([a-z0-9-]+)", urlsplit(url).path)
    if detail:
        own = props.get("carData") or {}
        rows = (
            [own]
            if str(own.get("id")) == detail[1] and own.get("slug") == detail[2]
            else []
        )
        result = {}
    else:
        result = props.get("searchResults") or {}
        rows = result.get("cars") or []
    items = []
    for own in rows:
        if not isinstance(own, dict):
            continue
        own_url = (
            f"https://www.instamotion.com/de/auto/{own.get('id')}/{own.get('slug')}"
        )
        item = _instamotion_item(own, own_url)
        if item:
            styles = [
                v
                for v in own.get("catalog_features", [])
                if v.get("const_key", "").startswith("CARSTYLE_")
            ]
            items.append(
                _own_fields(
                    item,
                    own,
                    body=styles[0].get("label") if len(styles) == 1 else None,
                    registration=own.get("registration_date"),
                    production=own.get("manufacture_date"),
                    generation=own.get("generation"),
                    dealer={
                        k: own.get(k)
                        for k in [
                            "location_country",
                            "location_city",
                            "location_post_code",
                            "location_street",
                            "seller",
                        ]
                    },
                )
            )
    total = _int(result.get("total"))
    current = _int(route.get("page", 1)) or 1
    links = [
        urljoin(url, a["href"])
        for a in soup.select("a[href]")
        if "page=" in a["href"] and "/de/autos?" in a["href"]
    ]
    return Page(
        items,
        total,
        math.ceil(total / 20) if total else 1 if total == 0 else None,
        current,
        sorted(set(links)),
        {
            "native_query": route,
            "own_detail": bool(detail),
            "country_policy": "own_location_country_required",
            "retained_ids": [str(v.get("id")) for v in rows],
            "id_binding_gap": len(rows) - len(items),
        },
    )


def _wiveda(body, url):
    from .germany_dealer_inventory import _devalue

    doc = json.loads(body)
    if isinstance(doc, dict) and "html" in doc and "payload" in doc:
        doc = doc["payload"]
    if isinstance(doc, list):
        doc = _devalue(doc)
    current = _int(parse_qs(urlsplit(url).query).get("page", [1])[0]) or 1
    result = doc.get("used-car-model-cards-mercedes-benz-c-180-p" + str(current), {})
    detail_id = re.fullmatch(
        r"/fahrzeug/([a-f0-9-]{36})(?:/_payload.json)?", urlsplit(url).path
    )
    if detail_id:
        rows = [
            v
            for v in doc.values()
            if isinstance(v, dict) and v.get("id") == detail_id[1]
        ]
    else:
        rows = result.get("items", [])
    items = []
    for own in rows:
        identity = own.get("id") if isinstance(own, dict) else None
        if not isinstance(identity, str) or not re.fullmatch(
            r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", identity
        ):
            continue
        dealer = own.get("dealer") or {}
        item = Listing(
            "dealer:wiveda.de",
            identity,
            "https://wiveda.de/fahrzeug/" + identity,
            " ".join(str(own.get(k) or "") for k in ["title", "variant"]).strip(),
            price=number(own.get("price")),
            mileage=_int(own.get("mileage")),
            year=model_year(own.get("year")),
            transmission=gearbox(own.get("transmission")),
            fuel=fuel(own.get("fuel_type")),
            vin=normalized_vin(own.get("vin")),
            city=own.get("location"),
            seller=dealer.get("name"),
            description=own.get("description"),
            raw={
                "market_country": _country(own.get("country")),
                "country_evidence": {
                    "path": "own_vehicle.country",
                    "value": own.get("country"),
                    "vehicle_id": identity,
                },
                "price_currency": own.get("currency"),
                "price_net_eur": own.get("net_price"),
                "availability_status": own.get("listing_status"),
            },
        )
        month = _int(own.get("registration_month"))
        registration = (
            f"{item.year}-{month:02d}" if item.year and month and month <= 12 else None
        )
        items.append(
            _own_fields(
                item,
                own,
                body=own.get("body_type"),
                registration=registration,
                generation=own.get("generation"),
                production=own.get("production_date"),
                dealer=dealer,
            )
        )
    return Page(
        items,
        _int(result.get("total")),
        _int(result.get("pages")),
        current,
        search_filters={
            "native_per_page": result.get("per_page"),
            "own_detail": bool(detail_id),
            "effective_conditions": result.get("kompass_fits"),
            "country_policy": "own_vehicle_country_required",
        },
    )


def _carwow(body, url):
    from .germany_additional import parse_additional

    page = parse_additional("carwow_de", body, url)
    soup = BeautifulSoup(body, "html.parser")
    card_ids = parse_qs(urlsplit(url).query).get("id", [])
    if urlsplit(url).path == "/stock_cars/deal-card" and len(card_ids) == 1:
        targets = {
            urljoin(url, a["href"])
            for a in soup.select("a[href]")
            if urlsplit(urljoin(url, a["href"])).path == "/deals/" + card_ids[0]
            and _public("carwow_de", urljoin(url, a["href"]))
        }
        if len(targets) == 1:
            page.search_filters["native_detail_url"] = next(iter(targets))
    node = soup.select_one("[data-stock-cars-v2--results-count-value]")
    if node:
        prefix = "data-stock-cars-v2--results-"
        page.search_filters["native_filters"] = {
            k.removeprefix(prefix): v
            for k, v in node.attrs.items()
            if k.startswith(prefix + "filter-")
        }
        per_page = _int(node.get(prefix + "per-page-value"))
        page.pages = (
            math.ceil(page.total / per_page)
            if page.total and per_page
            else 1
            if page.total == 0
            else None
        )
    for item in page.listings:
        own = (
            item.raw.get("own_inventory_record")
            or item.raw.get("own_detail_values")
            or {}
        )
        _own_fields(
            item,
            own,
            body=own.get("Karosserie") or own.get("vehicle_body_style"),
            registration=own.get("Erstzulassung"),
            production=own.get("production_date"),
            built_year=own.get("model_year"),
            generation=own.get("generation"),
        )
    return page


def _twelve(body, url):
    from .germany_discovery import _parse_twelve

    # The legacy route guard accepts /suchen; DOM parsing below is model independent.
    p = urlsplit(url)
    if p.path not in {"/auto/mercedes-benz/c-180", "/auto/mercedes-benz/c-180/coupe"}:
        return Page(
            [], search_filters={"parse_status": "unverified_twelve_mercedes_route"}
        )
    delegated = urlunsplit(p._replace(path="/suchen"))
    page = _parse_twelve(BeautifulSoup(body, "html.parser"), delegated)
    page.links = [
        urljoin(url, n["data-gtm-event-url-value"])
        for n in BeautifulSoup(body, "html.parser").select(
            ".pagy [data-gtm-event-url-value]"
        )
    ]
    for item in page.listings:
        _own_fields(item, {"card_text": item.raw.get("card_text")})
    return page


def _dealer(body, url, source):
    soup = BeautifulSoup(body, "html.parser")
    node = soup.select_one("script#__NEXT_DATA__")
    if source == "dealer:wellergruppe.de" and node:
        props = json.loads(node.get_text()).get("props", {}).get("pageProps", {})
        rows = props.get("ssrVehicles")
        if isinstance(rows, list):
            items = []
            for own in rows:
                identity = str(own.get("vehicleId") or "")
                own_url = "https://wellergruppe.de/fahrzeuge" + str(
                    own.get("url") or ""
                )
                if not identity.isdigit() or not urlsplit(own_url).path.endswith(
                    "-" + identity + "/"
                ):
                    continue
                values = {
                    x.get("label"): x.get("value") for x in own.get("features", [])
                }
                item = Listing(
                    source,
                    identity,
                    own_url,
                    " ".join(
                        str(own.get(k) or "") for k in ["displayName", "additionalName"]
                    ).strip(),
                    year=_registration_year(values.get("Erstzulassung")),
                    mileage=_int(number(values.get("Kilometerstand"))),
                    transmission=gearbox(values.get("Getriebe")),
                    fuel=fuel(values.get("Treibstoff")),
                    city=values.get("Standort"),
                    raw={
                        "market_country": "UNKNOWN",
                        "discovery_only": True,
                        "own_card_values": values,
                    },
                )
                items.append(
                    _own_fields(
                        item,
                        own,
                        body=values.get("Karosserie"),
                        registration=values.get("Erstzulassung"),
                    )
                )
            return Page(
                items,
                _int(props.get("ssrCount")),
                search_filters={
                    "native_filters": props.get("usedFilter"),
                    "country_policy": "own_detail_country_required",
                },
            )
    if (
        source == "dealer:autoland.de"
        and urlsplit(url).path.rstrip("/") == "/mercedes-benz/c-klasse"
    ):
        from .germany_dealers import _own_listing, _roots

        items = []
        for doc in _roots(soup):
            if doc.get("@type") != "ItemList":
                continue
            elements = doc.get("itemListElement", [])
            cards = soup.select(
                ".ivl-search--articles-container .ivl-search--article[data-article-id]"
            )
            if len(elements) != len(cards):
                continue
            for position, (element, card) in enumerate(zip(elements, cards), 1):
                own = element.get("item", {})
                links = card.select("a.ivl-search--link-wrapper[href][title]")
                if len(links) != 1 or element.get("position") != position:
                    continue
                own_url = urljoin(url, links[0]["href"])
                identity = re.fullmatch(
                    r"(?:/mercedes-benz/c-klasse/[^/]+/(\d+)/[^/]+|/detail/index/sArticle/(\d+))",
                    urlsplit(own_url).path,
                )
                if (
                    not identity
                    or (identity[1] or identity[2]) != card.get("data-article-id")
                    or not _public(source, own_url)
                    or own.get("url")
                    != "https://autoland.de/mercedes-benz/c-klasse#" + str(position)
                    or " ".join(str(own.get("name", "")).split())
                    != " ".join(links[0]["title"].split())
                    or _text(card.select_one("h2"))
                    != "Mercedes-Benz " + str(own.get("model", ""))
                    or _text(card.select_one("h3"))
                    != " ".join(str(own.get("vehicleConfiguration", "")).split())
                ):
                    continue
                offers = own.get("offers") or []
                offers = offers if isinstance(offers, list) else [offers]
                sellers = [
                    o.get("seller") for o in offers if isinstance(o.get("seller"), dict)
                ]
                addresses = [
                    v.get("address")
                    for v in sellers
                    if isinstance(v.get("address"), dict)
                ]
                if not addresses:
                    continue
                item = _own_listing(own, offers, sellers, addresses, own_url, source)
                if item:
                    item.external_id = identity[1] or identity[2]
                    item.year = model_year(
                        str(own.get("dateVehicleFirstRegistered", ""))
                    )
                    claims = {_country(a.get("addressCountry")) for a in addresses}
                    item.raw["market_country"] = (
                        next(iter(claims)) if len(claims) == 1 else "CONFLICT"
                    )
                    items.append(
                        _own_fields(
                            item,
                            own,
                            body=own.get("bodyType"),
                            registration=own.get("dateVehicleFirstRegistered"),
                            production=own.get("productionDate"),
                            built_year=own.get("vehicleModelDate"),
                            dealer=addresses,
                        )
                    )
        controls = soup.select_one("ul.pagination[data-pages][data-page]")
        counter = soup.select_one(".ivl-search--number-count")
        return Page(
            items,
            _int(_text(counter)),
            _int(controls.get("data-pages")) if controls else None,
            _int(controls.get("data-page")) if controls else None,
            search_filters={"country_policy": "own_offer_seller_address_required"},
        )
    # The existing Autoland VDP guard compares BMW stock URIs; bind the native
    # Mercedes URI through the same JSON-LD path without calling that guard.
    from .germany_dealers import _own_listing, _roots

    canonicals = soup.select('head link[rel~="canonical"][href]')
    if len(canonicals) != 1 or urlsplit(
        urljoin(url, canonicals[0]["href"])
    ).path.rstrip("/") != urlsplit(url).path.rstrip("/"):
        return Page([], search_filters={"parse_status": "own_canonical_mismatch"})
    roots = _roots(soup)
    if source == "dealer:wellergruppe.de":
        # Bind the split owned hero to its exact Next vehicle and primary Car root.
        native = props.get("vehicle") if node and isinstance(props, dict) else None
        if (
            len(soup.select("script#__NEXT_DATA__")) != 1
            or not isinstance(native, dict)
            or not _public(source, canonicals[0]["href"])
            or canonicals[0]["href"].rstrip("/") != url.rstrip("/")
        ):
            return Page(
                [], search_filters={"parse_status": "own_detail_native_binding_failed"}
            )
        identity = re.search(r"-([1-9]\d*)/?$", urlsplit(url).path)
        native_id = native.get("vehicleId")
        owned = [
            root
            for root in roots
            if root.get("@type") == "Car"
            and root.get("@id") == canonicals[0]["href"]
            and root.get("url") == canonicals[0]["href"]
        ]
        if (
            not identity
            or isinstance(native_id, bool)
            or not isinstance(native_id, int)
            or str(native_id) != identity[1]
            or len(owned) != 1
        ):
            return Page(
                [], search_filters={"parse_status": "own_detail_native_binding_failed"}
            )
        primary = owned[0]
        manufacturer = native.get("manufacturer")
        if not isinstance(manufacturer, dict):
            return Page(
                [], search_filters={"parse_status": "own_detail_native_binding_failed"}
            )
        headings = soup.select("h1")
        display = (
            headings[0].select(":scope > span.MuiTypography-h1")
            if len(headings) == 1
            else []
        )
        variant = headings[0].select(":scope > p") if len(headings) == 1 else []
        brand = primary.get("brand")
        mileage = primary.get("mileageFromOdometer")
        condition = native.get("condition")
        meta = native.get("meta")
        native_vin = normalized_vin(meta.get("vin")) if isinstance(meta, dict) else None
        schema_vin = normalized_vin(primary.get("vehicleIdentificationNumber"))
        if (
            len(display) != 1
            or len(variant) != 1
            or not manufacturer.get("displayName")
            or not manufacturer.get("name")
            or _text(display[0]) != manufacturer["displayName"]
            or _text(variant[0]) != manufacturer["name"]
            or primary.get("name") != manufacturer["name"]
            or not isinstance(brand, dict)
            or brand.get("name") != manufacturer.get("brand")
            or primary.get("model") != manufacturer.get("model")
            or not isinstance(mileage, dict)
            or not isinstance(condition, dict)
            or mileage.get("value") != condition.get("mileage")
            or (native_vin and schema_vin and native_vin != schema_vin)
        ):
            return Page(
                [], search_filters={"parse_status": "own_detail_native_binding_failed"}
            )
        roots = owned
    candidates = []
    for own in roots:
        types = own.get("@type")
        if not set(types if isinstance(types, list) else [types]) & {
            "Car",
            "Vehicle",
            "Product",
        }:
            continue
        offers = own.get("offers") or []
        offers = offers if isinstance(offers, list) else [offers]
        declared = [own.get("url"), *(o.get("url") for o in offers)]
        declared = [u for u in declared if u]
        if not declared or any(
            not _public(source, u)
            or urlsplit(u).path.rstrip("/") != urlsplit(url).path.rstrip("/")
            for u in declared
        ):
            continue
        if source != "dealer:wellergruppe.de" and not any(
            _text(h).casefold().startswith(str(own.get("name", "")).casefold())
            for h in soup.select("h1")
        ):
            continue
        sellers = [o.get("seller") for o in offers if isinstance(o.get("seller"), dict)]
        addresses = [
            s.get("address") for s in sellers if isinstance(s.get("address"), dict)
        ]
        if not addresses:
            continue
        item = _own_listing(own, offers, sellers, addresses, url, source)
        if item:
            item.year = model_year(str(own.get("dateVehicleFirstRegistered", "")))
            if source == "dealer:autoland.de":
                match = re.fullmatch(
                    r"(?:/mercedes-benz/c-klasse/[^/]+/(\d+)/[^/]+|/detail/index/sArticle/(\d+))",
                    urlsplit(url).path,
                )
                if match:
                    item.external_id = match[1] or match[2]
            elif source == "dealer:wellergruppe.de":
                match = re.search(r"-(\d+)/?$", urlsplit(url).path)
                if match:
                    item.external_id = match[1]
            claims = {_country(a.get("addressCountry")) for a in addresses}
            item.raw["market_country"] = (
                next(iter(claims)) if len(claims) == 1 else "CONFLICT"
            )
            candidates.append(
                _own_fields(
                    item,
                    own,
                    body=own.get("bodyType"),
                    registration=own.get("dateVehicleFirstRegistered"),
                    production=own.get("productionDate"),
                    built_year=own.get("vehicleModelDate"),
                    dealer=addresses,
                )
            )
    return Page(
        candidates if len(candidates) == 1 else [], search_filters={"own_detail": True}
    )


def _registration_year(text):
    match = re.fullmatch(r"\d{1,2}/((?:19|20)\d{2})", str(text or ""))
    return int(match[1]) if match else model_year(text)


def _markt(body, url):
    soup = BeautifulSoup(body, "html.parser")
    headings = [_text(v) for v in soup.select("h1")]
    empty = any(
        _text(v) == "Leider wurden keine Anzeigen gefunden." for v in soup.select("h2")
    )
    keywords = urlsplit(url).path.rstrip("/").split("/")[-1].replace("+", " ").lower()
    bound = (
        urlsplit(url).path.startswith("/fahrzeuge/autos/suche/")
        and "mercedes" in keywords
        and "180" in keywords
        and any(keywords in h.lower() for h in headings)
    )
    if empty and bound:
        return Page(
            [],
            0,
            1,
            1,
            search_filters={
                "native_empty_search": True,
                "own_heading": headings,
                "country": "UNKNOWN",
            },
        )
    return Page(
        [], search_filters={"parse_status": "markt_nonempty_contract_unverified"}
    )


def parse_mercedes_de_sources(source, body, url):
    """Unified parser callable for the parent; no network access during import."""
    if not _public(source, url):
        return Page([], search_filters={"parse_status": "unsupported_public_url"})
    parsers = {
        "pkw_de": _pkw,
        "autohero_de": _autohero,
        "kleinanzeigen_de": _klein,
        "instamotion_de": _instamotion,
        "dealer:wiveda.de": _wiveda,
        "carwow_de": _carwow,
        "gebrauchtwagen_de": _twelve,
        "markt_de": _markt,
    }
    if source in parsers:
        try:
            return parsers[source](body, url)
        except (ValueError, TypeError, KeyError, AttributeError):
            return Page([], search_filters={"parse_status": "invalid_native_payload"})
    if source in {"dealer:wellergruppe.de", "dealer:autoland.de"}:
        return _dealer(body, url, source)
    if source == "quoka_de":
        from .germany_discovery import parse_discovery

        page = parse_discovery(source, body, url)
        for item in page.listings:
            own = (
                item.raw.get("own_inventory_record")
                or item.raw.get("own_details")
                or {}
            )
            _own_fields(item, own)
        return page
    return Page([], search_filters={"parse_status": "contract_unverified"})


def mercedes_de_page_request(
    source, url, page=1, *, request_body=None, observed_links=None
):
    """Verified POST template / GET pagination; overall limit of 200 pages."""
    if (
        not isinstance(page, int)
        or isinstance(page, bool)
        or not 1 <= page <= 200
        or not _public(source, url)
    ):
        raise ValueError("invalid_mercedes_public_request")
    p = urlsplit(url)
    q = parse_qs(p.query, keep_blank_values=True)
    if source == "pkw_de":
        if p.path != "/api/v1/cars/search/basic" or q.get("brand_model") != ["49_522"]:
            raise ValueError("verified_pkw_c180_query_required")
        q["page"] = [str(page)]
        return {
            "method": "GET",
            "url": urlunsplit(p._replace(query=urlencode(q, doseq=True))),
            "body": None,
        }
    if source == "autohero_de":
        if (
            not isinstance(request_body, dict)
            or request_body.get("operationName") != "searchAdV9AdsV2"
        ):
            raise ValueError("observed_autohero_post_required")
        body = copy.deepcopy(request_body)
        search = body.get("variables", {}).get("search", {})
        terms = list(_filter_terms(search.get("filter")))
        if (
            not {
                ("countryCode", "eq", '"DE"'),
                ("make", "eq", '"570"'),
                ("model", "eq", '["C-Klasse"]'),
            }
            <= set(terms)
            or search.get("limit") != 24
        ):
            raise ValueError("verified_autohero_mercedes_filter_required")
        search["offset"] = (page - 1) * 24
        return {"method": "POST", "url": AUTOHERO_API, "body": body}
    if source == "kleinanzeigen_de":
        from .germany_kleinanzeigen import kleinanzeigen_page_url

        if (
            q.get("sortingField") != ["SORTING_DATE"]
            and "/sortierung:neuste/" not in p.path
        ):
            raise ValueError("kleinanzeigen_date_sort_required")
        return {"method": "GET", "url": kleinanzeigen_page_url(url, page), "body": None}
    if page == 1:
        return {"method": "GET", "url": url, "body": None}
    if source in {"dealer:wiveda.de", "dealer:autoland.de"}:
        roots = {
            "dealer:wiveda.de": "/gebrauchtwagen/mercedes-benz/c-180",
            "dealer:autoland.de": "/mercedes-benz/c-klasse",
        }
        if p.path != roots[source] or set(q) - {"page"}:
            raise ValueError("verified_dealer_inventory_route_required")
        q["page"] = [str(page)]
        return {
            "method": "GET",
            "url": urlunsplit(p._replace(query=urlencode(q, doseq=True))),
            "body": None,
        }
    candidates = [
        u
        for u in observed_links or []
        if _public(source, u)
        and parse_qs(urlsplit(u).query).get("pag" if source == "quoka_de" else "page")
        == [str(page)]
    ]
    if len(set(candidates)) != 1:
        raise ValueError("native_pagination_link_required")
    return {"method": "GET", "url": candidates[0], "body": None}


def _filter_terms(node):
    if not isinstance(node, dict):
        return
    if node.get("field") is not None:
        yield node["field"], node.get("op"), json.dumps(node.get("value"))
    elif node.get("op") == "and":
        for child in node.get("value", []):
            yield from _filter_terms(child)


def wiveda_mercedes_payload_url(body, url):
    """data-src from the native SSR; do not invent a build UUID."""
    if not _public("dealer:wiveda.de", url):
        raise ValueError("invalid_wiveda_public_url")
    nodes = BeautifulSoup(body, "html.parser").select("script#__NUXT_DATA__[data-src]")
    if len(nodes) != 1:
        raise ValueError("own_wiveda_payload_missing")
    target = urljoin(url, nodes[0]["data-src"])
    owner, parsed = urlsplit(url), urlsplit(target)
    q = parse_qs(parsed.query)
    if (
        not _public("dealer:wiveda.de", target)
        or parsed.path != owner.path.rstrip("/") + "/_payload.json"
        or q.get("page", ["1"]) != parse_qs(owner.query).get("page", ["1"])
        or set(q) - {"page", "_b"}
        or len(q.get("_b", [])) != 1
    ):
        raise ValueError("wiveda_payload_owner_mismatch")
    return target


def collect_mercedes_de_source(
    getter,
    query,
    *,
    max_pages=20,
    max_requests=300,
    max_details=0,
    detail_eligible=None,
    detail_getter=None,
):
    """Bounded read-only crawl; track census and publication details independently.

    Getter: Fetcher.get(url, refresh=False, request_body=None). The caller
    retains raw data, timestamps and fetch linkage; no database is opened here.
    """
    for value, limit in ((max_pages, 200), (max_requests, 10000)):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= limit
        ):
            raise ValueError("invalid_mercedes_collection_bound")
    if (
        isinstance(max_details, bool)
        or not isinstance(max_details, int)
        or not 0 <= max_details <= 10000
    ):
        raise ValueError("invalid_mercedes_detail_bound")
    if query.get("enabled") is False:
        return {"listings": [], "responses": [], "status": "disabled", "errors": []}
    source, url = query["source"], query["url"]
    responses, pages, items = [], [], {}
    organic, discovered, seen_pages, detailed = set(), set(), set(), set()
    attempted_details, cached_details, policy_budget_deferred = set(), set(), set()
    enumeration_errors, detail_errors = [], []
    expected, eof = None, False

    def get(target, body=None, *, original=None):
        if len(responses) >= max_requests:
            raise ValueError("request_limit_reached")
        if not _public(source, target):
            raise ValueError("unsupported_public_request_url")
        response = (
            detail_getter(target, original=original)
            if original is not None and detail_getter is not None
            else getter(
                target,
                refresh=original is None,
                **({"request_body": body} if body is not None else {}),
            )
        )
        responses.append(response)
        if (
            original is not None
            and not getattr(response, "secondary_cached_accepted", None)
            and not getattr(response, "secondary_route_evidence", False)
        ):
            attempted_details.add(original.external_id)
        if response.status != 200 or response.error or not response.body:
            raise ValueError(response.error or "http_" + str(response.status))
        return response

    def parse(response):
        return parse_mercedes_de_sources(source, response.body, response.final_url)

    def capture_origin(item, response):
        item.raw["mercedes_capture_origin"] = {
            "requested_uri": response.url,
            "final_uri": response.final_url,
            "fetched_at": response.timestamp,
            "content_hash": hashlib.sha256(response.body.encode("utf-8")).hexdigest(),
        }

    observed_links = []
    try:
        for current in range(1, max_pages + 1):
            request = mercedes_de_page_request(
                source,
                url,
                current,
                request_body=query.get("request_body"),
                observed_links=observed_links,
            )
            response = get(request["url"], request["body"])
            page = parse(response)
            if source == "dealer:wiveda.de":
                response = get(
                    wiveda_mercedes_payload_url(response.body, response.final_url)
                )
                page = parse(response)
            if source == "carwow_de" and page.search_filters.get("native_payload_url"):
                response = get(page.search_filters["native_payload_url"])
                page = parse(response)
            # Even a changed/incomplete response retains all observed cards.
            page_ids = {v.external_id for v in page.listings}
            for item in page.listings:
                discovered.add(item.external_id)
                if (
                    source == "kleinanzeigen_de"
                    and item.raw.get("promotion_verified")
                    and item.raw.get("placement") == "organic"
                ):
                    organic.add(item.external_id)
                capture_origin(item, response)
                items.setdefault(item.external_id, item)
            pages.append(
                {
                    "page": current,
                    "total": page.total,
                    "received": len(page.listings),
                    "filters": page.search_filters,
                }
            )
            if page.search_filters.get("parse_status") or page.search_filters.get(
                "id_binding_gap"
            ):
                raise ValueError(
                    page.search_filters.get("parse_status") or "native_id_binding_gap"
                )
            if (
                page.page is not None
                and page.page != current
                and source != "autohero_de"
            ):
                raise ValueError("native_page_mismatch")
            if current == 1:
                expected = page.total
            elif page.total is not None and page.total != expected:
                raise ValueError("native_counter_changed")
            signature = tuple(sorted(page_ids))
            if page_ids and signature in seen_pages:
                raise ValueError("repeated_native_page")
            seen_pages.add(signature)
            observed_links = sorted(set(observed_links + page.links))
            if page.pages is not None and current >= page.pages or page.total == 0:
                eof = True
                break
            if page.pages is None:
                raise ValueError("native_pagination_bound_unverified")
        else:
            enumeration_errors.append("page_limit_reached")
    except ValueError as error:
        enumeration_errors.append(str(error))

    supported_details = source in {
        "kleinanzeigen_de",
        "instamotion_de",
        "autohero_de",
        "pkw_de",
        "quoka_de",
        "carwow_de",
        "dealer:wiveda.de",
        "dealer:wellergruppe.de",
        "dealer:autoland.de",
    }
    blocked = any("403" in e or "429" in e for e in enumeration_errors)
    # Census keeps all own IDs; only the caller can exclude detail work for its profile.
    detail_candidates = [
        item
        for item in items.values()
        if query.get("enrich_excluded")
        or detail_eligible is None
        or detail_eligible(item)
    ]
    excluded_details = sorted(
        set(items) - {item.external_id for item in detail_candidates}
    )
    selected_details = set()
    if supported_details and not blocked:
        for item in detail_candidates[:max_details]:
            selected_details.add(item.external_id)
            try:
                detail_url = (
                    "https://www.pkw.de/api/v1/cars/" + item.external_id
                    if source == "pkw_de"
                    else item.url
                )
                response = get(detail_url, original=item)
                detail = parse(response)
                if source == "dealer:wiveda.de":
                    response = get(
                        wiveda_mercedes_payload_url(response.body, response.final_url),
                        original=item,
                    )
                    detail = parse(response)
                if source == "carwow_de" and detail.search_filters.get(
                    "native_detail_url"
                ):
                    response = get(
                        detail.search_filters["native_detail_url"], original=item
                    )
                    detail = parse(response)
                matches = [
                    v
                    for v in detail.listings
                    if v.external_id == item.external_id
                    or v.url.rstrip("/") == item.url.rstrip("/")
                ]
                if len(matches) == 1:
                    capture_origin(matches[0], response)
                    matches[0].raw["discovery_record"] = item.raw
                    accepted_cache = getattr(
                        response, "secondary_cached_accepted", None
                    )
                    if accepted_cache:
                        matches[0].raw["secondary_cached_accepted"] = accepted_cache
                        cached_details.add(item.external_id)
                    items[item.external_id] = matches[0]
                    detailed.add(item.external_id)
                else:
                    detail_errors.append("own_detail_unparsed:" + item.external_id)
            except ValueError as error:
                if str(error) == "detail_budget_deferred":
                    policy_budget_deferred.add(item.external_id)
                detail_errors.append(str(error) + ":" + item.external_id)
                if (
                    len(responses) >= max_requests
                    or "403" in str(error)
                    or "429" in str(error)
                ):
                    break
    census_ids = organic if source == "kleinanzeigen_de" else discovered
    census = (
        expected is not None
        and len(census_ids) == expected
        and eof
        and not enumeration_errors
    )
    relevant_ids = {item.external_id for item in detail_candidates}
    held_countries = [
        i.external_id
        for i in items.values()
        if i.external_id in relevant_ids and not i.raw.get("market_country")
    ]
    details_complete = (
        (
            not detail_candidates
            or supported_details
            and len(detailed) == len(detail_candidates)
        )
        and not detail_errors
        and not held_countries
    )
    return {
        "listings": list(items.values()),
        "responses": responses,
        "pages": pages,
        "expected": expected,
        "received": len(discovered),
        "organic_received": len(organic),
        "enumeration_status": "complete" if census else "partial",
        "detail_status": "complete" if details_complete else "partial",
        "details_expected": len(detail_candidates),
        "details_received": len(detailed),
        "details_excluded": excluded_details,
        **({"search_listings": detail_candidates} if detail_getter is not None else {}),
        "details_selected": sorted(selected_details),
        "details_attempted": sorted(attempted_details),
        "details_cached": sorted(cached_details),
        "details_deferred": sorted(relevant_ids - detailed),
        "details_budget_deferred": sorted(
            policy_budget_deferred
            | (
                {item.external_id for item in detail_candidates[max_details:]}
                if supported_details
                else set()
            )
        ),
        "country_held": held_countries,
        "status": "complete" if census and details_complete else "partial",
        "errors": enumeration_errors + detail_errors,
        "enumeration_errors": enumeration_errors,
        "detail_errors": detail_errors,
        "scope": "verified_query_only",
        "infer_missing": False,
    }
