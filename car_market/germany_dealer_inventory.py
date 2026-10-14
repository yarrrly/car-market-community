"""Autoland: public BMW 2er inventory, 08.10.2026, 52 IDs / 5 pages.

Contract: fixtures/germany_dealer_inventory/manifest.json. JSON-LD #position
is linked to the native DOM stock ID and VDP URI only within the same page.
The parent dispatcher handles crawling; import does not initiate network access.
"""

import math
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .db import now
from .dom import text as dom_text
from .germany_dealers import (
    _country,
    _money,
    _offers,
    _own_listing,
    _roots,
    _text,
    _types,
    _uri,
)
from .parse import Page, number

SOURCE = "dealer:autoland.de"
ROOT = "https://autoland.de/bmw/2er-reihe"
PAGE_SIZE = 12


def autoland_stock_id(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "autoland.de"
        or parsed.username
        or parsed.password
        or parsed.netloc != "autoland.de"
        or parsed.query
        or parsed.fragment
    ):
        return None
    match = re.fullmatch(r"/bmw/2er-reihe/[a-z0-9-]+/(\d+)/[^/]+/?", parsed.path)
    return match.group(1) if match else None


def dealer_inventory_page_url(url, page):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "autoland.de"
        or parsed.path.rstrip("/") != "/bmw/2er-reihe"
        or parsed.fragment
        or set(query) - {"page"}
        or (
            query.get("page")
            and (len(query["page"]) != 1 or not query["page"][0].isdigit())
        )
        or isinstance(page, bool)
        or not isinstance(page, int)
        or not 1 <= page <= 500
    ):
        raise ValueError("unsupported_or_filtered_dealer_inventory_url")
    return urlunsplit(
        parsed._replace(query="" if page == 1 else urlencode({"page": page}))
    )


def _integer(value):
    return int(value) if re.fullmatch(r"\d+", str(value)) else None


def _normal(value):
    """Compare decoded native text and DOM text without another HTML parse."""
    content = dom_text(value) if isinstance(value, Tag) else str(value or "")
    return " ".join(content.split())


