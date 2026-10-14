"""Native NL facts and contracts; a search filter does not prove C205/country."""

import copy
import json
import math
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from .parse import Listing, Page, dutch_listing, fuel, gearbox, json_documents, parse_page, walk


def _at(data, *path):
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _first(*values):
    return next(
        (value for value in values if value is not None and value not in ("", {}, [])), None
    )


def _country(value):
    if isinstance(value, dict):
        value = _first(
            value.get("code"),
            value.get("isoCode"),
            value.get("countryCode"),
            value.get("formatted"),
            value.get("name"),
        )
    text = str(value or "").strip().upper()
    return {
        "NEDERLAND": "NL",
        "NETHERLANDS": "NL",
        "NLD": "NL",
        "BELGIË": "BE",
        "BELGIE": "BE",
        "BELGIUM": "BE",
        "BELGIEN": "BE",
        "DUITSLAND": "DE",
        "DEUTSCHLAND": "DE",
        "GERMANY": "DE",
        "DEU": "DE",
        "FRANKRIJK": "FR",
        "FRANCE": "FR",
    }.get(text, text) or None


def _offer(record):
    offer = record.get("offers")
    if isinstance(offer, list):
        offer = offer[0] if len(offer) == 1 else None
    return offer if isinstance(offer, dict) else {}


def _own_detail_uri(record, url):
    """The main card schema must point to the same publication origin/path."""
    declared = _first(record.get("url"), record.get("@id"), _offer(record).get("url"))
    if not isinstance(declared, str):
        return False
    expected, actual = urlsplit(url), urlsplit(urljoin(url, declared))
    return bool(
        expected.scheme in {"http", "https"}
        and (expected.scheme, expected.hostname, expected.path.rstrip("/"))
        == (actual.scheme, actual.hostname, actual.path.rstrip("/"))
    )


def normalize_autokopen_detail_id(item, requested_url, final_url, body):
    """Normalize an own URI fallback only with exact request/final/canonical binding."""
    from bs4 import BeautifulSoup

    if (
        item.source != "autokopen"
        or item.raw.get("detail_adapter") != "autokopen_price_on_request"
        or not requested_url == final_url == item.url == item.external_id
    ):
        return
    uri = urlsplit(requested_url)
    match = re.fullmatch(r"/auto/detail/[^/]+-(\d+)", uri.path)
    if (
        uri.scheme != "https"
        or uri.netloc not in {"autokopen.nl", "www.autokopen.nl"}
        or uri.query
        or uri.fragment
        or not match
    ):
        return
    canonical = BeautifulSoup(body, "html.parser").select('head link[rel~="canonical"]')
    if len(canonical) == 1 and canonical[0].get("href") == requested_url:
        item.external_id = match.group(1)



def _retain_autotrack_dealer(item, body, url):
    """Only the dealer card in the VDP; do not use the footer or description text."""
    from bs4 import BeautifulSoup

    identity = re.search(r"-(\d+)/?$", urlsplit(url).path)
    if (
        not identity
        or identity.group(1) != str(item.external_id)
        or not _own_detail_uri(item.raw, url)
    ):
        return
    soup = BeautifulSoup(body, "html.parser")
    routes = soup.select('a[data-testid="vehicle-dealer-route"][href]')
    if len(routes) != 1:
        return
    route = routes[0]
    destination = parse_qs(urlsplit(route["href"]).query).get("destination", [])
    if len(destination) != 1:
        return
    parts = [x.strip() for x in destination[0].split(",")]
    postal = re.fullmatch(r"([1-9]\d{3}\s?[A-Z]{2})\s+(.+)", parts[-1]) if len(parts) == 3 else None
    if not postal:
        return
    for block in route.parents:
        if block.name not in {"div", "section"}:
            continue
        lines = [node.get_text(" ", strip=True) for node in block.select("p")]
        dealer_links = [
            a
            for a in block.select('a[href^="/autobedrijf/"]')
            if re.fullmatch(r"/autobedrijf/[^/]+/\d+(?:/voorraad)?", a["href"])
        ]
        if parts[0] in lines and parts[1] in lines and parts[2] in lines and dealer_links:
            item.raw["own_detail_dealer"] = {
                "name": parts[0],
                "address": {
                    "streetAddress": parts[1],
                    "postalCode": postal.group(1),
                    "addressLocality": postal.group(2),
                },
                "binding": "own_vdp_vehicle_dealer_route_and_visible_card",
                "publication_url": url,
            }
            return


