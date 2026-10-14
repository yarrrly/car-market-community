"""Public Mobile.de HTML/Flight responses; network and browser startup stay outside the adapter.

SEO URLs ignore query filters (live response from 07.10.2026). Their count
does not prove national completeness; Page retains the effective filters.
"""

import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .germany_autoscout import _dict, _integer, _positive, _price_currency as _quote_currency
from .parse import Listing, Page, fuel, gearbox, json_documents, number, walk

SOURCE = "mobile_de"
HOSTS = {"mobile.de", "www.mobile.de", "suchen.mobile.de", "home.mobile.de", "m.mobile.de"}
DEALER_HOST = "fahrzeugmarkt.auto-straussberger.de"
DEALER_SOURCE = "dealer:" + DEALER_HOST
DEALER_SEARCH = "https://" + DEALER_HOST + "/suche/?fwp_klasse=pkw&_modell=218-active-tourer"


def _public_mobile(url):
    try:
        parts = urlsplit(url)
        return (
            parts.scheme in {"http", "https"}
            and parts.hostname in HOSTS
            and not parts.username
            and not parts.password
            and parts.port in (None, 443 if parts.scheme == "https" else 80)
        )
    except (ValueError, TypeError, AttributeError):
        return False


def mobile_ad_id(url):
    if not _public_mobile(url):
        return None
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    if parts.path in {"/fahrzeuge/details.html", "/home/vip", "/home/vip.html"}:
        ids = query.get("id", [])
        return ids[0] if len(ids) == 1 and re.fullmatch(r"[1-9]\d*", ids[0]) else None
    match = re.fullmatch(r"/auto-inserat/[^/]+/([1-9]\d*)\.html", parts.path)
    return match[1] if match else None


def mobile_detail_url(ad_id, *, customer_id=None):
    if not re.fullmatch(r"[1-9]\d*", str(ad_id)):
        raise ValueError("Invalid Mobile.de ad ID")
    if customer_id is not None:
        return "https://home.mobile.de/home/vip?" + urlencode(
            {"id": ad_id, "customerId": customer_id}
        )
    return "https://suchen.mobile.de/fahrzeuge/details.html?" + urlencode({"id": ad_id})


def mobile_page_url(url, page):
    """Primary/dealer pagination. SEO pages must use observed UI navigation."""
    if not _public_mobile(url):
        raise ValueError("Invalid Mobile.de public URL")
    parts = urlsplit(url)
    if parts.path not in {
        "/fahrzeuge/search.html",
        "/home/index.html",
    }:
        raise ValueError("Pagination not verified for this Mobile.de path")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValueError("Page must be a positive integer")
    query = parse_qs(parts.query, keep_blank_values=True)
    query["pageNumber"] = [str(page)]
    return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))


def mobile_search_url(*, page=1, registration=None, price=None, customer_id=None):
    """Plan the primary URL; omitting dam requires checking the effective response.

    Do not add default radius, transmission, price, fuel or condition restrictions.
    Primary availability and the meaning of 'all damaged' remain unconfirmed.
    """
    params = {"cn": "DE", "ms": "3500;107;;", "vc": "Car", "isSearchRequest": "true"}
    if registration is not None:
        params["fr"] = registration
    if price is not None:
        params["p"] = price
    base = "https://suchen.mobile.de/fahrzeuge/search.html"
    if customer_id is not None:
        base = "https://home.mobile.de/home/index.html"
        params["customerId"] = str(customer_id)
    return mobile_page_url(base + "?" + urlencode(params), page)