def parse_dealer_inventory(body, url):
    """Country from item offers.seller.address; identity from the native card + schema."""
    page = Page([], search_filters={"country": "DE", "coverage_status": "unverified"})

    def reject(reason):
        page.listings = []
        page.links = []
        page.total = page.pages = page.page = None
        page.search_filters["parse_status"] = reason
        return page

    try:
        requested = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        dealer_inventory_page_url(url, requested)
    except (ValueError, TypeError):
        return reject("unsupported_inventory_url")
    soup = BeautifulSoup(body, "html.parser")
    head = soup.head
    canonical = head.select('link[rel~="canonical"][href]') if head else []
    if len(canonical) != 1 or _uri(canonical[0]["href"], url) != _uri(url, url):
        return reject("inventory_canonical_mismatch")
    controls = soup.select(
        ".pagination-next-vehicles[data-article-count][data-page][data-pages]"
    )
    forms = soup.select('form.ivl-search--form[action="/ivlsearch"]')
    lists = [x for x in _roots(soup) if "ItemList" in _types(x)]
    counters = soup.select(".ivl-search--number-count")
    pagination = soup.select("ul.pagination[data-page][data-pages]")
    if (
        len(controls) > 1
        or len(forms) != 1
        or len(lists) != 1
        or len(counters) != 1
        or len(pagination) != 1
    ):
        return reject("inventory_contract_missing_or_ambiguous")
    form = forms[0]
    total = _integer(counters[0].get_text(strip=True))
    pages, current = (
        _integer(pagination[0].get(k)) for k in ("data-pages", "data-page")
    )
    if controls and any(
        _integer(controls[0].get(k)) != v
        for k, v in (
            ("data-article-count", total),
            ("data-page", current),
            ("data-pages", pages),
        )
    ):
        return reject("inventory_counter_conflict")
    if (
        total is None
        or pages is None
        or current != requested
        or pages != max(1, math.ceil(total / PAGE_SIZE))
        or current > pages
    ):
        return reject("inventory_counter_or_page_mismatch")
    values = {}
    for field in form.select("input[name],select[name]"):
        if field.has_attr("disabled"):
            continue
        name = field["name"]
        if field.name == "select":
            selected = field.select("option[selected]")
            value = selected[0].get("value") if len(selected) == 1 else None
        elif field.get("type") in {"checkbox", "radio"} and not field.has_attr(
            "checked"
        ):
            continue
        else:
            value = field.get("value")
        if name in values:
            return reject("ambiguous_inventory_filters")
        values[name] = value
        if name.startswith(("min", "max")):
            slider = field.find_parent(attrs={"data-range-slider": True})
            boundary = "data-rangemin" if name.startswith("min") else "data-rangemax"
            cleaned = re.sub(r"[€.,\s]|PS|km", "", str(value))
            if not slider or cleaned != slider.get(boundary):
                return reject("narrow_inventory_range")
        elif (
            name not in {"limit", "page", "sorting", "isAvailable", "make", "model"}
            and value != "all"
        ):
            return reject("narrow_inventory_filter")
    expected = {
        "limit": "12",
        "page": str(current),
        "sorting": "ASC",
        "isAvailable": "0",
        "make": "473",
        "model": "480",
    }
    if any(values.get(k) != v for k, v in expected.items()):
        return reject("inventory_native_filter_mismatch")
    elements = lists[0].get("itemListElement")
    cards = soup.select(
        ".ivl-search--articles-container .ivl-search--article[data-article-id]"
    )
    count = min(PAGE_SIZE, max(0, total - (current - 1) * PAGE_SIZE))
    if (
        not isinstance(elements, list)
        or len(elements) != len(cards)
        or len(cards) != count
    ):
        return reject("inventory_schema_card_count_mismatch")
    discovered, held, unconfirmed, ids = [], [], [], set()
    for position, (element, card) in enumerate(zip(elements, cards), 1):
        item = element.get("item") if isinstance(element, dict) else None
        links = card.select("a.ivl-search--link-wrapper[href][title]")
        stock = card.get("data-article-id")
        if len(links) != 1 or not stock or stock in ids:
            return reject("inventory_duplicate_or_missing_native_identity")
        own_url = _uri(links[0]["href"], url)
        if (
            not own_url
            or autoland_stock_id(own_url) != stock
            or str(card.get("data-page")) != str(current)
        ):
            return reject("inventory_stock_uri_or_page_mismatch")
        ids.add(stock)
        discovered.append({"stock_id": stock, "url": own_url})
        if (
            not isinstance(item, dict)
            or not _types(item) & {"Car", "Vehicle"}
            or element.get("position") != position
            or item.get("url") != ROOT + "#" + str(position)
            or _normal(item.get("name")) != _normal(links[0]["title"])
            or _normal(card.select_one("h2"))
            != _normal("BMW " + str(item.get("model", "")))
            or _normal(card.select_one("h3"))
            != _normal(item.get("vehicleConfiguration"))
        ):
            held.append(
                {"stock_id": stock, "reason": "schema_native_card_binding_failed"}
            )
            continue
        offers = _offers(item, lambda x: x)
        sellers = [o.get("seller") for o in offers]
        addresses = [s.get("address") if isinstance(s, dict) else None for s in sellers]
        if not addresses or any(
            not isinstance(a, dict) or _country(a.get("addressCountry")) != "DE"
            for a in addresses
        ):
            held.append({"stock_id": stock, "reason": "own_seller_country_not_DE"})
            continue
        if any(
            _normal(s.get("name"))
            != _normal("Autoland Niederlassung " + card.get("data-location", ""))
            for s in sellers
        ):
            held.append(
                {"stock_id": stock, "reason": "own_seller_card_location_mismatch"}
            )
            continue
        native_fields = [_normal(x) for x in card.select(".tile-data-value")]
        mileage = item.get("mileageFromOdometer") or {}
        if not isinstance(mileage, dict):
            held.append({"stock_id": stock, "reason": "own_mileage_shape_invalid"})
            continue
        registration = str(item.get("dateVehicleFirstRegistered", ""))
        registration_label = "EZ " + registration[5:7] + "/" + registration[:4]
        km = [v for v in native_fields if v.endswith(" km")]
        if (
            registration_label not in native_fields
            or len(km) != 1
            or number(km[0]) != number(mileage.get("value"))
            or _normal(item.get("vehicleTransmission")) not in native_fields
        ):
            held.append(
                {
                    "stock_id": stock,
                    "reason": "own_card_registration_mileage_gear_mismatch",
                }
            )
            continue
        price, _ = _money(offers)
        native_price = card.select_one('meta[itemprop="price"][content]')
        if native_price is None or price != number(native_price["content"]):
            held.append({"stock_id": stock, "reason": "own_price_binding_failed"})
            continue
        listing = _own_listing(item, offers, sellers, addresses, own_url, SOURCE)
        if listing is None:
            held.append({"stock_id": stock, "reason": "conflicting_own_vin"})
            continue
        listing.raw.update(detail_complete=False, dealer_stock_id=stock)
        listing.raw["own_identifiers"]["native_article_id"] = stock
        listing.raw["identity_evidence"].update(
            kind="own_schema_position_and_native_card",
            inventory_url=url,
            inventory_page=current,
            position=position,
            native_article_id=stock,
            native_title=links[0]["title"],
        )
        if listing.scope == "needs_confirmation" or (
            listing.fuel == "petrol"
            and re.search(
                r"\b218\s*i(?:A)?\b",
                " ".join(
                    str(item.get(k, ""))
                    for k in ("name", "model", "vehicleConfiguration")
                ),
                re.IGNORECASE,
            )
            and not re.search(
                r"active\s*tour|gran\s*(?:tour|coup)",
                " ".join(
                    str(item.get(k, ""))
                    for k in ("name", "model", "vehicleConfiguration")
                ),
                re.IGNORECASE,
            )
        ):
            unconfirmed.append(
                {
                    "stock_id": stock,
                    "url": own_url,
                    "reason": listing.raw.get("model_variant_status")
                    or "own_body_not_proven",
                }
            )
            listing.raw["model_identity_status"] = "needs_own_detail_confirmation"
        page.listings.append(listing)
    page.links = [row["url"] for row in discovered]
    page.total, page.pages, page.page = total, pages, current
    page.search_filters.update(
        parse_status="parsed_own_inventory",
        coverage_status="page_only",
        native_filters=expected,
        own_country_required=True,
        discovered=discovered,
        held=held,
        model_unconfirmed=unconfirmed,
    )
    return page


