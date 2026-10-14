"""Exact retained dealer identities survive owned UI presentation changes."""

import copy
import gzip
import hashlib
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from car_market.germany_dealer_inventory import _devalue, parse_wiveda_bundle
from car_market.mercedes_de_sources import parse_mercedes_de_sources
from car_market.model_profiles import MERCEDES_PROFILE, use_profile

FIXTURES = Path(__file__).parent / "fixtures" / "provider_owned_details"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
WELLER = [m for m in MANIFEST if m["kind"] == "weller_html"]


def retained(meta):
    encoded = (FIXTURES / meta["fixture"]).read_bytes()
    assert hashlib.sha256(encoded).hexdigest() == meta["fixture_sha256"]
    decoded = gzip.decompress(encoded)
    assert hashlib.sha256(decoded).hexdigest() == meta["decoded_sha256"]
    return decoded.decode("utf-8")


def wiveda_bundle():
    html = next(m for m in MANIFEST if m["kind"] == "wiveda_html")
    payload = next(m for m in MANIFEST if m["kind"] == "wiveda_payload")
    return {
        "kind": "wiveda_native",
        "html_url": html["url"],
        "html": retained(html),
        "payload_url": payload["url"],
        "payload": json.loads(retained(payload)),
    }


def native_field(bundle, field, value):
    payload = bundle["payload"]
    identity = bundle["html_url"].rsplit("/", 1)[-1]
    node = next(
        v
        for v in payload
        if isinstance(v, dict)
        and isinstance(v.get("id"), int)
        and payload[v["id"]] == identity
        and "title" in v
        and "description" in v
    )
    payload.append(value)
    node[field] = len(payload) - 1


def rewrite_schema(soup, change):
    for script in soup.select('script[type="application/ld+json"]'):
        doc = json.loads(script.get_text())
        changed = change(doc)
        if changed is not None:
            script.string = json.dumps(changed, ensure_ascii=False)


def weller_page(body, url):
    with use_profile(MERCEDES_PROFILE, "DE"):
        page = parse_mercedes_de_sources("dealer:wellergruppe.de", body, url)
        for item in page.listings:
            item.qualify()
        return page


def test_wiveda_exact_owned_subtitle_presentation_keeps_full_native_variant():
    bundle = wiveda_bundle()
    identity = bundle["html_url"].rsplit("/", 1)[-1]
    record = _devalue(bundle["payload"])["vehicle-" + identity]
    assert record["variant"] == "218i ACTIVE TOUR*TURBO+KETTE NEU*AUTO*KEYLES"

    page = parse_wiveda_bundle(bundle, bundle["html_url"])

    assert len(page.listings) == 1
    item = page.listings[0]
    assert item.scope == "target" and item.raw["market_country"] == "DE"
    assert item.raw["model_variant"] == record["variant"]
    assert item.raw["native_vehicle"] == record
    assert item.raw["identity_evidence"]["native_vehicle_id"] == identity
    assert item.raw["country_evidence"]["vehicle_id"] == identity
    assert item.raw["country_evidence"]["payload_url"] == bundle["payload_url"]
    assert item.price == 7999 and item.mileage == record["mileage"]
    assert item.raw["detail_complete"] is True


@pytest.mark.parametrize(
    "field",
    [
        "canonical",
        "sku",
        "native_id",
        "native_country",
        "title",
        "dealer_id",
        "mileage",
        "description",
        "subtitle_approximate",
        "subtitle_wrong_segment",
        "subtitle_duplicate",
        "vehicle_root_duplicate",
    ],
)
def test_wiveda_presentation_exception_never_relaxes_owned_binding(field):
    bundle = wiveda_bundle()
    soup = BeautifulSoup(bundle["html"], "html.parser")
    if field == "canonical":
        soup.select_one('link[rel="canonical"]')["href"] = (
            "https://wiveda.de/fahrzeug/9ec24de5-36ee-4387-a4bc-b942bfc27e23"
        )
    elif field in {"sku", "mileage", "description"}:

        def change(doc):
            if not isinstance(doc, dict) or "Car" not in doc.get("@type", []):
                return None
            if field == "sku":
                doc["sku"] = "9ec24de5-36ee-4387-a4bc-b942bfc27e23"
            elif field == "mileage":
                doc["mileageFromOdometer"]["value"] += 1
            else:
                doc["description"] += " different owned description"
            return doc

        rewrite_schema(soup, change)
    elif field == "native_id":
        native_field(bundle, "id", "9ec24de5-36ee-4387-a4bc-b942bfc27e23")
    elif field == "native_country":
        native_field(bundle, "country", "NL")
    elif field == "title":
        soup.select_one("h1").string = "BMW 220 Active Tourer"
    elif field == "dealer_id":
        soup.select_one('[data-testid="contact-card-dealer-link"]')["href"] = (
            "/haendler/other"
        )
    elif field == "subtitle_approximate":
        soup.select_one(
            '[data-testid="contact-card-subtitle"]'
        ).string = "218i ACTIVE TOUR"
    elif field == "subtitle_wrong_segment":
        soup.select_one(
            '[data-testid="contact-card-subtitle"]'
        ).string = "218i ACTIVE TOUR · TURBO+KETTE NEU · MANUAL"
    elif field == "subtitle_duplicate":
        subtitle = soup.select_one('[data-testid="contact-card-subtitle"]')
        subtitle.insert_after(copy.copy(subtitle))
    elif field == "vehicle_root_duplicate":
        script = next(
            s
            for s in soup.select('script[type="application/ld+json"]')
            if "Car" in json.loads(s.get_text()).get("@type", [])
        )
        script.insert_after(copy.copy(script))
    bundle["html"] = str(soup)

    assert not parse_wiveda_bundle(bundle, bundle["html_url"]).listings


