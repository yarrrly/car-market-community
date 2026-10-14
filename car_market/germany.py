"""Country, publication evidence and isolation of the German collection scope."""

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from .identity import normalized_vin
from .parse import Page, number

DE_SOURCES = {
    "mobile_de",
    "autoscout24_de",
    "autouncle_de",
    "kleinanzeigen_de",
    "bmw_de",
    "autohero_de",
    "pkw_de",
    "heycar_de",
    "markt_de",
    "quoka_de",
    "gebrauchtwagen_de",
    "instamotion_de",
    "carwow_de",
    "mercedes_benz_de",
}
COUNTRIES = {"DE", "D", "GERMANY", "DEUTSCHLAND", "DEU"}


def source_for_url(url):
    host = (urlsplit(url).hostname or "").lower()
    if host == "vehicle-selection.prod.stolo.eu-central-1.aws.bmw.cloud":
        return "bmw_de"
    if host == "www.instamotion.com":
        return "instamotion_de"
    if host == "angebote.carwow.de":
        return "carwow_de"
    for domain, source in {
        "mobile.de": "mobile_de",
        "autoscout24.de": "autoscout24_de",
        "autoscout24.com": "autoscout24_de",
        "autouncle.de": "autouncle_de",
        "kleinanzeigen.de": "kleinanzeigen_de",
        "bmw.de": "bmw_de",
        "autohero.com": "autohero_de",
        "pkw.de": "pkw_de",
        "heycar.de": "heycar_de",
        "markt.de": "markt_de",
        "quoka.de": "quoka_de",
        "12gebrauchtwagen.de": "gebrauchtwagen_de",
    }.items():
        if host == domain or host.endswith("." + domain):
            return source
    return "dealer:" + host


def canonical_de_id(source, url):
    parsed = urlsplit(url)
    if (parsed.scheme or parsed.netloc) and (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or source_for_url(url) != source
    ):
        return None
    if source == "mobile_de":
        if parsed.scheme or parsed.netloc:
            from .germany_mobile import mobile_ad_id

            return mobile_ad_id(url)
        candidate = parse_qs(parsed.query).get("id", [""])[0]
        if candidate.isdigit():
            return candidate
        match = re.search(
            r"/(?:details|vehicle|car)/[^?]*?(\d{6,})(?:\.html)?/?$", parsed.path
        )
    elif source == "autoscout24_de":
        if parsed.scheme:
            from .germany_autoscout import _own_id

            return _own_id(url)
        if not parsed.scheme and re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", parsed.path, re.IGNORECASE
        ):
            return parsed.path.lower()
        prefix = "angebote" if (parsed.hostname or "").endswith(".de") else "offers"
        match = re.fullmatch(
            r"/"
            + prefix
            + r"/(?:[^/]*-)?([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?",
            parsed.path,
            re.IGNORECASE,
        )
        return match.group(1).lower() if match else None
    elif source == "dealer:fahrzeugmarkt.auto-straussberger.de":
        from .germany_mobile import mobile_dealer_ad_id

        return mobile_dealer_ad_id(url)
    elif source in {"instamotion_de", "carwow_de"}:
        if not parsed.scheme and not parsed.netloc:
            pattern = r"[1-9]\d*" if source == "instamotion_de" else r"[a-f0-9]{32}"
            return parsed.path.lower() if re.fullmatch(pattern, parsed.path) else None
        from .germany_additional import _public

        if not _public(source, url):
            return None
        pattern = (
            r"/de/auto/([1-9]\d*)/[^/]+"
            if source == "instamotion_de"
            else r"/deals/([a-f0-9]{32})"
        )
        own = re.fullmatch(pattern, parsed.path)
        return own[1] if own else None
    elif source == "markt_de":
        from .germany_additional_public import _markt_id

        if not parsed.scheme and not parsed.netloc:
            return parsed.path if re.fullmatch(r"[a-f0-9]{8}", parsed.path) else None
        return _markt_id(url)
    elif source == "autouncle_de":
        match = re.search(r"/d/(\d+)", parsed.path)
    elif source == "kleinanzeigen_de":
        match = re.search(r"/(\d+)-\d+-\d+/?$", parsed.path)
    elif source == "bmw_de":
        if not parsed.scheme and not parsed.netloc:
            return (
                parsed.path.lower()
                if re.fullmatch(
                    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                    parsed.path,
                    re.IGNORECASE,
                )
                else None
            )
        from .germany_bmw import bmw_publication_id

        return bmw_publication_id(url)
    elif source in {"gebrauchtwagen_de", "quoka_de"}:
        from .germany_discovery import discovery_id

        return discovery_id(source, url)
    elif source in ("pkw_de", "autohero_de"):
        if not parsed.scheme and not parsed.netloc:
            pattern = (
                r"\d+"
                if source == "pkw_de"
                else r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
            )
            return (
                parsed.path.lower()
                if re.fullmatch(pattern, parsed.path, re.IGNORECASE)
                else None
            )
        from .germany_secondary import _own_id

        return _own_id(url, source)
    else:
        match = None
    return (
        match.group(1).lower()
        if match
        else urlunsplit(
            (
                parsed.scheme,
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                parsed.query,
                "",
            )
        )
    )