def collect_dealer_inventory(collector, run, query):
    """Bounded crawl; complete only with a stable, complete set of stock IDs."""
    if collector.country != "DE" or query.get("source") != SOURCE:
        raise ValueError("dealer_inventory_requires_own_DE_collector")
    root = dealer_inventory_page_url(query["url"], 1)
    limit = query.get("max_pages", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("invalid_dealer_inventory_max_pages")
    key, started = query["key"], now()
    errors, ids, targets, held, fingerprints = [], set(), set(), [], {}
    model_unconfirmed = {}
    expected, pages, completed_pages = None, None, 0

    def fetch(url, refresh=False):
        response = collector.fetcher.get(url, refresh=refresh)
        if response.status != 200 or response.error or not response.body:
            collector.fetch_record(run, SOURCE, response)
            raise ValueError(response.error or "dealer_http_" + str(response.status))
        if _uri(response.final_url, url) != _uri(url, url):
            collector.fetch_record(run, SOURCE, response)
            raise ValueError("dealer_inventory_redirect")
        page = parse_dealer_inventory(response.body, response.final_url)
        if page.search_filters.get("parse_status") != "parsed_own_inventory":
            collector.fetch_record(run, SOURCE, response)
            raise ValueError(
                page.search_filters.get("parse_status", "inventory_unparsed")
            )
        # Normal ingest retains raw data and country-gated observations in the same database.
        collector.ingest_response(run, SOURCE, key, response, queue=True)
        return page

    try:
        first = fetch(root)
        expected, pages = first.total, first.pages
        for number_ in range(1, min(pages, limit) + 1):
            page = (
                first
                if number_ == 1
                else fetch(dealer_inventory_page_url(root, number_))
            )
            current = {v["stock_id"] for v in page.search_filters["discovered"]}
            if page.total != expected or page.pages != pages or page.page != number_:
                raise ValueError("dealer_inventory_counter_changed")
            if current & ids:
                raise ValueError("dealer_inventory_repeated_stock_ids")
            ids.update(current)
            targets.update(x.external_id for x in page.listings if x.scope == "target")
            held.extend(page.search_filters["held"])
            model_unconfirmed.update(
                (v["stock_id"], v) for v in page.search_filters["model_unconfirmed"]
            )
            fingerprints[number_] = current
            completed_pages += 1
        if pages > limit:
            errors.append("dealer_inventory_page_budget")
        else:
            after = fetch(root, refresh=True)
            if after.search_filters["held"]:
                errors.append("dealer_inventory_refresh_held_own_proofs")
            if (
                after.total != expected
                or after.pages != pages
                or {v["stock_id"] for v in after.search_filters["discovered"]}
                != fingerprints[1]
            ):
                errors.append("dealer_inventory_root_changed")
        if len(ids) != expected:
            errors.append("dealer_inventory_count_mismatch")
        if held:
            errors.append("dealer_inventory_held_own_proofs")
    except Exception as error:  # noqa: BLE001 — retain the source failure and continue with other sources
        errors.append(str(error))
    enumeration_complete = (
        expected is not None
        and len(ids) == expected
        and completed_pages == pages
        and not errors
    )
    if model_unconfirmed:
        errors.append("dealer_inventory_unconfirmed_target_models")
    result = {
        "source": SOURCE,
        "query": key,
        "expected": expected,
        "received": len(ids),
        "targets": len(targets),
        "pages": completed_pages,
        "held": held,
        "model_unconfirmed": list(model_unconfirmed.values()),
        "enumeration_status": "complete" if enumeration_complete else "partial",
        "status": "partial" if errors else "complete",
        "coverage_scope": "own_dealer_bmw_2er_native_inventory_only",
        "errors": errors,
    }
    collector.db.finish_source(
        run,
        SOURCE,
        key,
        started,
        result["status"],
        expected,
        len(ids),
        len(targets),
        completed_pages,
        "; ".join(errors) or None,
        infer_missing=False,
    )
    return result


WIVEDA_SOURCE = "dealer:wiveda.de"
WIVEDA_ROOT = "https://wiveda.de/gebrauchtwagen/bmw/218-active-tourer"
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"


def _devalue(payload):
    """Public Nuxt/devalue payload; references are not literal numbers."""
    if not isinstance(payload, list) or not 1 <= len(payload) <= 50000:
        raise ValueError("invalid_native_payload")
    active, cache = set(), {}

    def decode(index, depth=0):
        if index == -1:
            return None
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < len(payload)
            or depth > 60
        ):
            raise ValueError("invalid_native_reference")
        if index in cache:
            return cache[index]
        if index in active:
            raise ValueError("cyclic_native_reference")
        active.add(index)
        value = payload[index]
        if isinstance(value, dict):
            value = {k: decode(v, depth + 1) for k, v in value.items()}
        elif isinstance(value, list):
            if (
                len(value) == 2
                and isinstance(value[0], str)
                and value[0] in {"Reactive", "ShallowReactive", "Ref", "ShallowRef"}
            ):
                value = decode(value[1], depth + 1)
            else:
                value = [decode(v, depth + 1) for v in value]
        cache[index] = value
        active.remove(index)
        return value

    value = decode(0)
    if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
        raise TypeError("native_data_missing")
    return value["data"]


