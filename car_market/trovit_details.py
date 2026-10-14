"""Parse the retained Trovit /listing/ DOM with independent own-ad identity."""

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .parse import Listing, Page, fuel, gearbox, number, plate

CAPTCHA_REFUSAL = "trovit_captcha_refusal"


def is_trovit_captcha_route(url):
    """Recognize only the retained public Trovit challenge route."""
    try:
        if not isinstance(url, str) or any(ord(char) <= 32 for char in url):
            return False
        parsed = urlsplit(url)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname == "autos.trovit.nl"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and (
                parsed.path == "/index.php/cod.captcha_check"
                or parsed.path.startswith("/index.php/cod.captcha_check/")
            )
        )
    except (TypeError, ValueError):
        return False


def trovit_captcha_refusal(body, final_url):
    """An own challenge form is a refusal, not a successful vehicle response."""
    if not isinstance(body, str) or not is_trovit_captcha_route(final_url):
        return False
    soup = BeautifulSoup(body, "html.parser")
    if soup.select_one("body#atpage.cars[data-id]"):
        return False
    forms = [
        form
        for form in soup.select("form[action]")
        if str(form.get("method", "")).strip().lower() == "post"
        and is_trovit_captcha_route(urljoin(final_url, form["action"]))
        and len(form.select(".g-recaptcha")) == 1
    ]
    return len(forms) == 1


def _one(nodes):
    if len(nodes) != 1:
        raise ValueError("missing or ambiguous own block")
    return nodes[0]


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def parse_trovit_detail(body: str, url: str) -> Page | None:
    """Return Page or None; recommendations and unsupported bodies fail closed."""
    parsed = urlsplit(url)
    route = re.fullmatch(r"/listing/[^/]+\.([A-Za-z0-9_]+)\/?", parsed.path)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname != "autos.trovit.nl"
        or not route
    ):
        return None
    own_id = route[1]
    soup = BeautifulSoup(body, "html.parser")
    try:
        envelope = _one(soup.select("body#atpage.cars[data-id]"))
        if envelope["data-id"] != own_id:
            return None
        root = _one(envelope.select("#content_main > .ap_traffic"))
        info = _one(root.select(":scope > #main_info"))
        title = _text(_one(root.select(":scope > h1")))
        if not title:
            return None
        image = _one(info.select('#photos [data-test="hero-image"] > img[src]'))
        photo = urljoin(url, image["src"])
        image_url = urlsplit(photo)
        if (
            image_url.scheme not in {"http", "https"}
            or image_url.hostname != "img-nl-2.trovit.com"
            or not re.fullmatch(
                rf"/{re.escape(own_id)}/{re.escape(own_id)}\.\d+_\d+\.jpg",
                image_url.path,
            )
        ):
            return None
        script_ids = [
            m[1]
            for node in soup.select("script:not([src])")
            for m in re.finditer(
                r"\btrovit\.adId\s*=\s*[\"\']([A-Za-z0-9_]+)[\"\']", node.get_text()
            )
        ]
        if script_ids and set(script_ids) != {own_id}:
            return None
        description_node = _one(root.select(":scope > #description > p"))
        description = _text(description_node)
        specs = {}
        for line in description_node.get_text("\n", strip=True).splitlines():
            match = re.fullmatch(r"([^:]+):\s*(.+)", line.strip())
            if not match:
                continue
            key, value = match.groups()
            if key in specs and specs[key] != value:
                raise ValueError("conflicting own specs")
            specs[key] = value
        registration = specs.get("Kenteken")
        if registration is not None and not re.fullmatch(
            r"[A-Z0-9]{1,3}-[A-Z0-9]{1,3}-[A-Z0-9]{1,3}|[A-Z0-9]{6}", registration
        ):
            return None
        kenteken = plate(registration)
        if registration and not kenteken:
            return None
        year_match = re.fullmatch(
            r"(?:(?:jan|feb|mrt|apr|mei|jun|jul|aug|sep|okt|nov|dec)\.?\s+)?((?:19|20)\d{2})",
            specs.get("Bouwjaar", ""),
            re.IGNORECASE,
        )
        year = int(year_match[1]) if year_match else None
        mileage = number(specs.get("Tellerstand"))
        for prop in info.select(".properties > .property"):
            label, value = (
                _text(prop.select_one("span")),
                number(_text(prop.select_one("strong"))),
            )
            if label == "Jaar" and year is not None and value != year:
                return None
            if label == "Km" and mileage is not None and value != mileage:
                return None
        price_node = _one(info.select("#info > .price > .amount"))
        price = number(_text(price_node))
        if price is None or not _text(price_node).startswith("€"):
            return None
        address = _text(_one(info.select(".address > h2")))
        truncated = bool(re.search(r"(?:\.{3,}|…)\s*$", description))
        item = Listing(
            "trovit",
            own_id,
            url,
            title,
            price=price,
            mileage=mileage,
            year=year,
            transmission=gearbox(specs.get("Transmissie")),
            fuel=fuel(specs.get("Brandstofsoort")),
            kenteken=kenteken,
            description=description,
            city=address.split(",")[0].strip() or None,
            images=[photo],
            raw={
                "detail_adapter": "trovit_own_listing_dom",
                "specs": specs,
                "identity_evidence": {
                    "listing_id": own_id,
                    "body_data_id": envelope["data-id"],
                    "hero_image": photo,
                },
                "description_source": "own_listing_dom",
                "description_truncated": truncated,
                "detail_complete": bool(
                    description and len(description.split()) >= 8 and not truncated
                ),
                "upstream_portal": _text(info.select_one("#info > .source > .company"))
                or None,
                "address": address,
            },
        )
        return Page([item.qualify()], total=1, pages=1, page=1)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
