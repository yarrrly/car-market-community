"""Public Kleinanzeigen listing cards; responses from 07.10.2026.

Search is discovery with unknown country. Only publication details with proven
geography become observations; advertising cards do not prove exhaustion of
the result set. The configured collector handles network access.
"""

import hashlib
import json
import math
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .db import now
from .identity import normalized_vin
from .parse import Listing, Page, fuel, gearbox, number

SOURCE = "kleinanzeigen_de"
PAGE_SIZE = 25
GERMAN_STATES = (
    "Baden-Württemberg",
    "Bayern",
    "Berlin",
    "Brandenburg",
    "Bremen",
    "Hamburg",
    "Hessen",
    "Mecklenburg-Vorpommern",
    "Niedersachsen",
    "Nordrhein-Westfalen",
    "Rheinland-Pfalz",
    "Saarland",
    "Sachsen",
    "Sachsen-Anhalt",
    "Schleswig-Holstein",
    "Thüringen",
)


def _public(url):
    try:
        p = urlsplit(url)
        return (
            p.scheme == "https"
            and p.hostname in {"www.kleinanzeigen.de", "kleinanzeigen.de"}
            and not p.username
            and not p.password
            and p.port in (None, 443)
        )
    except (ValueError, TypeError):
        return False


def _ad_id(url):
    if not _public(url):
        return None
    match = re.fullmatch(r"/s-anzeige/[^/]+/([1-9]\d*)-216-\d+/?", urlsplit(url).path)
    return match[1] if match else None


def _search_parts(url):
    if not _public(url):
        return None
    pattern = (
        r"/s-autos/(?:(?P<region>[a-z-]+)/)?(?:sortierung:neuste/)?"
        r"(?:preis:(?P<low>\d*):(?P<high>\d*)/)?"
        r"(?:seite:(?P<page>[1-9]\d*)/)?[^/]+/k0c216(?:l(?P<location>[1-9]\d*))?/?"
    )
    match = re.fullmatch(pattern, urlsplit(url).path)
    if match is None:
        return None
    data = match.groupdict()
    if bool(data["region"]) != bool(data["location"]):
        return None
    if data["low"] is not None:
        if not data["low"] and not data["high"]:
            return None
        low, high = (int(data[k]) if data[k] else None for k in ("low", "high"))
        if low is not None and high is not None and low > high:
            return None
        data.update(low=low, high=high, price_filtered=True)
    else:
        data["price_filtered"] = False
    return data


def _search_route(url):
    return _search_parts(url) is not None


def kleinanzeigen_page_url(url, page):
    """Native region/sort/price precede /seite:N/; preserve all own bounds."""
    if not _search_route(url) or not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValueError("invalid_kleinanzeigen_page")
    p = urlsplit(url)
    path = re.sub(r"/seite:\d+", "", p.path)
    if page > 1:
        segments = path.strip("/").split("/")
        segments.insert(len(segments) - 2, f"seite:{page}")
        path = "/" + "/".join(segments)
    return urlunsplit(p._replace(path=path, fragment=""))