def wiveda_page_url(url, page):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "wiveda.de"
        or parsed.path != "/gebrauchtwagen/bmw/218-active-tourer"
        or parsed.fragment
        or set(query) - {"page"}
        or (
            "page" in query
            and (len(query["page"]) != 1 or not query["page"][0].isdigit())
        )
        or isinstance(page, bool)
        or not isinstance(page, int)
        or not 1 <= page <= 500
    ):
        raise ValueError("unsupported_wiveda_inventory_url")
    return urlunsplit(
        parsed._replace(query="" if page == 1 else urlencode({"page": page}))
    )


def wiveda_payload_url(html, url):
    """Use only data-src from the source SSR; do not guess build keys or routes."""
    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.select("script#__NUXT_DATA__[data-src]")
    if len(nodes) != 1:
        raise ValueError("own_payload_link_missing")
    payload_url = _uri(nodes[0]["data-src"], url)
    if not payload_url:
        raise ValueError("invalid_payload_uri")
    parsed, owner = urlsplit(payload_url), urlsplit(url)
    query, own_query = parse_qs(parsed.query), parse_qs(owner.query)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "wiveda.de"
        or parsed.path != owner.path.rstrip("/") + "/_payload.json"
        or set(query) - {"_b", "page"}
        or query.get("page", ["1"]) != own_query.get("page", ["1"])
        or len(query.get("_b", [])) != 1
        or not re.fullmatch(UUID, query["_b"][0])
        or (
            owner.path != "/gebrauchtwagen/bmw/218-active-tourer"
            and not re.fullmatch(r"/fahrzeug/" + UUID, owner.path)
        )
    ):
        raise ValueError("payload_owner_or_build_mismatch")
    return payload_url


