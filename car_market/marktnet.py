"""Public XML API: https://www.marktnet.nl/info/api."""

import html
import math
import re

from defusedxml import ElementTree as ET

from .dom import explicit_gearbox
from .parse import Listing, Page, fuel, number, plate_from_text

NAMESPACE = {"os": "http://a9.com/-/spec/opensearchrss/1.0/"}


def stated_fuel(description):
    match = re.search(
        r"\bBrandstof(?:soort)?\s*:\s*([^:\n]{1,100})",
        description,
        re.IGNORECASE,
    )
    if not match:
        return None
    value = re.split(
        r"\s+(?:Tankinhoud|Transmissie|Aandrijving|Vermogen|Aantal|Motorinhoud|Kleur|"
        r"Kenteken|Bouwjaar|Energielabel|Carrosserie\w*|Milieu)\b",
        match.group(1),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    return fuel(value)


def listing(node, detail=False):
    identity = node.get("id") if detail else node.findtext("guid")
    description = node.findtext("body" if detail else "description") or ""
    description = re.sub(r"<[^>]*>", " ", html.unescape(description))
    year = re.search(r"Bouwjaar:\s*(?:[a-z.]+\s*)?(\d{4})", description, re.IGNORECASE)
    mileage = re.search(r"Tellerstand:\s*([\d.]+)\s*km", description, re.IGNORECASE)
    money = node.findtext("price") if detail else re.search(r"Prijs:\s*€\s*([\d.,]+)", description)
    money = money.group(1) if hasattr(money, "group") else money
    image = node.find("enclosure")
    truncated = description.rstrip().endswith(("...", "…"))
    return Listing(
        "marktnet",
        str(identity),
        node.findtext("url" if detail else "link") or "https://www.marktnet.nl/" + str(identity),
        node.findtext("title") or "",
        price=number(money),
        year=int(year.group(1)) if year else None,
        mileage=number(mileage.group(1)) if mileage else None,
        fuel=stated_fuel(description),
        transmission=explicit_gearbox(description),
        kenteken=plate_from_text(description),
        description=description,
        seller=node.findtext("user/name") if detail else None,
        city=node.findtext("geo/city") if detail else None,
        images=[x.text.strip() for x in node.findall("images/image") if x.text and x.text.strip()]
        if detail
        else ([image.get("url")] if image is not None and image.get("url") else []),
        links=[]
        if detail
        else [
            {
                "url": "https://www.marktnet.nl/advertentie.xml?id=" + str(identity),
                "label": "full_description",
            }
        ],
        raw={
            "public_xml": True,
            "detail_complete": detail and not truncated,
            "description_may_be_truncated": truncated,
        },
    ).qualify()


def parse_xml(body):
    root = ET.fromstring(body.lstrip("\ufeff \r\n\t"))
    if root.tag == "ad":
        return Page([listing(root, detail=True)])
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ValueError("Marktnet XML inventory unavailable")
    total = channel.findtext("os:totalResults", namespaces=NAMESPACE)
    size = int(channel.findtext("os:itemsPerPage", "20", namespaces=NAMESPACE))
    start = int(channel.findtext("os:startIndex", "1", namespaces=NAMESPACE))
    total = int(total) if total is not None else None
    return Page(
        [listing(node) for node in channel.findall("item")],
        total=total,
        pages=math.ceil(total / max(1, size)) if total is not None else None,
        page=(start - 1) // max(1, size) + 1,
    )