def validate_market_path(path, country):
    """Validate an existing database before construction, migrations or any writes."""
    path = Path(path).resolve()
    for reserved_country, directory in {
        "NL": Path.home() / "src/first-car-nl",
        "DE": Path.home() / "src/first-car-de",
    }.items():
        if country != reserved_country and path.is_relative_to(directory.resolve()):
            raise ValueError(
                "Market country does not match the reserved data directory"
            )
    if not path.exists():
        return
    # Online WAL reads require the journal; standalone backup files have none.
    # immutable avoids creating sidecars merely to validate an export backup.
    wal = Path(str(path) + "-wal")
    mode = "mode=ro" if wal.exists() and wal.stat().st_size else "mode=ro&immutable=1"
    with closing(sqlite3.connect(path.as_uri() + "?" + mode, uri=True)) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        generic_negative = False
        if "monitor_state" in tables:
            record = conn.execute(
                "SELECT value FROM monitor_state WHERE key='market_scope'"
            ).fetchone()
            if record and json.loads(record[0]).get("country") != country:
                raise ValueError("Market country does not match the existing database")
            generic_negative = bool(
                record
                and json.loads(record[0]).get("model_profile") == "configured_vehicle"
            )
        if "listings" not in tables:
            return
        sources = {r[0] for r in conn.execute("SELECT DISTINCT source FROM listings")}
        if country == "DE" and any(
            s not in DE_SOURCES and not s.startswith("dealer:") for s in sources
        ):
            raise ValueError(
                "German configuration cannot write an existing non-German market database"
            )
        if country == "DE" and "observations" in tables:
            uncertain = conn.execute(
                """SELECT 1 FROM listings l LEFT JOIN observations o ON o.id=(
                SELECT n.id FROM observations n WHERE n.listing_id=l.id
                ORDER BY julianday(n.observed_at) DESC,n.id DESC LIMIT 1)
                WHERE o.id IS NULL OR NOT json_valid(o.details_json)
                OR CASE WHEN json_valid(o.details_json) THEN
                  COALESCE(json_extract(o.details_json,'$.raw.market_country'),'')<>'DE'
                  AND NOT (? AND l.scope='excluded' AND o.scope='excluded'
                    AND json_extract(o.details_json,'$.raw.model_profile')='configured_vehicle'
                    AND COALESCE(json_extract(o.details_json,'$.raw.market_country'),'')<>'')
                  ELSE 1 END
                LIMIT 1""",
                (generic_negative,),
            ).fetchone()
            if uncertain:
                raise ValueError("Existing publication lacks own German country proof")
        if country != "DE" and any(s in DE_SOURCES for s in sources):
            raise ValueError(
                "Non-German configuration cannot write a German market database"
            )
        if (
            country == "DE"
            and "vehicles" in tables
            and conn.execute(
                "SELECT 1 FROM vehicles WHERE kenteken IS NOT NULL LIMIT 1"
            ).fetchone()
        ):
            raise ValueError("German market cannot use registration plates as identity")