def _wiveda_listing(record, url, payload_url, schema=None):
    from .germany import qualify_german_item
    from .identity import normalized_vin
    from .parse import Listing, fuel, gearbox

    if _country(record.get("country")) != "DE":
        raise ValueError("own_vehicle_country_not_DE")
    identifier = record.get("id")
    if (
        not isinstance(identifier, str)
        or not re.fullmatch(UUID, identifier)
        or url != "https://wiveda.de/fahrzeug/" + identifier
    ):
        raise ValueError("own_native_id_uri_mismatch")
    dealer = record.get("dealer") or {}
    if not isinstance(dealer, dict):
        raise TypeError("invalid_own_dealer")
    description = (
        record.get("description")
        if isinstance(record.get("description"), str)
        else None
    )
    vins = {
        v
        for value in [
            record.get("vin"),
            *(
                re.findall(
                    r"\b(?:FIN|VIN|Fahrgestellnummer)\s*:\s*([A-HJ-NPR-Z0-9]{17})\b",
                    description or "",
                    re.IGNORECASE,
                )
            ),
        ]
        if (v := normalized_vin(value))
    }
    if len(vins) > 1:
        raise ValueError("conflicting_own_vin")
    cash = number(record.get("price"))
    currency = record.get("currency")
    if isinstance(cash, bool) or cash is None or cash < 0 or currency != "EUR":
        raise ValueError("own_cash_price_not_proven")
    if schema and _money(_offers(schema, lambda value: value)) != (
        float(cash),
        "advertised_price",
    ):
        raise ValueError("own_schema_price_conflict")
    native_year = record.get("year")
    year = (
        native_year
        if isinstance(native_year, int)
        and not isinstance(native_year, bool)
        and 1900 <= native_year <= 2100
        else None
    )
    month = record.get("registration_month")
    first_registration = (
        f"{year:04d}-{month:02d}"
        if year
        and isinstance(month, int)
        and not isinstance(month, bool)
        and 1 <= month <= 12
        else None
    )
    images = record.get("images") or []
    images = [x.get("url") if isinstance(x, dict) else x for x in images]
    raw = {
        "market_country": "DE",
        "market_context_country": "DE",
        "country_evidence": {
            "kind": "own_native_vehicle_country",
            "country": record["country"],
            "vehicle_id": identifier,
            "location": record.get("location"),
            "payload_url": payload_url,
        },
        "identity_evidence": {
            "kind": "own_UI_link_and_native_payload_ID",
            "native_vehicle_id": identifier,
            "canonical": url,
            "payload_url": payload_url,
        },
        "own_identifiers": {"native_vehicle_id": identifier},
        "dealer_stock_id": identifier,
        "model_brand": record.get("brand"),
        "model_family": record.get("model"),
        "model_variant": record.get("variant"),
        "native_vehicle": record,
        "price_currency": currency,
        "price_components": {
            "gross_purchase": cash,
            "net_price": record.get("net_price"),
            "vat_rate": record.get("vat_rate"),
        },
        "first_registration": first_registration,
        "condition": record.get("condition"),
        "detail_complete": schema is not None,
        "description_source": "own_native_vehicle",
        "availability_status": "available"
        if record.get("listing_status") == "active"
        else None,
    }
    if vins:
        raw["own_identifiers"]["vin"] = next(iter(vins))
    listing = Listing(
        WIVEDA_SOURCE,
        url,
        url,
        record.get("title")
        or " ".join(str(record.get(k, "")) for k in ["brand", "model"]),
        price=float(cash),
        mileage=int(record["mileage"])
        if isinstance(record.get("mileage"), (int, float))
        and not isinstance(record["mileage"], bool)
        and record["mileage"] >= 0
        else None,
        year=year,
        transmission=gearbox(record.get("transmission")),
        fuel=fuel(record.get("fuel_type")),
        vin=next(iter(vins), None),
        seller=dealer.get("name"),
        city=record.get("location"),
        description=description,
        images=[x for x in images if isinstance(x, str) and _uri(x, url)],
        raw=raw,
    )
    return qualify_german_item(listing)


