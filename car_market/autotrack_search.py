"""AutoTrack's own Next search envelope, observed on 11 October 2026."""

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from .germany_autoscout import (
    _dict,
    _integer,
    _label,
    _price,
    _price_currency,
    _registration_year,
    _state,
)
from .parse import Listing, Page, fuel, gearbox, plate

HOSTS = {"www.autotrack.nl", "autotrack.nl"}
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
VOLATILE = {"page", "query_id", "search_id", "tier_rotation", "host"}


def _public(url):
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in HOSTS
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )
    except (ValueError, TypeError):
        return False


def _card(own, base):
    if not isinstance(own, dict) or not isinstance(own.get("url"), str):
        return None
    try:
        url = urljoin(base, own["url"])
        path = urlsplit(url).path
    except (ValueError, TypeError):
        return None
    identity = str(own.get("id") or "").lower()
    match = re.fullmatch(r"/a/[^/]+-(" + UUID + r")/?", path, re.IGNORECASE)
    vehicle = own.get("vehicle")
    if (
        not _public(url)
        or not match
        or match[1].lower() != identity
        or not isinstance(vehicle, dict)
    ):
        return None
    location, seller = _dict(own.get("location")), _dict(own.get("seller"))
    detail_rows = own.get("vehicleDetails")
    detail_rows = detail_rows if isinstance(detail_rows, list) else []
    image_rows = own.get("images")
    image_rows = image_rows if isinstance(image_rows, list) else []
    details = {
        x.get("iconName"): x.get("data")
        for x in detail_rows
        if isinstance(x, dict) and not x.get("isPlaceholder")
    }
    raw = dict(own)
    price, price_type, price_path = _price(own)
    raw.update(
        market_country=location.get("countryCode"),
        country_code=location.get("countryCode"),
        model_brand=_label(vehicle.get("make")),
        own_uuid=identity,
        detail_complete=False,
        native_adapter="autotrack_own_next_search_v1",
        purchase_price_path=price_path,
        price_currency=_price_currency(own),
        publication_reference={
            "source": "autotrack",
            "external_id": url,
            "url": url,
            "kind": "explicit_own_publication_link",
        },
    )
    title = " ".join(
        _label(vehicle.get(k))
        for k in ("make", "model", "modelVersionInput")
        if vehicle.get(k)
    )
    item = Listing(
        "autotrack",
        url,
        url,
        title,
        price=price,
        price_type=price_type,
        mileage=_integer(
            vehicle.get("mileageInKmRaw")
            if vehicle.get("mileageInKmRaw") is not None
            else vehicle.get("mileageInKm") or details.get("mileage_odometer")
        ),
        year=_registration_year(
            vehicle.get("firstRegistrationDateRaw")
            or vehicle.get("firstRegistrationDate")
            or details.get("calendar")
        ),
        transmission=gearbox(
            _label(vehicle.get("transmission") or details.get("gearbox"))
        ),
        fuel=fuel(_label(vehicle.get("fuel") or details.get("gas_pump"))),
        kenteken=plate(vehicle.get("licensePlate") or own.get("licensePlate")),
        seller=seller.get("companyName"),
        city=location.get("city"),
        images=[x for x in image_rows if isinstance(x, str)],
        raw=raw,
    )
    return item.qualify()


def parse_native_search(body, url):
    """Return None only for the older site; rejected native state never falls back."""
    if not _public(url):
        return Page([])
    parsed = urlsplit(url)
    if parsed.path.startswith("/a/"):
        return None
    state, _envelope, _canonicals, build = _state(body)
    if state is None:
        return (
            Page([])
            if "__NEXT_DATA__" in body or body.lstrip().startswith("{")
            else None
        )
    if (
        "listings" not in state
        or "numberOfResults" not in state
        or not isinstance(state.get("listings"), list)
    ):
        return Page([])
    native_path = re.fullmatch(
        r"/_next/data/([A-Za-z0-9_-]{1,200})/lst\.json", parsed.path
    )
    if not (re.fullmatch(r"/aanbod(?:/[A-Za-z0-9_-]+)*", parsed.path) or native_path):
        return Page([])
    if parsed.path != "/aanbod" and not native_path:
        declared = state.get("pagePath")
        if not isinstance(declared, str) or urlsplit(declared).path != parsed.path:
            return Page([])
    query = _dict(state.get("pageQuery"))
    request = parse_qs(parsed.query, keep_blank_values=True)
    page_value = query.get("page")
    page = (
        int(page_value)
        if isinstance(page_value, (str, int))
        and not isinstance(page_value, bool)
        and re.fullmatch(r"[0-9]+", str(page_value))
        else None
    )
    if (
        query.get("cy") != "NL"
        or query.get("host") not in (None, *HOSTS)
        or page is None
        or not 1 <= page <= 200
        or request.get("page", ["1"]) != [str(page)]
    ):
        return Page([])
    for key, values in request.items():
        if key not in VOLATILE and values != [str(query.get(key))]:
            return Page([])
    if native_path:
        if build is not None and build != native_path[1]:
            return Page([])
        build = native_path[1]
    if not isinstance(build, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", build):
        return Page([])
    # Keep the native resolved route; the configured legacy URL can redirect.
    stable = {k: v for k, v in query.items() if k not in VOLATILE}
    if any(not isinstance(value, (str, int, bool)) for value in stable.values()):
        return Page([])
    # Resolved native filters also bind searches whose model lives in the path.
    params = sorted((k, str(v)) for k, v in stable.items())
    first_url = urlunsplit(
        parsed._replace(path="/aanbod", query=urlencode(params), fragment="")
    )
    filters = {
        "native_adapter": "autotrack_own_next_search_v1",
        "native_query": stable,
        "build_id": build,
        "search_url": first_url,
        "page_cap": 200,
    }
    items = [item for own in state["listings"] if (item := _card(own, url)) is not None]
    total, pages = state.get("numberOfResults"), state.get("numberOfPages")
    if (
        type(total) is not int
        or total < 0
        or type(pages) is not int
        or pages < 0
        or total > 0
        and pages == 0
    ):
        total = pages = None
    return Page(
        items,
        total=total,
        pages=pages,
        page=page,
        search_filters=filters,
    )


def native_page_url(page, first):
    filters = first.search_filters
    url, build = filters.get("search_url"), filters.get("build_id")
    if (
        type(page) is not int
        or not 1 <= page <= 200
        or not _public(url)
        or not isinstance(build, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", build)
    ):
        raise ValueError("Invalid proven AutoTrack page")
    parsed = urlsplit(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["page"] = [str(page)]
    return urlunsplit(
        parsed._replace(
            path=f"/_next/data/{build}/lst.json",
            query=urlencode(params, doseq=True),
            fragment="",
        )
    )