def bind_market(db, config):
    from .model_profiles import MERCEDES_PROFILE, config_profile

    if config_profile(config) in {MERCEDES_PROFILE, "configured_vehicle"}:
        return
    country = config.get("scope", {}).get("country")
    if country == "DE":
        validate_market_path(db.path, country)
        db.conn.execute(
            "INSERT OR REPLACE INTO monitor_state VALUES ('market_scope',?)",
            (json.dumps({"country": country}),),
        )
        db.conn.execute("DROP VIEW IF EXISTS purchase_candidates")
        db.conn.execute("""CREATE VIEW purchase_candidates AS
            SELECT m.* FROM market_current m JOIN latest_observations o ON o.listing_id=m.listing_id
            WHERE m.scope='target' AND m.availability='observed' AND m.price_eur>0
            AND m.price_type IN ('advertised_price','FIXED')
            AND json_extract(o.details_json,'$.raw.price_warning') IS NULL""")
        db.conn.commit()


def qualify_german_item(item):
    """Advertising text about other models does not change the listed vehicle model."""
    from .model_profiles import is_mercedes, qualify_mercedes

    from .search_profile import is_configured, qualify

    if is_configured(item):
        return qualify(item)
    if is_mercedes(item):
        return qualify_mercedes(item, "DE")
    raw = item.raw
    raw["market_context_country"] = "DE"
    if item.kenteken:
        raw.setdefault("registration_identifier", item.kenteken)
    item.kenteken = None
    item.vin = normalized_vin(item.vin)
    text = " ".join(
        str(v or "")
        for v in [
            item.title,
            raw.get("model_brand"),
            raw.get("model_family"),
            raw.get("model_variant"),
        ]
    ).lower()
    vehicle = raw.get("vehicle") if isinstance(raw.get("vehicle"), dict) else {}
    brand = raw.get("model_brand") or vehicle.get("make")
    brand = brand.get("name") if isinstance(brand, dict) else brand
    is_bmw = str(brand).upper() == "BMW" if brand else bool(re.search(r"\bbmw\b", text))
    other = bool(
        re.search(
            r"\b(?:216|220|223|225|230)\s*(?:i|d|e|xe)\b|\b218\s*d\b|gran\s*(?:tour|coup)",
            text,
        )
    )
    active = bool(re.search(r"active\s*[- ]?\s*tour", text))
    exact = bool(re.search(r"\b218\s*i\b", text))
    if not is_bmw or other or item.fuel in ("diesel", "hybrid"):
        item.scope = "excluded"
    elif active and exact:
        item.scope = "target" if item.fuel == "petrol" else "needs_confirmation"
    elif active and (
        re.search(r"\b218\b", text) or raw.get("model_status") == "motor_unconfirmed"
    ):
        item.scope = "needs_confirmation"
    else:
        item.scope = "excluded"
    if raw.get("offer_role") == "wanted" or re.match(
        r"^(?:suche|gesucht|kaufgesuch|ankauf|wir\s+kaufen)\b",
        item.title.strip(),
        re.IGNORECASE,
    ):
        raw["offer_role"] = "wanted"
        item.scope = "excluded"
    country = str(raw.get("market_country") or "").upper()
    if country and country not in COUNTRIES:
        raw["country_excluded"] = country
        item.scope = "excluded"
    elif item.scope != "excluded" and not country:
        raw["geography_status"] = "needs_confirmation"
        item.scope = "needs_confirmation"
    elif country in COUNTRIES:
        raw["market_country"] = "DE"
    own_year = re.search(
        r"\bErstzulassung\s*:\s*(?:\d{1,2}[./]\d{1,2}[./]|\d{1,2}[./])?(\d{4})\b",
        item.description or "",
        re.IGNORECASE,
    )
    if own_year and item.year and int(own_year.group(1)) != item.year:
        raw.setdefault("field_conflicts", {})["year"] = {
            "structured": item.year,
            "own_description": int(own_year.group(1)),
            "evidence": own_year.group(0),
        }
    classify_german_price(item)
    return item