def parse_wiveda_bundle(bundle, url):
    """Country belongs to the vehicle; do not transfer dealer HQ or map query values."""
    page = Page([], search_filters={"country": "DE", "coverage_status": "unverified"})
    try:
        if (
            not isinstance(bundle, dict)
            or bundle.get("kind") != "wiveda_native"
            or bundle.get("html_url") != url
        ):
            raise ValueError("invalid_wiveda_bundle_owner")
        html = bundle.get("html")
        if not isinstance(html, str):
            raise TypeError("own_html_missing")
        payload_url = wiveda_payload_url(html, url)
        if bundle.get("payload_url") != payload_url:
            raise ValueError("payload_uri_mismatch")
        soup = BeautifulSoup(html, "html.parser")
        canonical = soup.select('head link[rel~="canonical"][href]')
        if len(canonical) != 1 or _uri(canonical[0]["href"], url) != url:
            raise ValueError("own_canonical_mismatch")
        data = _devalue(bundle.get("payload"))
        detail = re.fullmatch(r"/fahrzeug/(" + UUID + ")", urlsplit(url).path)
        if detail:
            identifier = detail.group(1)
            record = data.get("vehicle-" + identifier)
            schemas = [x for x in _roots(soup) if _types(x) & {"Car", "Product"}]
            if not isinstance(record, dict) or len(schemas) != 1:
                raise ValueError("own_vehicle_missing_or_ambiguous")
            schema = schemas[0]
            subtitle = soup.select('[data-testid="contact-card-subtitle"]')
            native_dealer = soup.select('[data-testid="contact-card-dealer-link"]')
            variants = {_normal(record.get("variant"))}
            if isinstance(record.get("variant"), str):
                # The retained own UI renders the first three asterisk-separated segments.
                segments = [
                    part.strip()
                    for part in record["variant"].split("*")
                    if part.strip()
                ]
                if segments:
                    variants.add(_normal(" · ".join(segments[:3])))
            if (
                record.get("id") != identifier
                or schema.get("sku") != identifier
                or _uri(schema.get("url"), url) != url
                or not any(
                    _normal(x)
                    == _normal(record.get("title"))
                    == _normal(schema.get("name"))
                    for x in soup.select("h1")
                )
                or len(subtitle) != 1
                or _normal(subtitle[0]) not in variants
                or len(native_dealer) != 1
                or _uri(native_dealer[0].get("href"), url)
                != "https://wiveda.de/haendler/" + str(record.get("dealer_id"))
                or _normal(native_dealer[0])
                != _normal((record.get("dealer") or {}).get("name"))
                or schema.get("description") != record.get("description")
                or schema.get("mileageFromOdometer", {}).get("value")
                != record.get("mileage")
            ):
                raise ValueError("own_detail_native_binding_failed")
            page.listings = [_wiveda_listing(record, url, payload_url, schema)]
            page.total = page.pages = page.page = 1
            page.search_filters.update(
                parse_status="parsed_own_detail", coverage_status="detail_only"
            )
            return page
        number_ = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        wiveda_page_url(url, number_)
        state = data.get("used-car-model-cards-bmw-218-active-tourer-p" + str(number_))
        if not isinstance(state, dict):
            raise TypeError("own_inventory_key_missing")
        items = state.get("items")
        total, pages, size = (state.get(k) for k in ["total", "pages", "per_page"])
        if (
            not all(
                isinstance(v, int) and not isinstance(v, bool) and v >= 0
                for v in [total, pages]
            )
            or size != 12
            or pages != math.ceil(total / size)
            or state.get("page") != number_
        ):
            raise ValueError("own_inventory_counter_mismatch")
        cards = soup.select('[data-testid="vehicle-card"]')
        schemas = [v for v in _roots(soup) if "ItemList" in _types(v)]
        if (
            len(schemas) != 1
            or not isinstance(items, list)
            or len(cards) != len(items)
            or len(items) != min(size, total - (number_ - 1) * size)
        ):
            raise ValueError("own_inventory_card_count_mismatch")
        elements = schemas[0].get("itemListElement")
        if not isinstance(elements, list) or len(elements) != len(items):
            raise ValueError("own_inventory_schema_count_mismatch")
        discovered, held, seen = [], [], set()
        for position, (record, card, element) in enumerate(
            zip(items, cards, elements), 1
        ):
            identifier = record.get("id")
            own_url = "https://wiveda.de/fahrzeug/" + str(identifier)
            links = card.select('a[href^="/fahrzeug/"]')
            if (
                identifier in seen
                or not isinstance(identifier, str)
                or not re.fullmatch(UUID, identifier)
                or len(links) != 1
                or _uri(links[0]["href"], url) != own_url
                or element.get("url") != own_url
                or element.get("position") != (number_ - 1) * size + position
            ):
                raise ValueError("own_inventory_ID_binding_failed")
            seen.add(identifier)
            discovered.append({"stock_id": identifier, "url": own_url})
            title = card.select_one('[data-testid="vehicle-card-title"]')
            price = card.select_one('[data-testid="vehicle-card-price"]')
            dealer = card.select_one('[data-testid="dealer-name"]')
            location = card.select_one('[data-testid="dealer-location"]')
            if (
                _normal(title) != _normal(record.get("title"))
                or element.get("name") != record.get("title")
                or number(_text(price)) != record.get("price")
                or _normal(dealer) != _normal((record.get("dealer") or {}).get("name"))
                or _normal(location)
                != _normal(
                    (record.get("dealer") or {}).get("location")
                    or record.get("location")
                )
            ):
                held.append(
                    {"stock_id": identifier, "reason": "own_card_native_binding_failed"}
                )
                continue
            try:
                listing = _wiveda_listing(record, own_url, payload_url)
            except (ValueError, TypeError) as error:
                held.append({"stock_id": identifier, "reason": str(error)})
                continue
            listing.raw["identity_evidence"].update(
                inventory_url=url,
                inventory_page=number_,
                schema_position=element["position"],
            )
            page.listings.append(listing)
        page.total, page.pages, page.page = total, pages, number_
        page.links = [v["url"] for v in discovered]
        page.search_filters.update(
            parse_status="parsed_own_inventory",
            coverage_status="page_only",
            native_scope="used_218_active_tourer_landing",
            discovered=discovered,
            held=held,
        )
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        page.listings, page.links = [], []
        page.total = page.pages = page.page = None
        page.search_filters["parse_status"] = str(error)
    return page