def _flight(body):
    """Decode Flight records including byte-length T text, without JS execution."""
    chunks = []
    for match in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)</script>", body, re.DOTALL):
        try:
            part = json.loads(match[1])
            if len(part) > 1 and isinstance(part[1], str):
                chunks.append(part[1])
        except (ValueError, TypeError):
            continue
    data = "".join(chunks).encode("utf-8")
    records = {}
    pos = 0
    while pos < len(data):
        if data[pos : pos + 1] == b"\n":
            pos += 1
            continue
        if data[pos : pos + 1] == b":":
            end = data.find(b"\n", pos)
            pos = len(data) if end < 0 else end + 1
            continue
        match = re.match(rb"([0-9a-f]+):", data[pos:])
        if not match:
            break
        key = match[1].decode()
        pos += match.end()
        size = re.match(rb"T([0-9a-f]+),", data[pos:])
        if size:
            pos += size.end()
            length = int(size[1], 16)
            if pos + length > len(data):
                break
            records[key] = data[pos : pos + length].decode("utf-8", errors="replace")
            pos += length
            continue
        end = data.find(b"\n", pos)
        end = len(data) if end < 0 else end
        try:
            records[key] = json.loads(data[pos:end])
        except ValueError:
            pass
        pos = end + 1

    def resolve(value, visited=frozenset(), depth=0):
        if depth > 30:
            return None
        if isinstance(value, str) and re.fullmatch(r"\$[0-9a-f]+(?::[^\s]+)*", value):
            ref, *path = value[1:].split(":")
            if ref not in records or ref in visited:
                return None
            target = records[ref]
            try:
                for component in path:
                    target = (
                        target[int(component)] if isinstance(target, list) else target[component]
                    )
            except (KeyError, IndexError, TypeError, ValueError):
                return None
            return resolve(target, visited | {ref}, depth + 1)
        if isinstance(value, dict):
            return {k: resolve(v, visited, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v, visited, depth + 1) for v in value]
        return value

    return [resolve(value, frozenset({key})) for key, value in records.items()]


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def _country(value):
    return (
        "DE" if str(value or "").strip().upper() in {"DE", "D", "DEUTSCHLAND", "GERMANY"} else None
    )


def _date(value):
    value = str(value or "")
    match = re.fullmatch(r"(0[1-9]|1[0-2])/(\d{4})", value)
    if match:
        return f"{match[2]}-{match[1]}"
    match = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])(?:-\d{2})?", value)
    return match[0] if match else None


def _image_url(value):
    if not isinstance(value, str) or not value or value.startswith("$"):
        return None
    if value.startswith("img.classistatic.de/"):
        value = "https://" + value
    parts = urlsplit(value)
    return (
        value
        if parts.scheme in {"http", "https"} and parts.hostname == "img.classistatic.de"
        else None
    )


def _price_currency(item, card=None):
    prices = _dict(item.get("price"))
    # The gross quote and the fallback net quote each require their own currency.
    gross, net = _dict(prices.get("grs")), _dict(prices.get("nt"))
    chosen = (
        net
        if _positive(gross.get("amount")) is None and _positive(net.get("amount")) is not None
        else gross or net
    )
    currency = chosen.get("currency")
    if isinstance(currency, str) and currency.strip():
        return currency.strip().upper()
    # Only an ID-bound own card with the same quote can supply its currency.
    labels = card.select('[data-testid="main-price-label"]') if card else []
    amount = _positive(chosen.get("amount"))
    if len(labels) == 1 and amount is not None and _positive(_text(labels[0])) == amount:
        return _quote_currency({"price": {"priceFormatted": _text(labels[0])}})
    return "UNKNOWN"


def _price(item, card=None):
    prices = item.get("price") or {}
    if not isinstance(prices, dict):
        return None, "unknown"
    gross = _dict(prices.get("grs"))
    net = _dict(prices.get("nt"))
    kind = str(prices.get("type", "")).upper()
    if any(x in kind for x in ["LEAS", "MONTH", "RATE"]):
        return None, "monthly_payment"
    if kind in {"ON_REQUEST", "NEGOTIABLE_WITHOUT_PRICE"}:
        return None, "on_request"
    currency = _price_currency(item, card)
    if currency not in {"EUR", "UNKNOWN"}:
        return None, "non_eur"
    amount = _positive(gross.get("amount"))
    if amount is not None:
        return (
            (amount, "advertised_price")
            if currency == "EUR"
            else (None, "unknown_currency")
        )
    amount = _positive(net.get("amount"))
    if amount is not None:
        net_currency = net.get("currency")
        if net_currency != "EUR":
            return None, "unknown_currency" if not net_currency else "non_eur"
        return amount, "net_price"
    if item.get("leasingRate") or item.get("leasing"):
        return None, "monthly_payment"
    return None, "unknown"