def kleinanzeigen_region_queries(body, url):
    """Native state links form discovery partitions, never item country proof."""
    parts = _search_parts(url)
    if parts is None or parts["region"] or parts["price_filtered"]:
        raise ValueError("national_unpriced_root_required")
    names = {name.casefold() for name in GERMAN_STATES}
    found = {}
    for anchor in BeautifulSoup(body, "html.parser").select("a[href]"):
        if anchor.get_text(" ", strip=True).casefold() not in names:
            continue
        target = urljoin(url, anchor["href"])
        fields = _search_parts(target)
        if fields and fields["region"] and not fields["price_filtered"]:
            found[fields["region"]] = target
    return [
        {
            "source": SOURCE,
            "key": "active_region_" + key,
            "url": value,
            "max_pages": 50,
            "detail_limit": 2500,
            "request_limit": 2600,
            "pagination_certified": True,
            "infer_missing": False,
        }
        for key, value in sorted(found.items())
    ]


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def _astro_value(value, depth=0):
    """JSON-only Astro [0,value]/[1,array]; reject unknown tags."""
    if depth > 40:
        raise ValueError("astro_depth")
    if isinstance(value, dict):
        return {k: _astro_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        # Native tracker props contain [0] for an absent posterType.
        if len(value) == 1 and type(value[0]) is int and value[0] == 0:
            return None
        if len(value) != 2 or type(value[0]) is not int or value[0] not in (0, 1):
            raise ValueError("unsupported_astro_tag")
        tag, child = value
        if tag == 1:
            if not isinstance(child, list) or len(child) > 5000:
                raise ValueError("invalid_astro_array")
            return [_astro_value(x, depth + 1) for x in child]
        if isinstance(child, list):
            raise TypeError("invalid_astro_value")
        return _astro_value(child, depth + 1)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("invalid_astro_number")
    return value


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_astro_key")
        result[key] = value
    return result


def _native_promotions(soup, cards, url):
    """One tracker with an exact match to the native DOM id + seoLink."""
    trackers = []
    try:
        for node in soup.select('astro-island[component-url*="ImpressionTracker."][props]'):
            text = node["props"]
            if len(text) > 2_000_000:
                raise ValueError("astro_too_large")
            props = json.loads(text, object_pairs_hook=_unique_json)
            if isinstance(props, dict) and "resultAds" in props:
                trackers.append(_astro_value(props))
        if len(trackers) != 1:
            raise ValueError("missing_or_ambiguous_tracker")
        rows = trackers[0].get("resultAds")
        if not isinstance(rows, list):
            raise TypeError("invalid_tracker_rows")
        owned_rows = []
        for row in rows:
            if not isinstance(row, dict):
                raise TypeError("invalid_tracker_wrapper")
            if not isinstance(row.get("organicAdPreview"), dict):
                if row.get("sponsoredAdPresent") is True and row.get("organicAdPreview") is None:
                    continue  # Native empty advertising slots are not listings.
                raise ValueError("unbound_tracker_row")
            owned_rows.append(row)
        if len(owned_rows) != len(cards):
            raise ValueError("tracker_card_count_mismatch")
        bound, original_previews = {}, {}
        for wrapper, card in zip(owned_rows, cards):
            own = wrapper.get("organicAdPreview") if isinstance(wrapper, dict) else None
            if not isinstance(own, dict) or type(own.get("id")) not in (str, int):
                raise ValueError("invalid_own_preview")
            identity = str(own["id"])
            native_url = own.get("seoLink")
            if (
                identity != card.get("data-adid")
                or not isinstance(native_url, str)
                or urljoin(url, native_url) != urljoin(url, card.get("data-href", ""))
                or _ad_id(urljoin(url, native_url)) != identity
            ):
                raise ValueError("tracker_DOM_identity_mismatch")
            top, features, sponsored = (
                own.get("topAd"),
                own.get("appliedFeatures"),
                wrapper.get("sponsoredAdPresent"),
            )
            if (
                type(top) is not bool
                or not isinstance(features, list)
                or any(not isinstance(x, str) for x in features)
                or type(sponsored) is not bool
            ):
                raise ValueError("ambiguous_native_promotion")
            stable = {key: value for key, value in own.items() if key != "topAd"}
            if identity in original_previews and original_previews[identity] != stable:
                raise ValueError("duplicate_preview_facts_conflict")
            original_previews[identity] = stable
            placement = "promoted" if top else "sponsored" if sponsored else "organic"
            occurrence = {
                "placement": placement,
                "topAd": top,
                "sponsoredAdPresent": sponsored,
                "id": identity,
                "seoLink": native_url,
            }
            if identity in bound:
                bound[identity]["native_occurrences"].append(occurrence)
                bound[identity]["organic_present"] |= placement == "organic"
                continue
            bound[identity] = {
                "native_occurrences": [occurrence],
                "organic_present": placement == "organic",
                "placement": "promoted" if top else "sponsored" if sponsored else "organic",
                "topAd": top,
                "appliedFeatures": features,
                "sponsoredAdPresent": sponsored,
                "highlight": own.get("highlight"),
                "priorityAd": own.get("priorityAd"),
                "id": identity,
                "seoLink": native_url,
                "own_location_claims": {
                    k: v
                    for k, v in own.items()
                    if "country" in k.lower() or "location" in k.lower()
                },
                "proof": "bound_ImpressionTracker.resultAds.organicAdPreview",
            }
        return bound, None
    except (ValueError, TypeError, AttributeError, RecursionError) as error:
        return {}, str(error)


def _relevant(title):
    title = title.lower()
    return bool(
        re.search(r"\bbmw\b", title)
        and re.search(r"\b218\s*i?\b", title)
        and not re.search(r"\b218\s*d\b|gran\s*(?:tour|coup)|coup[eé]|cabrio", title)
    )


def _amount(text):
    value = number(text)
    return value if value is not None and math.isfinite(value) and value >= 0 else None


def _year(text):
    match = re.search(r"\b((?:19|20)\d{2})\b", text or "")
    return int(match[1]) if match else None


def _price_kind(label):
    if re.search(r"monat|mtl|leasing", label, re.IGNORECASE):
        return "lease_price" if "leasing" in label.lower() else "monthly_price"
    if re.search(r"netto|exkl\.?\s*MwSt|zzgl\.?\s*MwSt", label, re.IGNORECASE):
        return "net_price"
    if "export" in label.lower():
        return "export_price"
    if "anzahlung" in label.lower():
        return "deposit_price"
    return "advertised_price"


def _autohero_primary_reference(item):
    """The single primary own-intro from two retained Autohero syndications."""
    if item.raw.get("market_country") != "DE" or not item.description:
        return
    description = item.description
    intro = re.match(
        r"^Autohero\s*[-–]\s*Europas größter freier Händler mit über [\d.,]+ Autos zu Top-Preisen\s+Schaue dir diesen (?P<model>BMW .+?) jetzt auf autohero\.com an, um mehr Informationen zur Servicehistorie, Fahrzeugdaten, Gebrauchsspuren sowie weitere Details zu erhalten\.\s*(?P<url>https://[^\s<>]+)",
        description,
    )
    if not intro or len(re.findall(r"https?://[^\s<>]+", description)) != 1:
        return
    model = intro["model"]
    required = r"\b218\s*i\b.*\bActive\s+Tourer\b"
    if not re.search(required, model, re.IGNORECASE) or not re.search(
        required, item.title, re.IGNORECASE
    ):
        return

    def normalized(text):
        return re.sub(r"[^a-z0-9]", "", text.lower())

    if not normalized(item.title).startswith(normalized(model)):
        return
    original = intro["url"]
    try:
        parsed = urlsplit(original)
        own_id = re.fullmatch(
            r"/de/[a-z0-9-]+/id/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?", parsed.path
        )
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"www.autohero.com", "autohero.com"}
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.fragment
            or not own_id
        ):
            return
    except ValueError:
        return
    canonical = urlunsplit(parsed._replace(query="", fragment=""))
    item.raw["own_publication_reference"] = dict(item.raw["publication_reference"])
    item.raw["publication_reference"] = {
        "source": "autohero_de",
        "url": canonical,
        "external_id": own_id[1],
        "kind": "explicit_own_publication_link",
    }
    item.raw["reference_evidence"] = {
        "kind": "singular_own_primary_publisher_intro",
        "source_path": "#viewad-product #viewad-description-text",
        "offset_units": "unicode_codepoints_in_normalized_own_description",
        "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
        "intro": description[: intro.end("model")],
        "intro_span": [0, intro.end("model")],
        "model": model,
        "own_title": item.title,
        "url_span": list(intro.span("url")),
        "original_url": original,
        "canonical_url": canonical,
    }
    item.links.append({"label": "publisher", "url": canonical})


