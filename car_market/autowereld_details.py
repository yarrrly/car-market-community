"""Own AutoWereld VDP data, verified against retained 2026-10-04–06 responses.

Opt-in adapter: None means reject; callers must not replace it with a related car.
No network, JavaScript execution, or clock-derived observation timestamps.
"""

import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .parse import Listing, Page, fuel, gearbox, number, plate


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def _own_id(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "autowereld.nl",
        "www.autowereld.nl",
    }:
        return None
    match = re.fullmatch(r"/[^/]+/[^/]+/[^/]+-(\d+)/(?:details|foto)\.html", parsed.path)
    return match.group(1) if match else None


def _template_string(value):
    """Decode a static JS template literal, without evaluating expressions.

    The three quoted-wheel-size archive cases need one JS escaping layer before
    json.loads. Deliberately reject interpolation and unknown escape sequences.
    """
    if "${" in value:
        raise ValueError("dynamic template")
    output = []
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v"}
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\":
            output.append(char)
            index += 1
            continue
        index += 1
        if index == len(value):
            raise ValueError("unfinished escape")
        char = value[index]
        if char in "\\`\"'/":
            output.append(char)
        elif char in escapes:
            output.append(escapes[char])
        elif char in {"u", "x"}:
            length = 4 if char == "u" else 2
            digits = value[index + 1 : index + 1 + length]
            if len(digits) != length or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                raise ValueError("invalid character escape")
            output.append(chr(int(digits, 16)))
            index += length
        elif char == "\n":
            pass
        elif char == "\r":
            if value[index + 1 : index + 2] == "\n":
                index += 1
        else:
            raise ValueError("unsupported escape")
        index += 1
    return "".join(output)


def _occasion(soup):
    literals = []
    pattern = r"\bvar\s+occasionDetails\s*=\s*JSON\.parse\(`((?:\\.|[^\\`])*)`\)\s*;"
    for script in soup.find_all("script"):
        literals.extend(re.findall(pattern, script.string or "", re.DOTALL))
    if len(literals) != 1:
        return None
    try:
        result = json.loads(_template_string(literals[0]))
    except (ValueError, TypeError):
        return None
    return result if isinstance(result, dict) else None


def _specifications(root):
    specs = {}
    for row in root.select(":scope > .features table.specifications tr"):
        term, value = row.select_one("td.term"), row.select_one("td.value")
        if term and value:
            key, text = _text(term), _text(value)
            if key in specs and specs[key] != text:
                return None
            specs[key] = text
    return specs


def _plate_evidence(json_value, table_value):
    values = {"occasionDetails.kenteken": json_value, "own_table.Kenteken": table_value}
    normalized = {key: plate(value) for key, value in values.items()}
    valid = {value for value in normalized.values() if value}
    if len(valid) > 1:
        return None, {"values": values, "conflict": True}
    invalid = any(
        str(value or "").strip() not in {"", "-"} and normalized[key] is None
        for key, value in values.items()
    )
    if invalid:
        return None, {"values": values, "conflict": True}
    return next(iter(valid), None), {"values": values, "sources": normalized, "conflict": False}


def parse_autowereld_detail(body, url):
    """Return one verified own VDP Page, or None on missing/conflicting identity."""
    own_id = _own_id(url)
    if not own_id or not urlsplit(url).path.endswith("/details.html"):
        return None
    soup = BeautifulSoup(body, "html.parser")
    canonicals = soup.select("link[rel=canonical][href]")
    roots = soup.select("#maincontent > #global-info")
    node = _occasion(soup)
    if (
        len(canonicals) != 1
        or len(roots) != 1
        or node is None
        or str(node.get("nr")) != own_id
        or _own_id(urljoin(url, canonicals[0]["href"])) != own_id
    ):
        return None
    root = roots[0]
    specs = _specifications(root)
    if specs is None:
        return None
    heading = root.select_one(":scope > .info h1.title")
    own_links = heading.select('a[href*="/details.html"]') if heading else []
    if not heading or len(own_links) != 1 or _own_id(urljoin(url, own_links[0]["href"])) != own_id:
        return None
    kenteken, plate_evidence = _plate_evidence(node.get("kenteken"), specs.get("Kenteken"))
    if plate_evidence["conflict"]:
        return None
    conflicts = {}

    def value(field, key, term, normalize=number):
        from_json, from_table = normalize(node.get(key)), normalize(specs.get(term))
        if from_json is not None and from_table is not None and from_json != from_table:
            conflicts[field] = {"occasionDetails": node.get(key), "own_table": specs.get(term)}
            return None
        return from_table if from_table is not None else from_json

    description_node = soup.select_one("#maincontent > #occasion-extra-info > .extra-info-text")
    description = _text(description_node) or None
    truncated = bool(description and re.search(r"(?:\.{3,}|…)\s*$", description)) or bool(
        description_node
        and any(
            re.search(
                r"clamp|truncate|text-overflow\s*:\s*ellipsis",
                " ".join(n.get("class", [])) + " " + n.get("style", ""),
            )
            or n.get("data-truncated") == "true"
            for n in [
                description_node,
                *description_node.select("[class], [style], [data-truncated]"),
            ]
        )
    )
    provider = root.select_one(":scope > .provider > .user")
    seller = _text(provider.select_one(".name")) if provider else None
    zipcode = _text(provider.select_one(".zipcode")) if provider else ""
    city_match = re.fullmatch(r"\d{4}\s*[A-Z]{2}\s+(.+)", zipcode)
    images = []
    for anchor in soup.select("#maincontent > #photo-container a[href]"):
        if _own_id(urljoin(url, anchor["href"])) == own_id:
            images.extend(
                urljoin(url, image["src"])
                for image in anchor.select("img[src]")
                if urlsplit(urljoin(url, image["src"])).scheme in {"https", "http"}
            )
    item = Listing(
        "autowereld",
        url,
        url,
        _text(heading),
        price=value("price", "vraagprijs", "Prijs"),
        mileage=value("mileage", "kilometerstand", "Kilometerstand"),
        year=value("year", "bouwjaar", "Bouwjaar"),
        transmission=value("transmission", "transmissienaam", "Transmissie", gearbox),
        fuel=value("fuel", "brandstofnaam", "Brandstof", fuel),
        kenteken=kenteken,
        seller=seller or None,
        city=city_match.group(1) if city_match else None,
        description=description,
        images=list(dict.fromkeys(images)),
        raw={
            "detail_adapter": "autowereld_own_vdp",
            "identity_evidence": {"nr": own_id, "canonical": canonicals[0]["href"], "url": url},
            "plate_evidence": plate_evidence,
            "specs": specs,
            "field_conflicts": conflicts,
            "description_source": "autowereld_own_extra_info",
            "photos_source": "autowereld_own_preview",
            "description_truncated": truncated,
            "detail_complete": bool(
                description and len(description.split()) >= 8 and not truncated
            ),
        },
    ).qualify()
    if item.price is None:
        item.price_type = "unknown"
    return Page([item], total=1, pages=1, page=1)