def _listing(item, url, card=None, *, detail=False):
    identity = str(item.get("id", ""))
    if not re.fullmatch(r"[1-9]\d*", identity):
        return None
    attrs = _dict(item.get("attr"))
    tagged = (
        {a.get("tag"): a.get("value") for a in item.get("attributes", []) if isinstance(a, dict)}
        if isinstance(item.get("attributes"), list)
        else {}
    )
    contact = _dict(item.get("contact"))
    address = _dict(item.get("sellerAddress"))
    proofs = [v for v in [attrs.get("cn"), contact.get("country"), address.get("country")] if v]
    if not proofs or any(_country(v) != "DE" for v in proofs):
        return None
    own_url = item.get("url")
    if own_url and mobile_ad_id(own_url) != identity:
        return None
    bound_url = own_url or mobile_detail_url(
        identity,
        customer_id=item.get("sellerId") if urlsplit(url).hostname == "home.mobile.de" else None,
    )
    title = item.get("title") or " ".join(
        x for x in [item.get("shortTitle"), item.get("subTitle")] if isinstance(x, str)
    )
    if not title and card:
        title = _text(card.select_one('[data-testid$="-title"]'))
    if not isinstance(title, str) or not title:
        return None
    # Do not turn the reordered 218 Active Tourer i into an invented 218i.
    price_card = card if not detail else None
    price, price_type = _price(item, price_card)
    registration = _date(attrs.get("fr") or tagged.get("firstRegistration"))
    year = int(registration[:4]) if registration else None
    production = str(attrs.get("yc") or "")
    if year is None and re.fullmatch(r"(?:19|20)\d{2}", production):
        year = int(production)
    mileage_value = attrs.get("ml") or tagged.get("mileage")
    mileage = _integer(mileage_value) if re.search(r"\bkm\b", str(mileage_value)) else None
    description = item.get("htmlDescription") or item.get("description")
    if isinstance(description, str) and not description.startswith("$"):
        description = _text(BeautifulSoup(description, "html.parser")) or None
    else:
        description = None
    options = item.get("features") or []
    if card:
        options = options or [_text(x) for x in card.select('[data-testid="highlights-item"]')]
    images = [
        _image_url(x.get("uri") if isinstance(x, dict) else x) for x in item["images"]
    ] if isinstance(item.get("images"), list) else []
    if card and not any(images):
        images = [_image_url(x.get("src")) for x in card.select('img[data-testid*="-image"]')]
    links = [contact["homepageUrl"]] if isinstance(contact.get("homepageUrl"), str) else []
    if card:
        links += [
            urljoin(url, a["href"])
            for a in card.select("a[href]")
            if urlsplit(urljoin(url, a["href"])).scheme in {"https", "http"}
        ]
    raw = {
        "market_country": "DE",
        "country_evidence": proofs,
        "first_registration": registration,
        "attributes": attrs or tagged,
        "options": options,
        "price_components": item.get("price"),
        "leasing": item.get("leasing") or item.get("leasingRate"),
        "finance_plans": item.get("financePlans"),
        "seller_id": item.get("sellerId"),
        "detail_complete": bool(detail and description),
        "mobile_payload": item,
    }
    raw["seller_type"] = contact.get("enumType")
    raw["price_warning"] = None if price_type == "advertised_price" else price_type
    raw["seller_address"] = contact.get("address1")
    raw["seller_location"] = contact.get("address2")
    raw["dealer_links"] = [link for link in links if urlsplit(link).hostname == "home.mobile.de"]
    if tagged.get("sku") or item.get("sku"):
        raw["dealer_stock_id"] = tagged.get("sku") or item["sku"]
    if own_url:
        raw["publication_reference"] = {
            "source": SOURCE,
            "url": own_url,
            "external_id": identity,
            "kind": "explicit_own_publication_link",
        }
    vin = item.get("vin") or item.get("vehicleIdentificationNumber") or tagged.get("vin")
    vin = str(vin or "").strip().upper()
    vin = vin if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) and len(set(vin)) > 1 else None
    seller = contact.get("name")
    if not seller and card:
        info = card.select_one('[data-testid="seller-info"]')
        logo = info.select_one("img[alt]") if info else None
        seller = logo.get("alt", "").removeprefix("Logo ") if logo else None
    result = Listing(
        SOURCE,
        identity,
        bound_url,
        title,
        price=price,
        price_type=price_type,
        mileage=int(mileage) if mileage is not None else None,
        year=year,
        transmission=gearbox(attrs.get("tr") or tagged.get("transmission")),
        fuel=fuel(attrs.get("ft") or tagged.get("fuel")),
        kenteken=None,
        vin=vin,
        seller=seller,
        city=attrs.get("loc")
        or address.get("city")
        or re.sub(r"^DE-\d{5}\s+", "", contact.get("address2") or "")
        or None,
        description=description,
        images=list(dict.fromkeys(x for x in images if x)),
        links=list(dict.fromkeys(links)),
        raw=raw,
    ).qualify()
    raw["price_currency"] = _price_currency(item, price_card)
    if price_card and raw["price_currency"] != "UNKNOWN":
        raw["own_price_card_text"] = _text(price_card.select_one('[data-testid="main-price-label"]'))
    return result