def _own_geography(root, soup):
    locality = root.select_one("#viewad-locality")
    sources = [("#viewad-main-info #viewad-locality", _text(locality))]
    if locality:
        address = locality.parent
        sources.extend(
            ("own_address.addressCountry", x.get("content") or _text(x))
            for x in address.select('[itemprop="addressCountry"]')
        )
    breadcrumb = soup.select_one("main #vap-brdcrmb")
    # Only the listing geographic link, excluding Autos/general navigation.
    if breadcrumb:
        sources.extend(
            ("main #vap-brdcrmb own_location_link", _text(x))
            for x in breadcrumb.select('a[href^="/stadt/"], a[href^="/bundesland/"]')
        )
    regions, explicit, foreign = set(), False, set()
    for _, text in sources:
        for state in GERMAN_STATES:
            if re.search(r"(?<![\w-])" + re.escape(state) + r"(?![\w-])", text, re.IGNORECASE):
                regions.add(state)
        explicit |= bool(re.search(r"\b(?:DE|DEU|Deutschland|Germany)\b", text, re.IGNORECASE))
        for country, pattern in {
            "NL": r"\b(?:NL|Niederlande|Nederland|Netherlands)\b",
            "AT": r"\b(?:AT|Österreich|Austria)\b",
            "CH": r"\b(?:CH|Schweiz|Switzerland)\b",
            "BE": r"\b(?:BE|Belgien|Belgium)\b",
            "FR": r"\b(?:FR|Frankreich|France)\b",
        }.items():
            if re.search(pattern, text, re.IGNORECASE):
                foreign.add(country)
    country = (
        "CONFLICT"
        if foreign and (explicit or regions)
        else next(iter(foreign))
        if len(foreign) == 1
        else "UNKNOWN"
        if foreign
        else "DE"
        if explicit or regions
        else "UNKNOWN"
    )
    proof = {
        "kind": "explicit_own_location_country"
        if explicit
        else "derived_country_from_german_state"
        if regions
        else "unverified_own_location",
        "sources": [{"path": path, "value": text} for path, text in sources if text],
        "matched_regions": sorted(regions),
        "mapping": {state: "DE" for state in sorted(regions)},
        "foreign_country_claims": sorted(foreign),
        "result": country,
    }
    return country, proof