def own_nl_evidence(source, record):
    """Accept exactly one native card without recursively searching for sellers."""
    record = record if isinstance(record, dict) else {}
    general = _first(
        _at(record, "autogegevens", "algemeen"),
        _at(record, "carData", "general"),
        record.get("vehicle"),
        {},
    )
    history = _first(
        _at(record, "autogegevens", "geschiedenis"), _at(record, "carData", "history"), {}
    )
    attrs = record.get("attributes", {})
    if isinstance(attrs, list):
        attrs = {x["key"]: x.get("value") for x in attrs if isinstance(x, dict) and "key" in x}
    if not isinstance(attrs, dict):
        attrs = {}
    general = general if isinstance(general, dict) else {}
    history = history if isinstance(history, dict) else {}
    attrs = {k: v.get("value") if isinstance(v, dict) else v for k, v in attrs.items()}
    for group in _at(record, "carAttributes", "groupedWithIcons") or []:
        for attribute in group.get("attributes", []):
            if isinstance(attribute, dict) and attribute.get("key"):
                attrs.setdefault(attribute["key"], attribute.get("value"))
    offer = _offer(record)
    seller = _first(
        _at(record, "aanbieder", "aanbiedergegevens"),
        _at(record, "seller", "sellerData"),
        record.get("dealer"),
        record.get("company"),
        record.get("seller"),
        offer.get("seller"),
        offer.get("offeredBy"),
        record.get("own_detail_dealer"),
        {},
    )
    if not isinstance(seller, dict):
        seller = {}
    schema_address = _at(offer, "availableAtOrFrom", "address") or {}
    location = record.get("location") or seller.get("location") or {}
    address = _first(
        seller.get("address"),
        seller.get("postalAddress"),
        _at(record, "own_detail_dealer", "address"),
        {},
    )
    if isinstance(address, str):
        address = {"streetAddress": address}
    if not isinstance(address, dict):
        address = {}
    if not isinstance(location, dict):
        location = {}
    dealer_address = {
        "name": _first(
            seller.get("name"),
            seller.get("naam"),
            seller.get("title"),
            seller.get("companyName"),
            attrs.get("Bedrijfsnaam"),
            _at(record, "own_detail_dealer", "name"),
        ),
        "street_address": _first(
            address.get("streetAddress"),
            seller.get("street"),
            seller.get("straat"),
            location.get("street"),
            attrs.get("Adres"),
        ),
        "postal_code": _first(
            address.get("postalCode"),
            seller.get("postalcode"),
            seller.get("postalCode"),
            seller.get("postal_code"),
            seller.get("postcode"),
            schema_address.get("postalCode"),
            location.get("zipCode"),
            location.get("zip"),
            attrs.get("Postcode"),
        ),
        "city": _first(
            address.get("addressLocality"),
            seller.get("city"),
            seller.get("plaatsnaam"),
            schema_address.get("addressLocality"),
            location.get("city"),
            location.get("cityName"),
            attrs.get("Plaats"),
        ),
        "country": _country(
            _first(
                address.get("addressCountry"),
                seller.get("countryCode"),
                seller.get("country"),
                schema_address.get("addressCountry"),
                location.get("countryCode"),
                location.get("countryAbbreviation"),
                location.get("countryName"),
                attrs.get("Land"),
            )
        ),
        "bound_to_own_listing": bool(
            seller or schema_address or location or attrs.get("Land") or attrs.get("Bedrijfsnaam")
        ),
    }
    evidence = {
        "source": source,
        "brand": _first(
            general.get("merknaam"),
            general.get("brandName"),
            general.get("make"),
            record.get("brand"),
            record.get("brand_name"),
            _at(record, "carDetails", "brand"),
            _at(record, "own_detail_vehicle", "brand"),
        ),
        "model_family": _first(
            general.get("modelnaam"),
            general.get("modelName"),
            general.get("modelGroup"),
            record.get("model"),
            record.get("model_name"),
            _at(record, "carDetails", "model"),
            _at(record, "own_detail_vehicle", "model"),
            attrs.get("model"),
        ),
        "model_variant": _first(
            general.get("commercieleUitvoering"),
            general.get("uitvoering"),
            general.get("commercialEdition"),
            general.get("modelVersionInput"),
            record.get("variant"),
            record.get("vehicleConfiguration"),
            attrs.get("trim"),
        ),
        "body_type": _first(
            general.get("carrosserievorm"),
            general.get("bodyType"),
            record.get("bodyType"),
            record.get("body_type"),
            attrs.get("body"),
            attrs.get("carrosserie"),
            attrs.get("Carrosserie"),
            attrs.get("Categorie"),
            attrs.get("vehicleType"),
            general.get("variant")
            if str(general.get("variant", "")).casefold() in {"coupe", "coupé"}
            else None,
        ),
        "doors": _first(
            general.get("aantalDeuren"),
            general.get("numberOfDoors"),
            general.get("doors"),
            record.get("numberOfDoors"),
            attrs.get("deuren"),
            attrs.get("Deuren"),
            attrs.get("doors"),
        ),
        "generation": _first(
            general.get("generation"),
            general.get("modelGeneration"),
            _at(general, "classification", "modelGeneration"),
            _at(general, "rawData", "classification", "modelGeneration"),
            record.get("generation"),
            attrs.get("generation"),
        ),
        "production_date": _first(
            history.get("productionDate"),
            record.get("productionDate"),
            record.get("production_date"),
        ),
        "production_year": _first(
            history.get("productionYear"),
            record.get("productionYear"),
            record.get("production_year"),
        ),
        "model_year": _first(
            general.get("modelYear"),
            general.get("modeljaar"),
            history.get("modelYear"),
            history.get("modeljaar"),
            record.get("modelYear"),
            record.get("model_year"),
            record.get("vehicleModelDate"),
        ),
        "construction_year": _first(
            history.get("bouwjaar"),
            history.get("constructionYear"),
            general.get("year"),
            record.get("year"),
            record.get("build_year"),
            attrs.get("constructionYear"),
            attrs.get("bouwjaar"),
        ),
        "first_registration": _first(
            history.get("datumEersteToelatingInternationaal"),
            history.get("dateFirstAdmissionInternational"),
            history.get("firstRegistrationDate"),
            general.get("firstRegistrationDate"),
            record.get("dateVehicleFirstRegistered"),
            attrs.get("datum_deel_1"),
        ),
        "vin": _first(
            record.get("vin"),
            record.get("vehicleIdentificationNumber"),
            general.get("vin"),
            attrs.get("vin"),
        ),
        "country": _country(
            _first(record.get("countryCode"), record.get("country"), dealer_address["country"])
        ),
        "dealer_address": dealer_address,
        "infer_missing": False,
    }
    # Explicit generation code only in the card model/variant fields.
    if evidence["generation"] is None:
        title = " ".join(str(record.get(k) or "") for k in ("title", "name", "variant", "type"))
        match = re.search(r"\bC(?:204|205)\b", title, re.IGNORECASE)
        if match:
            evidence["generation"] = match.group().upper()
    return evidence