def parse_mobile(body, url):
    """Parse retained public search/dealer/own-detail responses into existing Page."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    result = Page([], search_filters={"country": "DE", "coverage_status": "unverified"})
    if not _public_mobile(url):
        result.search_filters["parse_status"] = "unsupported_host"
        return result
    soup = BeautifulSoup(body, "html.parser")
    documents = _flight(body)
    if not documents:
        documents = list(json_documents(body))
    objects = [obj for document in documents for obj in walk(document)]
    searches = [
        o["searchResults"]
        for o in objects
        if isinstance(o.get("searchResults"), dict)
        and isinstance(o["searchResults"].get("listings"), list)
    ]
    searches += [
        o for o in objects if "numResultsTotal" in o and isinstance(o.get("listings"), list)
    ]
    detail_id = mobile_ad_id(url)
    if detail_id:
        candidates = [
            o["listing"]
            for o in objects
            if o.get("eventScope") == "page-vip" and isinstance(o.get("listing"), dict)
        ]
        unique = {json.dumps(o, sort_keys=True): o for o in candidates}
        if len(unique) != 1 or str(next(iter(unique.values())).get("id")) != detail_id:
            result.search_filters["parse_status"] = "detail_identity_unverified"
            return result
        item = next(iter(unique.values()))
        listing = _listing(item, url, soup, detail=True)
        if listing:
            result.listings = [listing]
            result.search_filters["parse_status"] = "parsed_detail"
        return result
    if not searches:
        result.search_filters["parse_status"] = "no_public_inventory"
        return result
    state = searches[0]
    effective = [o["searchParams"] for o in objects if isinstance(o.get("searchParams"), dict)]
    result.search_filters["effective_filters"] = effective[0] if effective else {}
    result.search_filters["country_filter_verified"] = bool(
        effective and _country(effective[0].get("cn"))
    )
    result.search_filters["national_geography_verified"] = bool(
        result.search_filters["country_filter_verified"]
        and not any(k in effective[0] for k in ["ll", "rd", "gn", "zip", "customerId"])
    )
    result.search_filters["coverage_status"] = "partial"
    result.search_filters["inventory_kind"] = (
        "dealer" if urlsplit(url).hostname == "home.mobile.de" else "search"
    )
    result.total = (
        state.get("numResultsTotal") if isinstance(state.get("numResultsTotal"), int) else None
    )
    result.page = state.get("pageNumber") if isinstance(state.get("pageNumber"), int) else None
    pagination = soup.select_one('[data-testid="srp-pagination"]')
    visible = re.search(r"(\d+)\s*/\s*(\d+)", _text(pagination))
    result.pages = int(visible[2]) if visible else None
    if result.pages:
        result.search_filters["visible_page_cap"] = result.pages
    bindings = {}
    for obj in objects:
        if "listingId" in obj and isinstance(obj.get("testId"), str):
            bindings.setdefault(str(obj["listingId"]), set()).add(obj["testId"])
    seen = set()
    for item in state["listings"]:
        if not isinstance(item, dict):
            continue
        identity = str(item.get("id", ""))
        if identity in seen:
            continue
        ids = bindings.get(identity, set())
        card = soup.find(attrs={"data-testid": next(iter(ids))}) if len(ids) == 1 else None
        listing = _listing(item, url, card)
        if listing:
            seen.add(identity)
            result.listings.append(listing)
    result.links = list(dict.fromkeys(x.url for x in result.listings))
    result.search_filters["parse_status"] = "parsed_search"
    return result


def mobile_dealer_ad_id(url):
    """Native dealer WP ID: a photo filename is not a Mobile ID."""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != DEALER_HOST
        or parts.netloc != DEALER_HOST
        or parts.query
        or parts.fragment
    ):
        return None
    match = re.fullmatch(r"/fahrzeuge/([1-9]\d*)/[a-z0-9-]+/", parts.path)
    return match[1] if match else None


def _dealer_state(soup):
    states = []
    for script in soup.select("script"):
        text = script.get_text()
        match = re.match(r"\s*window\.FWP_JSON\s*=\s*", text)
        if not match:
            continue
        try:
            state, _ = json.JSONDecoder().raw_decode(text[match.end() :])
        except ValueError:
            continue
        if isinstance(state, dict):
            states.append(state)
    return states[0] if len(states) == 1 else {}


def parse_mobile_dealer(body, url):
    """Public catalog of a dealer discovered on Mobile.de.

    08.10.2026: FWP preload contains 14 vehicles but has an empty country field.
    The catalog returns only the frontier; import data from native details
    with postid, JSON-LD URL, heading and Standort block. SOURCE remains
    the dealer: numeric image filenames do not prove a Mobile publication.
    """
    page = Page(
        [],
        search_filters={
            "coverage_status": "partial",
            "inventory_kind": "native_dealer",
            "national_geography_verified": False,
        },
    )

    def reject(reason):
        page.search_filters["parse_status"] = reason
        return page

    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != DEALER_HOST or parts.fragment:
        return reject("unsupported_dealer_url")
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    soup = BeautifulSoup(body, "html.parser")
    if parts.path == "/suche/":
        state = _dealer_state(soup)
        preload = state.get("preload_data") or {}
        if not isinstance(preload, dict):
            return reject("no_native_dealer_inventory")
        settings = preload.get("settings") or {}
        if not isinstance(settings, dict):
            return reject("no_native_dealer_inventory")
        pager = settings.get("pager") or {}
        listify = settings.get("listify") or {}
        if not isinstance(pager, dict) or not isinstance(listify, dict):
            return reject("no_native_dealer_inventory")
        inventory = listify.get("listings")
        if not isinstance(inventory, list):
            return reject("no_native_dealer_inventory")
        for field, key in [("total", "total_rows"), ("page", "page"), ("pages", "total_pages")]:
            value = pager.get(key)
            if type(value) is int and value >= 0:
                setattr(page, field, value)
        page.search_filters["effective_filters"] = parse_qs(parts.query)
        page.search_filters["country_filter_verified"] = False
        for item in inventory:
            if not isinstance(item, dict):
                continue
            own_url = item.get("permalink")
            obj = item.get("object") or {}
            if (
                isinstance(own_url, str)
                and mobile_dealer_ad_id(own_url) == str(item.get("id"))
                and isinstance(obj, dict)
                and str(obj.get("ID")) == str(item.get("id"))
                and isinstance(obj.get("guid"), str)
                and mobile_dealer_ad_id(obj["guid"]) == str(item.get("id"))
                and obj.get("post_status") == "publish"
                and own_url not in page.links
            ):
                page.links.append(own_url)
        return reject("discovery_requires_own_detail")

    identity = mobile_dealer_ad_id(url)
    if not identity or not soup.body or "postid-" + identity not in soup.body.get("class", []):
        return reject("dealer_detail_identity_unverified")
    heading = soup.select_one("h1.job_listing-title")
    sidebar = soup.select_one("#secondary .widget_listing_sidebar_contact")
    variant = _text(sidebar.select_one(".job_listing-title-sub")) if sidebar else ""
    model = _text(sidebar.select_one(".job_listing-title")) if sidebar else ""
    if not variant or variant != _text(heading) or not model:
        return reject("dealer_detail_title_unverified")
    schemas = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            schemas.extend(walk(json.loads(script.get_text())))
        except (ValueError, TypeError):
            continue
    own = [o for o in schemas if o.get("@type") == "Review" and o.get("@id") == url]
    pages = [o for o in schemas if o.get("@type") == "WebPage" and o.get("url") == url]
    if (
        len(own) != 1
        or len(pages) != 1
        or _text(BeautifulSoup(str(own[0].get("name", "")), "html.parser")) != variant
        or not isinstance(own[0].get("url"), dict)
        or own[0]["url"].get("@id") != url
    ):
        return reject("dealer_detail_schema_unverified")
    stock = sidebar.select_one(".listing_details_data-section-location")
    location = _text(stock)
    countries = re.findall(r"\b([A-Z]{2})-\d{5}\b", location)
    seller_countries = re.findall(
        r"\b([A-Z]{2})-\d{5}\b", _text(soup.select_one("#main .lc_listing_seller_contact_info"))
    )
    reviewed = own[0].get("itemReviewed") or {}
    if not isinstance(reviewed, dict) or not isinstance(reviewed.get("address"), dict):
        return reject("own_dealer_stock_country_not_DE")
    own_country = reviewed["address"].get("addressCountry")
    if (
        countries != ["DE"]
        or any(country != "DE" for country in seller_countries)
        or (own_country and _country(own_country) != "DE")
    ):
        return reject("own_dealer_stock_country_not_DE")
    country_claim = re.search(r"\bDE-\d{5}\s+([^<\n]+)", stock.get_text("\n", strip=True))
    if not country_claim:
        return reject("own_dealer_stock_country_not_DE")
    attrs = {}
    for dd in soup.select("#main .listify_widget_panel_listing_specifics dd[data-title]"):
        key = dd["data-title"]
        value = _text(dd)
        if key in attrs and attrs[key] != value:
            return reject("conflicting_own_dealer_attributes")
        attrs[key] = value
    prices, price_components, leasing = set(), [], []
    for section in soup.select("#primary .listing_details_data-section-price"):
        price_node = section.select_one(".listing_details_price")
        currency = _text(price_node.select_one(".currency_symbol")) if price_node else ""
        label = _text(price_node) + " " + _text(section.select_one("p"))
        amount = number(_text(price_node))
        price_components.append({"amount": amount, "currency": currency, "label": label})
        terms = _text(section.select_one(".listing_details_leasing"))
        if terms and terms not in leasing:
            leasing.append(terms)
        kind = "advertised_price"
        if currency != "EUR":
            amount, kind = None, "non_eur"
        elif re.search(r"monat|monthly|leasing|lease|/\s*(?:mo|month)\b", label, re.IGNORECASE):
            amount, kind = None, "monthly_payment"
        elif re.search(r"netto|zzgl\.?\s*(?:MwSt|Mehrwertsteuer)", label, re.IGNORECASE):
            kind = "net_price"
        elif not re.search(r"inkl\.?\s*(?:MwSt|Mehrwertsteuer)", label, re.IGNORECASE):
            amount, kind = None, "unknown"
        if amount is not None and amount < 0:
            amount, kind = None, "unknown"
        prices.add((amount, kind))
    price, price_type = next(iter(prices)) if len(prices) == 1 else (None, "ambiguous_price")
    registration = _date(attrs.get("erstzulassung"))
    mileage = attrs.get("kilometerstand", "")
    mileage_amount = number(mileage) if re.fullmatch(r"[\d., ]+\s+km", mileage) else None
    vin = attrs.get("fahrgestellnummer") or attrs.get("fahrzeugidentifikationsnummer") or ""
    vin = vin.strip().upper()
    vin = vin if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) and len(set(vin)) > 1 else None
    description = soup.select_one(
        "#main .listify_widget_panel_listing_description .listify_expand_text"
    )
    address_lines = stock.get_text("\n", strip=True).splitlines()
    country_index = address_lines.index(country_claim[0])
    seller = address_lines[country_index - 2] if country_index >= 2 else None
    address = address_lines[country_index - 1] if country_index >= 1 else None
    image = own[0].get("image")
    images = [image] if isinstance(image, str) and urlsplit(image).netloc == DEALER_HOST else []
    result = Listing(
        DEALER_SOURCE,
        identity,
        url,
        model + " " + variant,
        price=price,
        price_type=price_type,
        mileage=int(mileage_amount) if mileage_amount is not None else None,
        year=int(registration[:4]) if registration else None,
        transmission=gearbox(attrs.get("getriebeart")),
        fuel=fuel(attrs.get("kraftstoffart")),
        vin=vin,
        seller=seller,
        city=country_claim[1],
        description=_text(description) or None,
        images=images,
        raw={
            "market_country": "DE",
            "country_evidence": [
                {
                    "kind": "own_detail_stock_location",
                    "text": country_claim[0],
                    "url": url,
                    "own_id": identity,
                }
            ],
            "detail_complete": True,
            "first_registration": registration,
            "attributes": attrs,
            "model_brand": model.split()[0],
            "model_family": model,
            "model_variant": variant,
            "dealer_stock_id": attrs.get("fahrzeugnummer"),
            "seller_address": address,
            "seller_location": country_claim[0],
            "condition": attrs.get("zustand"),
            "price_components": price_components,
            "leasing": leasing,
            "recovery_route": "mobile_discovered_own_dealer",
            "mobile_publication_identity_verified": False,
        },
    )
    from .germany import qualify_german_item

    qualify_german_item(result)
    page.listings = [result]
    return reject("parsed_own_dealer_detail")


def collect_mobile_dealer(get, url=DEALER_SEARCH, *, max_details=30):
    """Bounded public collection through the supplied get -> Response, without a database.

    Do not invent pagination beyond the single observed page.
    The caller retains responses and imports items through observe_german_item.
    The first 403/429 stops requests to this dealer, without retry/browser fallback.
    """
    if type(max_details) is not int or not 0 <= max_details <= 100:
        raise ValueError("max_details must be an integer between 0 and 100")
    if url != DEALER_SEARCH:
        raise ValueError("Only the observed public dealer search is callable")
    responses, errors, items = [], [], []

    def fetch(own_url):
        response = get(own_url)
        responses.append(response)
        if response.status in {403, 429}:
            errors.append("access_blocked:" + str(response.status))
            return None
        if response.error or response.status != 200 or not response.body:
            errors.append("fetch_failed:" + own_url)
            return None
        if response.final_url != own_url:
            errors.append("unexpected_redirect:" + own_url)
            return None
        return parse_mobile_dealer(response.body, response.final_url)

    first = fetch(url)
    if first is not None:
        if first.pages != 1:
            errors.append("dealer_pagination_unobserved")
        if len(first.links) > max_details:
            errors.append("detail_budget_exhausted")
        for own_url in first.links[:max_details]:
            detail = fetch(own_url)
            if responses[-1].status in {403, 429}:
                break
            if detail is None or len(detail.listings) != 1:
                errors.append("own_detail_unverified:" + own_url)
                continue
            item = detail.listings[0]
            if item.external_id != mobile_dealer_ad_id(own_url):
                errors.append("own_detail_identity_mismatch:" + own_url)
                continue
            items.append(item)
        if first.total is None or len(items) != first.total:
            errors.append(f"own_detail_count_mismatch:{len(items)}/{first.total}")
    return {
        "source": DEALER_SOURCE,
        "status": "complete_dealer_subset" if first is not None and not errors else "partial",
        "expected_count": first.total if first else None,
        "discovered_count": len(first.links) if first else 0,
        "received_count": len(items),
        "target_count": sum(item.scope == "target" for item in items),
        "national_coverage_verified": False,
        "mobile_publications_verified": 0,
        "errors": errors,
        "listings": items,
        "responses": responses,
    }