def _bound_og_geography(root, soup, identity, country, proof):
    """Head product geography bound to verified own adID and visible locality."""
    if country != "UNKNOWN":
        return country, proof  # Existing own foreign/conflicting location remains excluded.

    def rejected(reason):
        return country, {**proof, "bound_og_rejection": reason}

    if proof["foreign_country_claims"]:
        return rejected("own_visible_foreign_country")
    heads = soup.select("head")
    localities = root.select("#viewad-locality")
    if len(heads) != 1 or len(localities) != 1:
        return rejected("missing_or_ambiguous_own_head_locality")
    claims = {}
    for field in ("type", "url", "country-name", "locality", "region"):
        nodes = heads[0].select(f'meta[property="og:{field}"]')
        if len(nodes) != 1:
            if field == "region" and not nodes:
                continue
            return rejected(f"missing_or_ambiguous_og:{field}")
        value = nodes[0].get("content")
        if not isinstance(value, str) or not value.strip():
            return rejected(f"empty_og:{field}")
        claims[field] = value.strip()
    if claims["type"] != "product":
        return rejected("og_type_not_product")
    if _ad_id(claims["url"]) != identity:
        return rejected("og_url_not_own_ad")
    if claims["country-name"].casefold() not in {"de", "deu", "germany", "deutschland"}:
        return rejected("og_country_not_DE")

    def locality_text(text):
        return " ".join(text.split())

    visible = locality_text(_text(localities[0]))
    unprefixed = re.sub(r"^\d{5}\s+", "", visible)
    if not unprefixed or locality_text(claims["locality"]).casefold() != unprefixed.casefold():
        return rejected("og_locality_not_own_visible_locality")
    region = next(
        (
            state
            for state in GERMAN_STATES
            if state.casefold() == claims.get("region", "").casefold()
        ),
        None,
    )
    if "region" in claims and region is None:
        return rejected("og_region_not_german_state")
    # No postal code / coordinates inference: country is an explicit product claim.
    return "DE", {
        **proof,
        "kind": "bound_own_product_og_location",
        "sources": proof["sources"]
        + [
            {"path": f"head meta[property='og:{key}']", "value": value}
            for key, value in claims.items()
        ],
        "matched_regions": [region] if region else [],
        "mapping": {region: "DE"} if region else {},
        "og_country_normalized": "DE",
        "own_identity": {
            "external_id": identity,
            "og_url": claims["url"],
            "verified_by": "unique_own_canonical_adID_and_main_title_and_idbox",
        },
        "locality_binding": {
            "visible": visible,
            "without_postcode": unprefixed,
            "og": claims["locality"],
        },
        "result": "DE",
    }