@pytest.mark.parametrize("meta", WELLER, ids=lambda m: str(m["fetch_id"]))
def test_weller_split_owned_heading_binds_exact_native_car(meta):
    body = retained(meta)
    soup = BeautifulSoup(body, "html.parser")
    vehicle = json.loads(soup.select_one("script#__NEXT_DATA__").get_text())["props"][
        "pageProps"
    ]["vehicle"]

    page = weller_page(body, meta["url"])

    assert len(page.listings) == 1
    item = page.listings[0]
    assert item.external_id == meta["publication_id"] == str(vehicle["vehicleId"])
    assert item.raw["market_country"] == "DE"
    assert item.vin == vehicle["meta"]["vin"]
    assert item.mileage == vehicle["condition"]["mileage"]
    assert item.price == vehicle["purchaseOptions"]["price"]["value"]
    assert item.scope == "excluded"  # The retained owned cars are C220 diesel estates.
    assert item.raw["country_evidence"]["claims"] == ["DE"]
    assert item.raw["own_dealer_address"][0]["addressCountry"] == "DE"
    assert (
        item.raw["own_dealer_address"][0]["addressLocality"]
        == vehicle["locations"][0]["city"]
    )
    assert (
        item.raw["own_inventory_record"]["offers"][0]["seller"]["address"]
        == item.raw["own_dealer_address"][0]
    )
    assert item.raw["own_identifiers"]["vehicleIdentificationNumber"] == item.vin


@pytest.mark.parametrize(
    "field",
    [
        "canonical_foreign_host",
        "canonical_other_stock",
        "schema_id",
        "schema_uri",
        "native_id",
        "native_name",
        "native_display_name",
        "native_brand",
        "native_model",
        "native_vin",
        "native_mileage",
        "heading_variant",
        "heading_approximate",
        "heading_duplicate",
        "native_script_duplicate",
        "owned_car_duplicate",
        "own_seller_address_missing",
    ],
)
def test_weller_exact_native_binding_rejects_decoys_and_ambiguous_roots(field):
    meta = WELLER[0]
    soup = BeautifulSoup(retained(meta), "html.parser")
    next_node = soup.select_one("script#__NEXT_DATA__")
    doc = json.loads(next_node.get_text())
    vehicle = doc["props"]["pageProps"]["vehicle"]
    if field == "canonical_foreign_host":
        soup.select_one('link[rel="canonical"]')["href"] = meta["url"].replace(
            "wellergruppe.de", "example.test"
        )
    elif field == "canonical_other_stock":
        soup.select_one('link[rel="canonical"]')["href"] = WELLER[1]["url"]
    elif field.startswith("schema_") or field == "own_seller_address_missing":

        def change(root):
            if not isinstance(root, dict) or root.get("@type") != "Car":
                return None
            if field == "schema_id":
                root["@id"] = WELLER[1]["url"]
            elif field == "schema_uri":
                root["url"] = WELLER[1]["url"]
            else:
                root["offers"][0]["seller"].pop("address")
            return root

        rewrite_schema(soup, change)
    elif field.startswith("native_") and field != "native_script_duplicate":
        if field == "native_id":
            vehicle["vehicleId"] = int(WELLER[1]["publication_id"])
        elif field == "native_name":
            vehicle["manufacturer"]["name"] += " other"
        elif field == "native_display_name":
            vehicle["manufacturer"]["displayName"] += " other"
        elif field == "native_brand":
            vehicle["manufacturer"]["brand"] = "BMW"
        elif field == "native_model":
            vehicle["manufacturer"]["model"] = "C 180"
        elif field == "native_vin":
            vehicle["meta"]["vin"] = "WDD2052141F954905"
        else:
            vehicle["condition"]["mileage"] += 1
        next_node.string = json.dumps(doc, ensure_ascii=False)
    elif field == "heading_variant":
        soup.select_one("h1 > p").string = "C 180 similar heading"
    elif field == "heading_approximate":
        paragraph = soup.select_one("h1 > p")
        paragraph.string = paragraph.get_text() + " additional word"
    elif field == "heading_duplicate":
        heading = soup.select_one("h1")
        heading.insert_after(copy.copy(heading))
    elif field == "native_script_duplicate":
        next_node.insert_after(copy.copy(next_node))
    elif field == "owned_car_duplicate":
        script = next(
            s
            for s in soup.select('script[type="application/ld+json"]')
            if json.loads(s.get_text()).get("@type") == "Car"
        )
        conflicting = copy.copy(script)
        root = json.loads(conflicting.get_text())
        root["name"] += " contradictory owned car"
        conflicting.string = json.dumps(root, ensure_ascii=False)
        script.insert_after(conflicting)

    assert not weller_page(str(soup), meta["url"]).listings


def test_weller_never_inherits_de_country_from_native_location_or_another_product():
    meta = WELLER[0]
    soup = BeautifulSoup(retained(meta), "html.parser")

    def change(root):
        if isinstance(root, dict) and root.get("@type") == "Car":
            root["offers"][0]["seller"]["address"]["addressCountry"] = "NL"
            return root
        return None

    rewrite_schema(soup, change)

    page = weller_page(str(soup), meta["url"])

    assert len(page.listings) == 1
    assert page.listings[0].raw["market_country"] == "NL"
    assert page.listings[0].raw["country_evidence"]["claims"] == ["NL"]
    assert page.listings[0].raw["own_dealer_address"][0]["addressCountry"] == "NL"
    assert page.listings[0].scope == "excluded"