def collect_wiveda_inventory(collector, run, query):
    """Public SSR and its data-src payload, without login or API keys."""
    import json

    from .bmw_dedicated import persist_frontier
    from .bmw_detail_policy import DetailDeferred, deferred_record
    from .fetch import Response

    if collector.country != "DE" or query.get("source") != WIVEDA_SOURCE:
        raise ValueError("wiveda_requires_own_DE_collector")
    root = wiveda_page_url(query["url"], 1)
    max_pages, max_details = query.get("max_pages", 3), query.get("max_details", 2)
    if (
        any(
            isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 50
            for v in [max_pages, max_details]
        )
        or not max_pages
    ):
        raise ValueError("invalid_wiveda_budget")
    key, started = query["key"], now()
    errors, ids, country_ids, target_ids, pending_ids, held = (
        [],
        set(),
        set(),
        set(),
        set(),
        [],
    )
    details, pages, expected, root_ids, completed, requests = 0, None, None, None, 0, 0
    items, census, detail_records = {}, {}, []
    census_complete = False
    policy = collector.specialized_detail_policy(run)
    detail_requests = cache_hits = 0

    def persist():
        persist_frontier(
            collector,
            key,
            {
                "source": WIVEDA_SOURCE,
                "expected": expected,
                "census": list(census.values()),
                "census_complete": census_complete,
                "details": detail_records,
            },
        )

    def get(url, refresh=False):
        nonlocal requests
        requests += 1
        response = collector.fetcher.get(url, refresh=refresh)
        collector.fetch_record(run, WIVEDA_SOURCE, response)
        if response.status != 200 or response.error or not response.body:
            raise ValueError(response.error or "wiveda_http_" + str(response.status))
        if _uri(response.final_url, url) != _uri(url, url):
            raise ValueError("wiveda_own_route_redirect")
        return response

    def fetch(url, refresh=False):
        html = get(url, refresh=refresh)
        native = get(wiveda_payload_url(html.body, html.final_url), refresh=refresh)
        payload = json.loads(native.body)
        bundle = {
            "kind": "wiveda_native",
            "html_url": html.final_url,
            "html": html.body,
            "payload_url": native.final_url,
            "payload": payload,
        }
        parsed = parse_wiveda_bundle(bundle, html.final_url)
        if parsed.search_filters.get("parse_status") not in {
            "parsed_own_detail",
            "parsed_own_inventory",
        }:
            raise ValueError(
                parsed.search_filters.get("parse_status", "native_unparsed")
            )
        response = Response(
            html.url,
            html.final_url,
            200,
            json.dumps(bundle, ensure_ascii=False),
            native.timestamp,
            transport="ordinary_HTTP_own_HTML_payload_bundle",
        )
        collector.ingest_response(run, WIVEDA_SOURCE, key, response, queue=False)
        bundle_id = collector.db.conn.execute(
            "SELECT id FROM fetches WHERE run_id=? AND source=? AND url=? ORDER BY id DESC LIMIT 1",
            (run, WIVEDA_SOURCE, response.url),
        ).fetchone()[0]
        own = {item.raw["dealer_stock_id"]: item for item in parsed.listings}
        for record in parsed.search_filters.get("discovered", []):
            item = own.get(record["stock_id"])
            census[record["stock_id"]] = {
                **record,
                "search_observed_at": native.timestamp,
                "search_fetch_id": bundle_id,
                "scope": item.scope if item else "unverified",
                "detail_status": "pending"
                if item and item.scope != "excluded"
                else "held",
            }
        persist()
        return parsed

    def fetch_detail(item):
        from .bmw_dedicated import fetch_wiveda_detail

        return fetch_wiveda_detail(
            collector,
            run,
            item,
            key=key,
            query_remaining=max(0, max_details * 2 - detail_requests),
        )

    try:
        first = fetch(root)
        expected, pages = first.total, first.pages
        for number_ in range(1, min(pages, max_pages) + 1):
            page = first if number_ == 1 else fetch(wiveda_page_url(root, number_))
            current = {v["stock_id"] for v in page.search_filters["discovered"]}
            if page.total != expected or page.pages != pages or page.page != number_:
                raise ValueError("wiveda_counter_changed")
            if current & ids:
                raise ValueError("wiveda_repeated_page_IDs")
            if number_ == 1:
                root_ids = current
            ids.update(current)
            country_ids.update(v.raw["dealer_stock_id"] for v in page.listings)
            held.extend(page.search_filters["held"])
            for item in page.listings:
                items[item.raw["dealer_stock_id"]] = item
                if item.scope == "target":
                    target_ids.add(item.external_id)
                elif item.scope == "needs_confirmation":
                    pending_ids.add(item.external_id)
            completed += 1
        if pages > max_pages:
            errors.append("wiveda_page_budget")
        elif query.get("refresh_root", True) is False:
            errors.append("wiveda_live_root_control_not_captured")
        else:
            after = fetch(root, refresh=True)
            if (
                after.total != expected
                or after.pages != pages
                or {v["stock_id"] for v in after.search_filters["discovered"]}
                != root_ids
                or after.search_filters["held"]
            ):
                errors.append("wiveda_root_control_changed_or_held")
        if len(ids) != expected or len(country_ids) != expected:
            errors.append("wiveda_own_ID_country_count_mismatch")
        if held:
            errors.append("wiveda_held_own_proof")
        census_complete = (
            len(ids) == len(country_ids) == expected
            and completed == pages
            and not errors
        )
        persist()
        frontier = [v for v in items.values() if v.scope != "excluded"]
        # The native selection also covers manual transmission when present in inventory.
        manual = next((v for v in frontier if v.transmission == "manual"), None)
        samples = list(
            dict.fromkeys(
                [frontier[0].url, manual.url]
                if frontier and manual
                else [v.url for v in frontier]
            )
        )
        by_url = {v.url: v for v in frontier}
        samples.extend(v.url for v in frontier if v.url not in samples)
        for url in samples:
            item = by_url[url]
            record = {
                "id": item.external_id,
                "stock_id": item.raw["dealer_stock_id"],
                "url": item.url,
                "status": "pending",
                "search_fetch_id": census[item.raw["dealer_stock_id"]][
                    "search_fetch_id"
                ],
                "search_observed_at": census[item.raw["dealer_stock_id"]][
                    "search_observed_at"
                ],
            }
            detail_records.append(record)
            before = policy.used
            try:
                updated, proof = fetch_detail(item)
                record.update(
                    status="accepted",
                    fetch_id=proof.fetch_id,
                    observed_at=proof.response.timestamp,
                    cached=proof.cached,
                )
                cache_hits += int(proof.cached)
            except DetailDeferred as error:
                record.update(deferred_record(error, item.url))
                continue
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                record.update(reason=str(error))
                errors.append(str(error))
                continue
            finally:
                spent = policy.used - before
                detail_requests += spent
                requests += spent
                persist()
            items[updated.raw["dealer_stock_id"]] = updated
            target_ids.discard(updated.external_id)
            pending_ids.discard(updated.external_id)
            if updated.scope == "target":
                target_ids.add(updated.external_id)
            elif updated.scope == "needs_confirmation":
                pending_ids.add(updated.external_id)
            details += 1
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        errors.append(str(error))
    enumeration_status = (
        "complete"
        if expected is not None
        and len(ids) == len(country_ids) == expected
        and completed == pages
        and not errors
        else "partial"
    )
    if pending_ids:
        errors.append("wiveda_unconfirmed_engine_variants")
    result = {
        "source": WIVEDA_SOURCE,
        "query": key,
        "expected": expected,
        "received": len(ids),
        "own_DE_records": len(country_ids),
        "enumeration_status": enumeration_status,
        "targets": len(target_ids),
        "model_pending": len(pending_ids),
        "pages": completed,
        "details": details,
        "HTTP_requests": requests,
        "detail_HTTP_requests": detail_requests,
        "detail_cache_hits": cache_hits,
        "detail_deferred": [r for r in detail_records if r["status"] != "accepted"],
        "held": held,
        "detail_frontier": [
            v.url
            for v in items.values()
            if v.scope != "excluded" and not v.raw["detail_complete"]
        ],
        "status": "partial"
        if errors or any(r["status"] != "accepted" for r in detail_records)
        else "complete",
        "errors": errors,
        "coverage_scope": "public_used_218_active_tourer_landing_only",
        "coverage_limits": [
            "New/other condition inventory is outside this observed landing.",
            "Broad model contains diesel and unresolved engine variants.",
            "Detail/VIN enrichment remains bounded.",
        ],
    }
    collector.db.finish_source(
        run,
        WIVEDA_SOURCE,
        key,
        started,
        result["status"],
        expected,
        len(ids),
        len(target_ids),
        completed,
        "; ".join(errors) or None,
        infer_missing=False,
    )
    return result