def _detail(soup, url):
    identity = _ad_id(url)
    canonicals, roots, idboxes = (
        soup.select('link[rel~="canonical"][href]'),
        soup.select("#viewad-main-info"),
        soup.select("#viewad-ad-id-box"),
    )
    if (
        len(canonicals) != 1
        or _ad_id(urljoin(url, canonicals[0]["href"])) != identity
        or len(roots) != 1
        or len(idboxes) != 1
        or re.findall(r"\b\d+\b", _text(idboxes[0])) != [identity]
    ):
        return Page([])
    root = roots[0]
    titles = root.select("#viewad-title")
    if len(titles) != 1 or not _text(titles[0]):
        return Page([])
    fields = {}
    for row in soup.select(
        "#viewad-details .addetailslist--detail, #viewad-product .addetailslist--detail"
    ):
        value = row.select_one(".addetailslist--detail--value")
        if value:
            label = " ".join(str(x).strip() for x in row.contents if isinstance(x, str)).strip()
            if label in fields and fields[label] != _text(value):
                return Page([])
            fields[label] = _text(value)
    country, geography = _own_geography(root, soup)
    country, geography = _bound_og_geography(root, soup, identity, country, geography)
    desc_node = soup.select_one("#viewad-product #viewad-description-text")
    description = _text(desc_node) or None
    price_label = _text(root.select_one("#viewad-price"))
    currency = root.select_one('meta[itemprop="currency"]')
    currency = currency.get("content", "EUR") if currency else "EUR"
    images = list(
        dict.fromkeys(
            x.get("data-imgsrc") or x.get("src")
            for x in soup.select("#viewad-product img#viewad-image")
            if urlsplit(x.get("data-imgsrc") or x.get("src") or "").scheme == "https"
        )
    )
    vin = normalized_vin(
        fields.get("FIN") or fields.get("VIN") or fields.get("Fahrzeugidentifikationsnummer")
    )
    if vin is None:
        vins = {
            normalized_vin(m[1])
            for m in re.finditer(
                r"\b(?:FIN|VIN|Fahrzeugidentifikationsnummer)\s*[:=]\s*([A-HJ-NPR-Z0-9]{17})(?![A-Z0-9])",
                description or "",
                re.IGNORECASE,
            )
        } - {None}
        vin = next(iter(vins)) if len(vins) == 1 else None
    options = [_text(x) for x in soup.select("#viewad-product .checktaglist .checktag")]
    seller = soup.select_one("#viewad-contact .userprofile-vip a")
    canonical = urljoin(url, canonicals[0]["href"])
    raw = {
        "market_country": country,
        "country_evidence": geography,
        "detail_complete": bool(description),
        "own_details": fields,
        "own_location": _text(root.select_one("#viewad-locality")),
        "model_brand": fields.get("Marke"),
        "model_family": fields.get("Modell"),
        "price_label": price_label,
        "price_currency": currency,
        "options": options,
        "description_claims": description,
        "first_registration": fields.get("Erstzulassung"),
        "publication_reference": {
            "source": SOURCE,
            "url": canonical,
            "external_id": identity,
            "kind": "explicit_own_publication_link",
        },
    }
    # A shortened own title ("218 Active Tourer") can be resolved by an
    # explicit opening statement about this car, never by later promotions.
    intro = re.match(
        r"^\s*(?:(?:Top[- ]gepflegter|Gepflegter|Sehr gepflegter)\s+)?BMW\s+218\s*i\b",
        description or "",
        re.IGNORECASE,
    )
    if (
        intro
        and country == "DE"
        and fuel(fields.get("Kraftstoffart")) == "petrol"
        and re.search(r"\bbmw\s+218\b", _text(titles[0]), re.IGNORECASE)
        and re.search(r"active\s*[- ]?\s*tour", _text(titles[0]), re.IGNORECASE)
    ):
        raw["model_variant"] = "218i"
        raw["own_model_evidence"] = {
            "kind": "bound_own_product_opening_model",
            "external_id": identity,
            "path": "#viewad-product #viewad-description-text",
            "opening": intro[0],
            "offset": [intro.start(), intro.end()],
            "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
            "own_title": _text(titles[0]),
        }
    visible_status = _text(
        root.select_one('#viewad-status, .boxedarticle--status, [data-testid="ad-status"]')
    ).lower()
    if "verkauft" in visible_status:
        raw.update(sold=True, availability_status="sold")
    elif "reserviert" in visible_status:
        raw.update(reserved=True, availability_status="reserved")
    mileage = _amount(fields.get("Kilometerstand", ""))
    item = Listing(
        SOURCE,
        identity,
        canonical,
        _text(titles[0]),
        price=_amount(price_label),
        price_type=_price_kind(price_label),
        mileage=int(mileage) if mileage is not None else None,
        year=_year(fields.get("Erstzulassung")),
        transmission=gearbox(fields.get("Getriebe")),
        fuel=fuel(fields.get("Kraftstoffart")),
        kenteken=None,
        vin=vin,
        seller=_text(seller) or None,
        city=raw["own_location"] or None,
        description=description,
        images=images,
        raw=raw,
    ).qualify()
    if (
        country == "UNKNOWN"
        and _relevant(item.title)
        and item.fuel not in {"diesel", "hybrid"}
        and fields.get("Marke", "BMW").upper() == "BMW"
    ):
        item.scope = "needs_confirmation"
    _autohero_primary_reference(item)
    return Page(
        [item],
        total=1,
        pages=1,
        page=1,
        search_filters={"country": country, "payload": "own_detail_dom"},
    )