def enrich_nl_listing(item):
    """Retain fields for the strict profile gate; do not assign generation from year."""
    item.raw = copy.deepcopy(item.raw or {})
    evidence = own_nl_evidence(item.source, item.raw)
    item.raw["mercedes_nl_evidence"] = evidence
    for source_key, raw_key in (
        ("brand", "model_brand"),
        ("model_family", "model_family"),
        ("model_variant", "model_variant"),
    ):
        if evidence[source_key] is not None:
            item.raw.setdefault(raw_key, evidence[source_key])
    for key in (
        "generation",
        "production_date",
        "production_year",
        "model_year",
        "first_registration",
        "vin",
        "country",
    ):
        if evidence[key] is not None:
            item.raw.setdefault(key, evidence[key])
    if evidence["body_type"] is not None:
        item.raw.setdefault("body_type", evidence["body_type"])
    if evidence["doors"] is not None:
        item.raw.setdefault("doors", evidence["doors"])
    item.raw.setdefault("dealer_address", evidence["dealer_address"])
    address = evidence["dealer_address"]
    if address["bound_to_own_listing"]:
        item.raw.setdefault(
            "dealerAddress",
            {
                "name": address["name"],
                "streetAddress": address["street_address"],
                "postalCode": address["postal_code"],
                "city": address["city"],
                "country": address["country"],
            },
        )
    if evidence["country"] is not None:
        item.raw.setdefault("country_code", evidence["country"])
    if evidence["vin"] is not None and item.vin is None:
        item.vin = evidence["vin"]
    return item