def classify_german_price(item):
    raw = item.raw
    currency = str(raw.get("price_currency") or "EUR").upper()
    if currency in {"UNKNOWN", "CONFLICT"}:
        raw.update(
            advertised_amount=item.price,
            price_currency=currency,
            price_warning=(
                "currency_conflict" if currency == "CONFLICT" else "currency_unconfirmed"
            ),
        )
        item.price = None
        item.price_type = "currency_unconfirmed"
        return item
    if currency != "EUR":
        raw.update(
            advertised_amount=item.price,
            price_currency=currency,
            price_warning="non_eur_currency",
        )
        item.price = None
        item.price_type = "foreign_currency"
        return item
    contract = re.search(
        r"\b(?:Mietvertrags[uü]bertragung|Leasing(?:vertrags)?[uü]bernahme|Leasingvertrags[uü]bertragung)\b",
        item.title,
        re.IGNORECASE,
    ) or re.search(
        r"\b(?:meinen|den|einen)\s+(?:verbleibenden\s+)?Leasingvertrag\b.{0,160}\b[uü]bertragen\b",
        item.description or "",
        re.IGNORECASE,
    )
    if contract:
        raw.update(
            price_warning="lease_transfer",
            offer_role="lease_transfer",
            advertised_amount_eur=item.price,
            lease_transfer_evidence=contract.group(0),
        )
        item.price_type = "lease_transfer"
        return item
    warning = None
    if item.price_type not in ("advertised_price", "FIXED"):
        return item
    if raw.get("price_is_net") or raw.get("net_only"):
        warning = "net_price"
    elif raw.get("price_is_monthly") is True or (
        isinstance(raw.get("leasing"), dict)
        and item.price is not None
        and number(raw["leasing"].get("monthlyRate")) == item.price
    ):
        warning = "monthly_price"
    text = item.title + " " + (item.description or "")
    patterns = [
        ("export_price", r"\bExportpreis\s*[:=]?\s*(?:€|EUR)?\s*(\d[\d.,]*)"),
        (
            "export_price",
            r"(?:€\s*)?(\d[\d.,]*)\s*(?:€|EUR)?\s*(?:nur\s+(?:f[uü]r\s+)?Export|Exportpreis)",
        ),
        ("deposit_price", r"\bAnzahlung\s*[:=]?\s*(?:€|EUR)?\s*(\d[\d.,]*)"),
        (
            "monthly_price",
            r"(?:€\s*)?(\d[\d.,]*)\s*(?:€|EUR)?\s*(?:/\s*(?:Monat|mtl)|(?:pro|je)\s+Monat|monatlich|mtl\.?)",
        ),
        ("net_price", r"\bNettopreis\s*[:=]?\s*(?:€|EUR)?\s*(\d[\d.,]*)"),
        (
            "net_price",
            r"(?:€\s*)?(\d[\d.,]*)\s*(?:€|EUR)?\s*(?:netto|(?:zzgl|exkl|excl)\.?\s*(?:19\s*%\s*)?MwSt)",
        ),
    ]
    for kind, pattern in patterns:
        if any(
            item.price is not None and number(m.group(1)) == item.price
            for m in re.finditer(pattern, text, re.IGNORECASE)
        ):
            warning = kind
            break
    if warning:
        raw["price_warning"] = warning
        raw["advertised_amount_eur"] = item.price
        item.price_type = warning
    return item