def parse_kleinanzeigen(body, url) -> Page:
    """Publication details or search discovery; unknown country does not become DE."""
    if not _public(url) or not isinstance(body, (str, bytes)):
        return Page([])
    soup = BeautifulSoup(body, "html.parser")
    if _ad_id(url):
        return _detail(soup, url)
    if not _search_route(url):
        return Page([])
    summary = _text(soup.select_one("#srp-breadcrumb-summary"))
    match = re.search(r"([\d.]+)\s*-\s*([\d.]+)\s+von\s+([\d.]+)", summary)
    bounds = tuple(int(x.replace(".", "")) for x in match.groups()) if match else None
    total = bounds[2] if bounds else None
    route_page = re.search(r"/seite:(\d+)/", urlsplit(url).path)
    route_page = int(route_page[1]) if route_page else 1
    page = (bounds[0] - 1) // PAGE_SIZE + 1 if bounds else None
    cards = soup.select("#srchrslt-adtable article[data-adid][data-href]")
    promotions, promotion_error = _native_promotions(soup, cards, url)
    result = Page(
        [],
        total=total,
        pages=math.ceil(total / PAGE_SIZE) if total is not None else None,
        page=page,
        search_filters={
            "country": "UNKNOWN",
            "search_heading": summary,
            "declared_range": list(bounds) if bounds else None,
            "range_valid": bool(bounds and 1 <= bounds[0] <= bounds[1] <= bounds[2]),
            "route_page": route_page,
            "page_size": PAGE_SIZE,
            "promotion_classification": "bound_native_tracker"
            if promotion_error is None
            else "unknown",
            "promotion_error": promotion_error,
        },
    )
    parts = _search_parts(url)
    if parts:
        result.search_filters["requested_region"] = parts["region"]
        result.search_filters["requested_location_id"] = parts["location"]
        if parts["price_filtered"]:
            result.search_filters["price_bounds"] = [parts["low"], parts["high"]]
            for key, expected in (("minPrice", parts["low"]), ("maxPrice", parts["high"])):
                values = {
                    int(node["value"]) if node.get("value", "") else None
                    for node in soup.select(f'input[name="{key}"]')
                    if node.get("value", "").isdigit() or not node.get("value", "")
                }
                if values and values != {expected}:
                    result.search_filters["price_filter_conflict"] = key
                    result.search_filters["range_valid"] = False
    seen = set()
    for position, card in enumerate(cards, 1):
        identity, link = card.get("data-adid"), urljoin(url, card.get("data-href", ""))
        if _ad_id(link) != identity or identity in seen:
            continue
        seen.add(identity)
        title = _text(card.select_one("h3"))
        price_node = next(
            (
                x
                for x in card.select("p")
                if "€" in _text(x) and "line-through" not in x.get("class", [])
            ),
            None,
        )
        price_label = _text(price_node)
        text = _text(card)
        mileage = re.search(r"([\d.]+)\s*km\b", text)
        year = re.search(r"\bEZ\s+\d{1,2}/(\d{4})\b", text)
        promoted = bool(
            card.select('[data-testid="top-ad"], .aditem-addon-top, .aditem-addon-highlight')
        ) or any(
            card.get(k) in {"top", "premium", "promoted"} for k in ["data-adtype", "data-placement"]
        )
        raw = {
            "market_country": "UNKNOWN",
            "discovery_only": True,
            "country_evidence": {
                "kind": "unverified_search_card",
                "search_heading": summary,
                "image_alt_claims": [x.get("alt") for x in card.select("img[alt]")],
            },
            "card_text": text,
            "own_card_sha256": hashlib.sha256(str(card).encode()).hexdigest(),
            "card_attributes": dict(card.attrs),
            "position": position,
            "placement": promotions.get(identity, {}).get("placement", "unknown"),
            "promotion_verified": identity in promotions,
            "native_preview": promotions.get(identity),
            "display_promotion_hint": promoted,
            "price_label": price_label,
        }
        item = Listing(
            SOURCE,
            identity,
            link,
            title,
            price=_amount(price_label),
            price_type=_price_kind(price_label),
            mileage=int(_amount(mileage[1]))
            if mileage and _amount(mileage[1]) is not None
            else None,
            year=int(year[1]) if year else None,
            images=[x["src"] for x in card.select("img[src]")],
            raw=raw,
        )
        item.scope = "needs_confirmation" if _relevant(title) else "excluded"
        result.listings.append(item)
    result.links = [x.url for x in result.listings]
    return result