def observed_nl_contract(source, body):
    """Only native response state; the request URL is not evidence."""
    if source in {"marktplaats", "anwb", "autofirst"} and body.lstrip().startswith("{"):
        data = json.loads(body)
        if source == "marktplaats":
            return {
                "filters": data.get("searchRequest"),
                "total": data.get("totalResultCount"),
                "page_limit": data.get("maxAllowedPageNumber"),
            }
        return {
            "total": data.get("total", _at(data, "meta", "total")),
            "page": data.get("pageNumber", _at(data, "meta", "current_page")),
            "page_size": data.get("pageSize", _at(data, "meta", "per_page")),
        }
    nodes = [n for doc in json_documents(body) for n in walk(doc)]
    if source == "gaspedaal":
        own = next(
            (
                n
                for n in nodes
                if isinstance(n.get("searchRequestBody"), dict)
                and isinstance(n.get("commonPortalURLProps"), dict)
            ),
            None,
        )
        if own:
            p = own["commonPortalURLProps"]
            return {
                "filters": own["searchRequestBody"],
                "total": p.get("totalOccasions"),
                "page": p.get("pageNumber"),
                "page_size": p.get("pageSize"),
            }
    if source == "autotrack":
        data = next(
            (
                n
                for n in nodes
                if isinstance(n.get("merkModel"), dict) and "filter" in n["merkModel"]
            ),
            None,
        )
        inventory = next(
            (
                n
                for n in nodes
                if isinstance(n.get("hits"), list)
                and {"total", "pageNumber", "pageSize"} <= n.keys()
            ),
            {},
        )
        return {
            "filters": data,
            **{k: inventory.get(k) for k in ("total", "pageNumber", "pageSize")},
        }
    if source == "autokopen":
        own = next((n for n in nodes if "initialResults" in n and "searchParams" in n), {})
        return {"filters": own.get("searchParams"), **own.get("meta", {})}
    if source == "autotrader":
        state = next((n for n in nodes if "listings" in n and "pageQuery" in n), {})
        return {
            "filters": state.get("pageQuery"),
            "total": state.get("numberOfResults"),
            "pages": state.get("numberOfPages"),
        }
    if source == "viabovag":
        own = next(
            (
                n
                for n in nodes
                if isinstance(n.get("searchResults"), dict) and "results" in n["searchResults"]
            ),
            {},
        )
        return {
            "filters": own.get("serverSearchRequest"),
            "total": _at(own, "searchResults", "count"),
        }
    return {}


def parse_mercedes_nl_page(source, body, url):
    """Wrapper for the model_profile dispatcher; the caller sets use_profile(...)."""
    if source == "autokopen" and "/auto/detail/" not in url:
        own = next(
            (
                n
                for d in json_documents(body)
                for n in walk(d)
                if isinstance(n.get("initialResults"), list) and "meta" in n
            ),
            None,
        )
        if own is None:
            return Page([])
        meta = own["meta"]
        page = Page([], total=meta.get("total"), pages=meta.get("pages"), page=meta.get("page"))
        for row in own["initialResults"]:
            dealer = row.get("dealer") or {}
            item = Listing(
                source,
                str(row["id"]),
                urljoin(url, "/auto/detail/" + row["slug"]),
                " ".join(str(row.get(k) or "") for k in ("title", "variant")),
                price=row.get("price"),
                mileage=row.get("mileage"),
                year=row.get("year"),
                fuel=fuel(row.get("fuel")),
                transmission=gearbox(row.get("transmission")),
                seller=dealer.get("name"),
                city=dealer.get("city"),
                raw=row,
            )
            page.listings.append(enrich_nl_listing(item).qualify())
        return page
    if source == "autotrack" and body.lstrip().startswith("{"):
        data = json.loads(body)
        total, size = data.get("total"), data.get("pageSize")
        page = Page(
            [dutch_listing(source, row, url) for row in data.get("hits", [])],
            total=total,
            page=data.get("pageNumber"),
            pages=math.ceil(total / size) if total is not None and size else None,
        )
    elif source in {"autofirst", "vakgarage", "carteam", "autouncle", "autokopen"}:
        from .quality_dealer_adapters import parse_quality_page

        page = parse_quality_page(source, body, url)
    else:
        page = parse_page(source, body, url)
    for item in page.listings:
        if source == "autotrack" and len(page.listings) == 1 and "/a/" in urlsplit(url).path:
            _retain_autotrack_dealer(item, body, url)
        if source == "autokopen" and len(page.listings) == 1:
            path = urlsplit(url).path
            sku = str(item.raw.get("sku") or "")
            if (
                path.startswith("/auto/detail/")
                and re.fullmatch(r"\d+", sku)
                and path.rstrip("/").endswith("-" + sku)
                and _own_detail_uri(item.raw, url)
            ):
                item.external_id = sku
        if source == "autotrader" and isinstance(item.raw.get("vehicle"), dict):
            v = item.raw["vehicle"]
            item.title = " ".join(
                str(v.get(k) or "")
                for k in ("make", "motorTypeName", "variant", "modelVersionInput")
            )
        enrich_nl_listing(item).qualify()
    return page