def parse_germany_page(source, body, url):
    if source in {"instamotion_de", "carwow_de", "markt_de", "facebook_de"}:
        from .germany_additional import parse_additional

        page = parse_additional(source, body, url)
    elif source == "dealer:fahrzeugmarkt.auto-straussberger.de":
        from .germany_mobile import parse_mobile_dealer

        page = parse_mobile_dealer(body, url)
    elif (
        source == "dealer:autoland.de"
        and urlsplit(url).path.rstrip("/") == "/bmw/2er-reihe"
    ):
        from .germany_dealer_inventory import parse_dealer_inventory

        page = parse_dealer_inventory(body, url)
    elif source == "autoscout24_de":
        from .germany_autoscout import parse_autoscout_de

        page = parse_autoscout_de(body, url)
    elif source == "mobile_de":
        from .germany_mobile import parse_mobile

        page = parse_mobile(body, url)
    elif source == "kleinanzeigen_de":
        from .germany_kleinanzeigen import parse_kleinanzeigen

        page = parse_kleinanzeigen(body, url)
    elif source == "bmw_de":
        from .germany_bmw import parse_bmw

        page = parse_bmw(body, url)
    elif source in {"gebrauchtwagen_de", "quoka_de"}:
        from .germany_discovery import parse_discovery

        page = parse_discovery(source, body, url)
    elif source in (
        "autouncle_de",
        "kleinanzeigen_de",
        "bmw_de",
        "autohero_de",
        "pkw_de",
        "gebrauchtwagen_de",
    ):
        from .germany_secondary import parse_secondary

        page = parse_secondary(source, body, url)
    elif source.startswith("dealer:"):
        from .germany_dealers import parse_dealer

        page = parse_dealer(source, body, url)
    else:
        return Page([])
    for item in page.listings:
        qualify_german_item(item)
    return page


def german_detail_identity_matches(source, item_url, final_url, body, advertisement_id):
    if source_for_url(item_url) != source or source_for_url(final_url) != source:
        return False
    expected = canonical_de_id(source, final_url)
    if expected is None:
        return False
    if (
        not advertisement_id
        or canonical_de_id(source, str(advertisement_id)) != expected
    ) and str(advertisement_id) != expected:
        return False
    page = parse_germany_page(source, body, final_url)
    valid = [
        item
        for item in page.listings
        if item.external_id == expected
        and canonical_de_id(source, item.url) == expected
    ]
    if (
        len(page.listings) != 1
        or len(valid) != 1
        or valid[0].raw.get("market_country") != "DE"
    ):
        return False
    from bs4 import BeautifulSoup

    canonicals = BeautifulSoup(body, "html.parser").select(
        'link[rel~="canonical"][href]'
    )
    if len(canonicals) > 1:
        return False
    if canonicals:
        declared = urljoin(final_url, canonicals[0]["href"])
        if (
            source_for_url(declared) != source
            or canonical_de_id(source, declared) != expected
        ):
            return False
    return canonical_de_id(source, item_url) == expected


@contextmanager
def observation_transaction(db):
    if not db._in_transaction and not db.conn.in_transaction:
        with db.transaction():
            yield
    else:
        original_mode = db._in_transaction
        db._in_transaction = True
        db.conn.execute("SAVEPOINT german_observation")
        try:
            yield
        except BaseException:
            db.conn.execute("ROLLBACK TO SAVEPOINT german_observation")
            db.conn.execute("RELEASE SAVEPOINT german_observation")
            raise
        else:
            db.conn.execute("RELEASE SAVEPOINT german_observation")
        finally:
            db._in_transaction = original_mode


def observe_german_item(db, run, item, stamp, fetch_id=None, key=None):
    qualify_german_item(item)
    if item.raw.get("market_country") != "DE":
        raise ValueError(
            "Own German country proof is required before identity or observation writes"
        )
    if (
        item.scope == "excluded"
        and not db.conn.execute(
            "SELECT 1 FROM listings WHERE source=? AND external_id=?",
            (item.source, item.external_id),
        ).fetchone()
    ):
        return None
    with observation_transaction(db):
        old = db.conn.execute(
            "SELECT availability,last_seen FROM listings WHERE source=? AND external_id=?",
            (item.source, item.external_id),
        ).fetchone()
        listing = db.observe(run, item, stamp, fetch_id, key)
        from .provenance import timestamp

        declared = item.raw.get("availability_status")
        older = bool(old and timestamp(stamp) < timestamp(old["last_seen"]))
        if older:
            state = old["availability"]
        elif item.raw.get("sold") is True or declared == "sold":
            state = "seller_marked_sold"
        elif item.raw.get("reserved") is True or declared == "reserved":
            state = "seller_reserved"
        elif (
            old
            and old["availability"] in ("seller_marked_sold", "seller_reserved")
            and declared != "available"
        ):
            state = old["availability"]
        else:
            state = None
        if state:
            db.conn.execute(
                "UPDATE listings SET availability=? WHERE id=?", (state, listing)
            )
    return listing