def collect_kleinanzeigen(collector, run, query):
    """Discovery ledger and native DE details, without generic search ingest."""
    if getattr(collector, "country", None) != "DE" or query.get("source") != SOURCE:
        raise ValueError("kleinanzeigen_requires_german_collector")
    started, key = now(), query["key"]
    ledger = {
        "version": 1,
        "source": SOURCE,
        "query_key": key,
        "run": run,
        "started_at": started,
        "pages": [],
        "cards": {},
        "detail_results": {},
        "errors": [],
        "country_policy": "search_unknown_details_required",
        "promotion_policy": "only_bound_native_tracker_flags_certify_organic",
    }
    config = getattr(collector, "config", {})
    page_limit = query.get("max_pages", config.get("max_pages", 30))
    detail_limit = query.get("detail_limit", config.get("kleinanzeigen_detail_limit", 100))
    request_limit = query.get("request_limit", page_limit + detail_limit)
    if any(
        not isinstance(v, int) or isinstance(v, bool) or v < 1
        for v in [page_limit, detail_limit, request_limit]
    ):
        raise ValueError("invalid_kleinanzeigen_budget")
    requested, expected, last_page, attempted, observed = 0, None, 1, 0, 0

    def persist():
        collector.db.conn.execute(
            "INSERT OR REPLACE INTO monitor_state(key,value) VALUES (?,?)",
            (
                "kleinanzeigen_discovery:" + key,
                json.dumps(ledger, ensure_ascii=False, sort_keys=True),
            ),
        )
        collector.db.conn.commit()

    def fetch(url, entry=None):
        nonlocal requested
        policy = getattr(collector, "specialized_detail_policy", None)
        if entry is not None and callable(policy):
            try:
                result = policy(run).fetch(
                    SOURCE, url, identity=entry["id"], query_remaining=request_limit - requested,
                    facts={field: entry.get(field) for field in ("price", "year", "mileage", "fuel", "transmission")},
                )
            except BaseException as error:
                requested += getattr(error, "detail_policy_requests", 0)
                raise
            requested += result.requests
            response, fetch_id = result.response, result.fetch_id
            if response.error or response.status != 200 or not response.body:
                raise ValueError(response.error or f"http_{response.status}")
            return response, fetch_id
        if requested >= request_limit:
            raise ValueError("request_limit")
        requested += 1
        response = collector.fetcher.get(url)
        fetch_id = collector.fetch_record(run, SOURCE, response, retain_raw=True)
        if response.error or response.status != 200 or not response.body:
            raise ValueError(response.error or f"http_{response.status}")
        return response, fetch_id

    page_number = 1
    while page_number <= min(last_page, page_limit):
        try:
            response, fetch_id = fetch(kleinanzeigen_page_url(query["url"], page_number))
            page = parse_kleinanzeigen(response.body, response.final_url)
            if page.page != page_number:
                if page.page == 50 and page_number > 50:
                    ledger["errors"].append("native_source_cap:50")
                raise ValueError(f"native_page_requested_mismatch:{page_number}:{page.page}")
            if page.total is None:
                raise ValueError("search_counter_or_route_unverified")
            if expected is None:
                expected, last_page = page.total, page.pages
            elif page.total != expected:
                ledger["errors"].append(f"counter_changed:{expected}:{page.total}")
            record = {
                "page": page_number,
                "url": response.final_url,
                "fetch_id": fetch_id,
                "declared_range": page.search_filters["declared_range"],
                "range_valid": page.search_filters["range_valid"],
                "ids": [x.external_id for x in page.listings],
                "promoted_ids": [
                    x.external_id for x in page.listings if x.raw["placement"] == "promoted"
                ],
                "organic_candidate_ids": [
                    x.external_id
                    for x in page.listings
                    if x.raw["placement"] in {"organic", "unknown"}
                ],
                "organic_ids": [
                    x.external_id
                    for x in page.listings
                    if (
                        x.raw["placement"] == "organic"
                        or (x.raw.get("native_preview") or {}).get("organic_present")
                    )
                    and x.raw["promotion_verified"]
                ],
                "unknown_placement_ids": [
                    x.external_id for x in page.listings if not x.raw["promotion_verified"]
                ],
                "promotion_error": page.search_filters["promotion_error"],
            }
            ledger["pages"].append(record)
            for item in page.listings:
                entry = ledger["cards"].setdefault(
                    item.external_id,
                    {
                        "id": item.external_id,
                        "url": item.url,
                        "title": item.title,
                        "model_scope": item.scope,
                        "market_country": "UNKNOWN",
                        "price": item.price,
                        "price_type": item.price_type,
                        "year": getattr(item, "year", None),
                        "mileage": getattr(item, "mileage", None),
                        "fuel": getattr(item, "fuel", None),
                        "transmission": getattr(item, "transmission", None),
                        "raw": item.raw,
                        "seen_on_pages": [],
                    },
                )
                entry["seen_on_pages"].append(
                    {"page": page_number, "fetch_id": fetch_id, "placement": item.raw["placement"]}
                )
            if not record["range_valid"]:
                ledger["errors"].append("invalid_declared_range")
                break
            start, end, _ = record["declared_range"]
            if record["promotion_error"]:
                ledger["errors"].append(
                    f"native_promotion_unverified:{page_number}:{record['promotion_error']}"
                )
            if len(record["organic_ids"]) != end - start + 1:
                ledger["errors"].append(f"organic_range_not_reconciled:{page_number}")
            if not page.listings:
                ledger["errors"].append("empty_before_confirmed_boundary")
                break
            persist()
        except Exception as error:  # noqa: BLE001 — retain partial discovery and continue with details
            ledger["errors"].append(f"search:{page_number}:{error}")
            break
        page_number += 1
    if last_page > page_limit:
        ledger["errors"].append("page_limit")
    relevant = [x for x in ledger["cards"].values() if x["model_scope"] == "needs_confirmation"]
    for entry in relevant[:detail_limit]:
        if requested >= request_limit and not callable(getattr(collector, "specialized_detail_policy", None)):
            ledger["errors"].append("request_limit")
            break
        identity = entry["id"]
        attempted += 1
        try:
            response, fetch_id = fetch(entry["url"], entry)
            if getattr(response, "detail_policy_accepted", False):
                ledger["detail_results"][identity] = {
                    "status": "observed", "cached": True, "fetch_id": fetch_id,
                    "fetched_at": response.timestamp, "market_country": "DE",
                    "country_evidence": response.detail_policy_item["raw"]["country_evidence"],
                }
                observed += 1
                persist()
                continue
            if _ad_id(response.final_url) != identity:
                raise ValueError("detail_redirect_identity_mismatch")
            own = parse_kleinanzeigen(response.body, response.final_url)
            if len(own.listings) != 1 or own.listings[0].external_id != identity:
                raise ValueError("detail_own_identity_unverified")
            item = own.listings[0]
            if item.raw.get("market_country") != "DE":
                raise ValueError("detail_country_unverified_or_foreign")
            state = "excluded_model"
            if item.scope in {"target", "needs_confirmation"}:
                listing_id = collector._observe(
                    run, item, response.timestamp, fetch_id, key
                )
                if callable(getattr(collector, "specialized_detail_policy", None)):
                    listing_id = collector.specialized_detail_policy(
                        run
                    ).accept_observation(
                        item, response.timestamp, fetch_id, listing_id, key
                    )
                state = "observed" if listing_id else "not_observed"
                observed += state == "observed"
                if (
                    listing_id
                    and item.links
                    and callable(getattr(collector, "queue_details", None))
                ):
                    collector.queue_details(item, listing_id)
            ledger["detail_results"][identity] = {
                "status": state,
                "fetch_id": fetch_id,
                "market_country": "DE",
                "country_evidence": item.raw["country_evidence"],
            }
        except Exception as error:  # noqa: BLE001 — foreign/unknown country remains discovery only
            from .bmw_detail_policy import DetailDeferred, deferred_record

            ledger["detail_results"][identity] = deferred_record(error, entry["url"]) if isinstance(error, DetailDeferred) else {"status": "unconfirmed", "error": str(error)}
            ledger["errors"].append(f"detail:{identity}:{error}")
        persist()
    if len(relevant) > detail_limit:
        ledger["errors"].append("detail_limit")
    ledger["pending_details"] = {
        entry["id"]: ledger["detail_results"].get(entry["id"], {"status": "deferred", "url": entry["url"], "reason": "query_detail_limit"})
        for entry in relevant
        if ledger["detail_results"].get(entry["id"], {}).get("status") not in {"observed", "excluded_model"}
    }
    received = len(ledger["cards"])
    organic_ids = sorted({identity for page in ledger["pages"] for identity in page["organic_ids"]})
    organic_count = len(organic_ids)
    if expected is None or organic_count != expected:
        ledger["errors"].append(f"organic_unique_count_not_reconciled:{organic_count}:{expected}")
    details_reconciled = all(
        ledger["detail_results"].get(entry["id"], {}).get("status")
        in {"observed", "excluded_model"}
        for entry in relevant
    )
    if query.get("pagination_certified") is not True:
        ledger["errors"].append("pagination_not_certified")
    status = (
        "complete"
        if not ledger["errors"] and len(ledger["pages"]) == last_page and details_reconciled
        else "partial"
    )
    ledger.update(
        expected_count=expected,
        received_count=received,
        organic_count=organic_count,
        organic_ids=organic_ids,
        details_reconciled=details_reconciled,
        target_count=observed,
        detail_attempts=attempted,
        requests=requested,
        status=status,
        finished_at=now(),
    )
    persist()
    collector.db.finish_source(
        run,
        SOURCE,
        key,
        started,
        status,
        expected,
        received,
        observed,
        len(ledger["pages"]),
        ";".join(ledger["errors"]) or None,
        infer_missing=False,
    )
    if ledger["errors"]:
        collector.failures.append("kleinanzeigen_partial:" + key)
    return ledger
