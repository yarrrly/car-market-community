"""Typed native AutoScout listings for the two C205 markets."""

import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .germany_autoscout import (
    _dict,
    _integer,
    _label,
    _price,
    _price_currency,
    _registration_year,
    _state,
    _text,
)
from .identity import normalized_vin
from .model_profiles import MERCEDES_PROFILE, qualify_mercedes
from .parse import Listing, Page, fuel, gearbox

# Five UUID sections; explicit shape prevents recommendation/route substitution.
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"


def _host(url, country):
    try:
        parsed = urlsplit(url)
        hosts = (
            {"www.autoscout24.de", "autoscout24.de"}
            if country == "DE"
            else {"www.autoscout24.nl", "autoscout24.nl"}
        )
        return (
            parsed.scheme == "https"
            and parsed.hostname in hosts
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )
    except (ValueError, TypeError, AttributeError):
        return False


def autoscout_identity(url, country):
    if not _host(url, country):
        return None
    route = "angebote" if country == "DE" else "aanbod"
    path = urlsplit(url).path
    match = re.fullmatch(
        r"/" + route + r"/(?:[^/]*-)?(" + _UUID + r")/?", path, re.IGNORECASE
    )
    if not match and country == "DE":
        match = re.fullmatch(r"/smyle/details/(" + _UUID + r")/?", path, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _item(own, url, country, detail):
    vehicle = own.get("vehicle") or {}
    identity = str(own.get("id") or "").lower()
    if not isinstance(vehicle, dict) or autoscout_identity(url, country) != identity:
        return None
    raw = dict(own)
    from .search_profile import is_configured

    raw["model_profile"] = "configured_vehicle" if is_configured() else MERCEDES_PROFILE
    raw["market_context_country"] = country
    location = own.get("location") or {}
    raw["market_country"] = (
        location.get("countryCode") if isinstance(location, dict) else None
    )
    raw["publication_reference"] = {
        "source": "autoscout24_de" if country == "DE" else "autoscout24",
        "external_id": identity,
        "url": url,
        "kind": "explicit_own_publication_link",
    }
    if detail:
        raw["availability_status"] = "available"
    raw["detail_complete"] = bool(detail and _text(own.get("description")))
    raw["own_body_type"] = (
        vehicle.get("bodyType") or vehicle.get("body") or vehicle.get("variant")
    )
    raw["body_type"] = raw["own_body_type"]
    detail_fields = (
        {
            x.get("iconName"): x.get("data")
            for x in own["vehicleDetails"]
            if isinstance(x, dict) and isinstance(x.get("iconName"), str)
        }
        if isinstance(own.get("vehicleDetails"), list)
        else {}
    )
    registered = (
        vehicle.get("firstRegistrationDateRaw")
        or vehicle.get("firstRegistrationDate")
        or detail_fields.get("calendar")
    )
    raw["own_first_registration"] = registered
    mileage = (
        vehicle.get("mileageInKmRaw")
        if vehicle.get("mileageInKmRaw") is not None
        else vehicle.get("mileageInKm")
    )
    title = " ".join(
        _label(x)
        for x in [
            vehicle.get("make"),
            vehicle.get("motorTypeName") or vehicle.get("model"),
            vehicle.get("variant"),
            vehicle.get("modelVersionInput"),
        ]
        if x
    )
    price, price_type, price_path = _price(own)
    raw["purchase_price_path"] = price_path
    raw["price_currency"] = _price_currency(own)
    raw["price_warning"] = None if price_type == "advertised_price" else price_type
    seller = own.get("seller") or {}
    make = vehicle.get("make")
    raw["model_brand"] = _label(make)
    vin = normalized_vin(
        vehicle.get("vin") or vehicle.get("vehicleIdentificationNumber")
    )
    own_plate = (
        vehicle.get("licensePlate")
        or vehicle.get("registrationPlate")
        or own.get("licensePlate")
    )
    from .parse import plate

    item = Listing(
        source="autoscout24_de" if country == "DE" else "autoscout24",
        external_id=identity,
        url=url,
        title=title,
        price=price,
        price_type=price_type,
        mileage=_integer(mileage),
        year=_registration_year(registered),
        transmission=gearbox(
            vehicle.get("transmissionType") or vehicle.get("transmission")
        ),
        fuel=fuel(
            _dict(vehicle.get("fuelCategory")).get("formatted") or vehicle.get("fuel")
        ),
        kenteken=plate(own_plate) if country == "NL" else None,
        vin=vin,
        seller=seller.get("companyName") if isinstance(seller, dict) else None,
        city=location.get("city") if isinstance(location, dict) else None,
        description=_text(own.get("description")) or None,
        images=[x for x in own["images"] if isinstance(x, str)]
        if isinstance(own.get("images"), list)
        else [],
        raw=raw,
    )
    if is_configured():
        from .search_profile import qualify

        return qualify(item)
    return qualify_mercedes(item, country)


def _configured_smyle(state, url, country, canonicals):
    """Decode the observed own Smyle envelope, never checkout/recommendation data."""
    identity = autoscout_identity(url, country)
    proper = _dict(state.get("properData"))
    own = _dict(proper.get("carDetails"))
    ocs = _dict(own.get("ocsInfo"))
    if (
        not identity
        or state.get("listingAvailable") is not True
        or proper.get("guid") != identity
        or own.get("id") != identity
        or ocs.get("status") != "ACTIVE"
        or any(
            autoscout_identity(urljoin(url, link), country) != identity
            for link in canonicals
        )
    ):
        return Page([])
    vehicle = dict(_dict(own.get("vehicle")))
    vehicle.update(
        make=own.get("make"),
        model=own.get("model"),
        modelVersionInput=own.get("modelVersion"),
        vin=own.get("vin"),
    )
    primary = _dict(vehicle.get("primaryFuel"))
    additional = vehicle.get("additionalFuel")
    additional = additional if isinstance(additional, list) else []

    def component(value):
        value = str(value or "").strip().casefold()
        if value in {"super 95", "super 98", "super plus", "super e10", "super e5"}:
            return "petrol"
        if value == "strom":
            return "electric"
        return fuel(value)

    parts = {component(primary.get("formatted"))}
    parts.update(
        component(_dict(_dict(entry).get("value")).get("formatted"))
        for entry in additional
    )
    parsed_fuel = None
    if None not in parts:
        parsed_fuel = (
            "hybrid"
            if "electric" in parts and parts & {"petrol", "diesel"}
            else next(iter(parts))
            if len(parts) == 1
            else None
        )
    vehicle["fuelCategory"] = {"formatted": parsed_fuel}
    value = _dict(_dict(own.get("price")).get("value"))
    transformed = {
        "id": identity,
        "vehicle": vehicle,
        "location": ocs.get("location") or {},
        "price": {
            "priceRaw": value.get("raw"),
            "priceFormatted": value.get("formatted"),
        },
        "seller": ocs.get("seller") or {},
        "images": [own["imageUrl"]] if isinstance(own.get("imageUrl"), str) else [],
    }
    item = _item(transformed, url, country, True)
    if item:
        item.raw["own_smyle_record"] = own
        item.raw["own_fuel_fields"] = {
            "primaryFuel": primary,
            "additionalFuel": additional,
        }
        item.raw["purchase_price_path"] = "properData.carDetails.price.value.raw"
        item.raw["detail_complete"] = False
    return Page([item] if item else [], total=1)


def _search_path(path):
    native = re.fullmatch(r"/_next/data/[^/]+(/lst/[A-Za-z0-9_/-]+)\.json", path)
    return (native[1] if native else path).rstrip("/")


def _inventory_query_matches(request, native, country, *, ordering=True, path=""):
    # A cached narrower year/price response cannot certify a broader request.
    # These query axes occur in the retained C205 and DE price-control inputs.
    for key in ("fregfrom", "fregto", "pricefrom", "priceto"):
        if key not in request and native.get(key) not in (None, ""):
            return False
    # Native route IDs and these exact defaults are evidenced by the eight
    # original NL/DE captures. Any other response-only facet can narrow the
    # user's inventory and must not certify a broader request.
    defaults = {"powertype": "kw", "pricetype": "public", "show_nfm": "true"}
    defaults.update(
        {"ustate": "N,U"}
        if country == "NL"
        else {"lm_premium": "true", "specialconditions": "true"}
    )
    # The retained C205 searches encode these exact two facets in their route.
    # Do not invent mappings for unobserved native body/transmission slugs.
    if "bt_coupe" in _search_path(path).split("/"):
        defaults["body"] = "3"
    if "tr_automatik" in _search_path(path).split("/"):
        defaults["gear"] = "A"
    route_or_metadata = {
        "cat",
        "mcat",
        "mmmv",
        "cy",
        "page",
        "sort",
        "desc",
        "query_id",
        "search_id",
        "tier_rotation",
        "country",
        "build_id",
        "page_size",
        "page_cap",
        "search_path",
    }
    for key, value in native.items():
        if key in request or key in route_or_metadata or value in (None, ""):
            continue
        if key not in defaults or type(value) is not str or value != defaults[key]:
            return False
    for key, values in request.items():
        if key in {"cy", "page", "query_id", "search_id", "tier_rotation"}:
            continue
        if not ordering and key in {"sort", "desc"}:
            continue
        if len(values) != 1:
            return False
        if key not in native:
            # NL omits this exact include request in all four retained model
            # searches. DE echoes it; neither market may contradict it.
            # Evidence: tests/fixtures/autoscout_query_binding/manifest.json.
            if country == "NL" and key == "damaged_listing" and values == ["include"]:
                continue
            return False
        value = native[key]
        if type(value) not in (str, int) or values != [str(value)]:
            return False
    return True


def _native_taxonomy_matches(native):
    """Bind opaque native IDs, preserving the observed DE group expansion."""
    fields = {key: native[key] for key in ("cat", "mcat", "mmmv") if key in native}
    if any(not isinstance(value, str) or not value for value in fields.values()):
        return False
    categories = fields.get("cat", "").split(",") if "cat" in fields else []
    bases = fields.get("mcat", "").split(",") if "mcat" in fields else []
    if (
        bases
        and categories
        and any(
            not any(
                category.startswith(base)
                and (len(category) == len(base) or not category[len(base)].isdigit())
                for base in bases
            )
            for category in categories
        )
    ):
        return False
    make_ids = []
    for values in (categories, bases):
        if values:
            matches = [re.match(r"ma([0-9]+)(?:[^0-9]|$)", value) for value in values]
            if not all(matches):
                return False
            make_ids.append({match[1] for match in matches})
    if "mmmv" in fields:
        selections = [value.split("|") for value in fields["mmmv"].split(",")]
        if any(len(value) != 4 or not value[0].isdigit() for value in selections):
            return False
        make_ids.append({value[0] for value in selections})
        # NL model IDs echo directly. DE can instead return group/motor cats
        # while mmmv retains the legacy model ID; do not guess that mapping.
        models = [
            re.match(r"ma[0-9]+mo([0-9]+)(?:[^0-9]|$)", value) for value in categories
        ]
        if (
            models
            and all(models)
            and {match[1] for match in models} != {value[1] for value in selections}
        ):
            return False
    return not make_ids or all(value == make_ids[0] for value in make_ids)


def _search_inventory_matches(state, parsed, native, country):
    if not _inventory_query_matches(
        parse_qs(parsed.query, keep_blank_values=True),
        native,
        country,
        path=parsed.path,
    ):
        return False
    if not _native_taxonomy_matches(native):
        return False
    declared = state.get("pagePath")
    if declared is not None:
        if not isinstance(declared, str):
            return False
        try:
            route = urlsplit(declared)
        except ValueError:
            return False
        if (
            route.scheme
            or route.netloc
            or _search_path(route.path) != _search_path(parsed.path)
        ):
            return False
    elif any(
        key in state
        for key in ("taxonomy", "breadcrumbs", "canonicalOrigin", "listPageOrigin")
    ):
        # Full modern envelopes declare their own route. Older retained typed
        # projections lack these routing fields and still bind explicit filters.
        return False
    if any(key in state for key in ("taxonomy", "canonicalOrigin", "listPageOrigin")):
        breadcrumbs = state.get("breadcrumbs")
        if (
            not isinstance(breadcrumbs, list)
            or not breadcrumbs
            or not isinstance(breadcrumbs[-1], dict)
        ):
            return False
        own_route = breadcrumbs[-1].get("url")
        if not isinstance(own_route, str):
            return False
        breadcrumb = urlsplit(own_route)
        if (
            breadcrumb.scheme
            or breadcrumb.netloc
            or _search_path(breadcrumb.path) != _search_path(parsed.path)
        ):
            return False
    return True


def parse_mercedes_autoscout(body, url, country):
    if country not in {"NL", "DE"} or not _host(url, country):
        return Page([])
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if not isinstance(body, str):
        return Page([])
    if not body.lstrip().startswith("{"):
        scripts = BeautifulSoup(body, "html.parser").select("script#__NEXT_DATA__")
        if len(scripts) != 1 or scripts[0].get("type") != "application/json":
            return Page([])
    state, _envelope, canonicals, build = _state(body)
    if state is None:
        return Page([])
    from .search_profile import is_configured

    if (
        country == "DE"
        and urlsplit(url).path.startswith("/smyle/details/")
        and is_configured()
    ):
        return _configured_smyle(state, url, country, canonicals)
    own = state.get("listingDetails")
    if isinstance(own, dict):
        identity = autoscout_identity(url, country)
        declared = own.get("webPage")
        status = own.get("status")
        if (
            not identity
            or not isinstance(declared, str)
            or autoscout_identity(declared, country) != identity
            or status != "Active"
        ):
            return Page([])
        if len(canonicals) > 1 or any(
            autoscout_identity(urljoin(url, c), country) != identity for c in canonicals
        ):
            return Page([])
        item = _item(own, url, country, True)
        return Page([item] if item else [], total=1)
    if not isinstance(state.get("listings"), list):
        return Page([])
    parsed = urlsplit(url)
    if not (
        re.fullmatch(r"/lst/[A-Za-z0-9_/-]+", parsed.path)
        or re.fullmatch(r"/_next/data/[^/]+/lst/[A-Za-z0-9_/-]+\.json", parsed.path)
    ):
        return Page([])
    query = _dict(state.get("pageQuery"))
    request = parse_qs(parsed.query, keep_blank_values=True)
    cy = query.get("cy")
    expected = ("D", "DE") if country == "DE" else ("NL",)
    page = _integer(query.get("page", "1"))
    if (
        cy not in expected
        or request.get("cy") not in ([code] for code in expected)
        or page is None
        or not 1 <= page <= 200
        or request.get("page", ["1"]) != [str(page)]
    ):
        return Page([])
    if is_configured():
        if not _search_inventory_matches(state, parsed, query, country):
            return Page([])
        document = (
            json.loads(body)
            if body.lstrip().startswith("{")
            else json.loads(scripts[0].string or "")
        )
        routing = _dict(document.get("query"))
        if "slug" in routing or document.get("page") == "/lst/[...slug]":
            # The independent own route declaration must agree as well; one
            # pagePath/breadcrumb field cannot substitute a different model.
            slug = routing.get("slug")
            if (
                not isinstance(slug, list)
                or slug != _search_path(parsed.path).split("/")[2:]
            ):
                return Page([])
    stable = {
        key: value
        for key, value in query.items()
        if key not in {"page", "query_id", "sort", "desc", "search_id", "tier_rotation"}
    }
    filters = {
        **stable,
        "country": country,
        "build_id": build,
        "page_size": 20,
        "page_cap": 200,
    }
    if is_configured():
        filters["search_path"] = _search_path(parsed.path)
    if not build:
        match = re.search(r"/_next/data/([^/]+)/", urlsplit(url).path)
        if match:
            filters["build_id"] = match[1]
    items = []
    for row in state["listings"]:
        if not isinstance(row, dict) or not isinstance(row.get("url"), str):
            continue
        item = _item(row, urljoin(url, row["url"]), country, False)
        if item:
            items.append(item)
    return Page(
        items,
        total=_integer(state.get("numberOfResults")),
        pages=_integer(state.get("numberOfPages")),
        page=page,
        search_filters=filters,
    )


def mercedes_autoscout_page_url(query, page, first):
    url = query["url"]
    country = first.search_filters.get("country")
    if (
        country not in {"NL", "DE"}
        or not _host(url, country)
        or not isinstance(page, int)
        or isinstance(page, bool)
        or not 1 <= page <= 200
    ):
        raise ValueError("Invalid AutoScout page")
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    build = first.search_filters.get("build_id")
    if (
        first.search_filters.get("country") != country
        or not isinstance(build, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", build)
    ):
        raise ValueError("Unproven AutoScout country/build")
    if not re.fullmatch(r"/lst/[A-Za-z0-9_/-]+", path):
        raise ValueError("Invalid search route")
    params = parse_qs(parsed.query, keep_blank_values=True)
    expected = (["D"], ["DE"]) if country == "DE" else (["NL"],)
    if params.get("cy") not in expected:
        raise ValueError("AutoScout query contradicts proven country")
    proven_path = first.search_filters.get("search_path")
    if proven_path is not None and (
        _search_path(path) != proven_path
        or not _inventory_query_matches(
            params, first.search_filters, country, ordering=False, path=path
        )
    ):
        raise ValueError("AutoScout query contradicts proven inventory filters")
    params["page"] = [str(page)]
    return urlunsplit(
        parsed._replace(
            path=f"/_next/data/{build}{path}.json",
            query=urlencode(params, doseq=True),
            fragment="",
        )
    )
