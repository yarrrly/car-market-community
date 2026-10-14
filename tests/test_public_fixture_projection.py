"""Synthetic, nondialable contact controls for private source projections."""

import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "projection", Path(__file__).parents[1] / "tools/public_fixture_projection.py"
)
projection = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(projection)


def test_free_description_contacts_preserve_vehicle_tokens_and_country_prefix():
    # Intentionally nondialable zero subscriber number, not a captured contact.
    original = "BMW 218i Active Tourer, 2019, automatic. Telefon: +49 0000000000."
    result = projection.Projection().contact_text(original)
    assert "+49 0000000000" not in result
    assert "+49[private-phone-" in result
    assert "BMW 218i Active Tourer, 2019, automatic" in result


def test_date_ids_prices_images_and_native_uris_are_not_telephone_contacts():
    original = {
        "id": "06000000000",
        "vin": "WBA060000000000001",
        "url": "https://example.invalid/car/06000000000",
        "price": 19800,
        "year": 2019,
        "mileage": 0,
        "images": ["https://example.invalid/image@2x.png"],
        "description": "Captured 01.10.2026 12:30, maintenance recorded.",
    }
    assert projection.Projection().document(original) == original


def test_phone_number_mobile_and_whatsapp_fields_are_typed_not_numeric_references():
    original = {
        "mobile": "+49 0000000000",
        "phoneNumber": "+49 0000000000",
        "whatsappNumber": "+49 0000000000",
        "phone": None,
        "refs": [308, 214, 352, 296],
        "mobile_url": "https://example.invalid/1234567890",
    }
    result = projection.Projection().document(original)
    assert result["mobile"] == result["phoneNumber"] == result["whatsappNumber"]
    assert result["phone"] is None and result["refs"] == original["refs"]
    assert result["mobile_url"] == original["mobile_url"]


def test_direct_nuxt_phone_cell_preserves_indices_and_shared_none():
    original = [
        {"phoneNumber": 1, "name": 2, "phone": 3},
        "+49 0000000000",
        "Example Business",
        None,
    ]
    result = projection.Projection().document(original)
    assert result[0] == original[0] and result[2:] == original[2:]
    assert result[1].startswith("+49[private-phone-")
    shared = [{"phoneNumber": 1, "id": 1}, "06000000000"]
    assert projection.Projection().document(shared) == shared


def test_html_description_and_json_ld_compose_without_rewriting_images_or_schema():
    data = {
        "@type": "Car",
        "sku": "1234",
        "description": "Bel 06000000000",
        "price": 19800,
    }
    original = (
        '<script type="application/ld+json">'
        + json.dumps(data)
        + '</script><p>Bel 06000000000</p><img src="image@2x.png"><pubdate>01.10.2026 12:30</pubdate>'
    )
    result = projection.Projection().text(original, "fixture.html")
    assert "06000000000" not in result
    assert '<img src="image@2x.png">' in result
    assert "<pubdate>01.10.2026 12:30</pubdate>" in result
    assert '"sku": "1234"' in result and '"@type": "Car"' in result


def test_existing_exact_literal_class_is_reused_between_old_field_and_new_description():
    current = "+49[redacted-53]"
    projector = projection.Projection({"+49 0000000000": current})
    assert (
        projector.document({"phoneNumber": "+49 0000000000"})["phoneNumber"] == current
    )
    assert projector.contact_text("Telefon +49 0000000000") == "Telefon " + current


def test_local_capture_paths_omit_account_without_renewing_hash_or_clock():
    original = {
        "original_path": "/Users/example/private/original.html",
        "original_sha256": "a" * 64,
        "fetched_at": "2026-10-07T08:18:49Z",
    }
    result = projection.Projection().document(original)
    assert (
        result["original_path"] == "private-evidence:synthetic-fixture#/original_path"
    )
    assert result["original_sha256"] == original["original_sha256"]
    assert result["fetched_at"] == original["fetched_at"]


def test_asset_at_sign_and_noncontact_json_integer_remain_unchanged():
    original = {"description": "Image image@2x.png, BMW 218i, 2018", "phoneNumber": 214}
    assert projection.Projection().document(original) == original


def test_provider_home_route_is_not_a_private_filesystem_path():
    original = {
        "final_url": "https://suchen.mobile.de/home/vip?id=1234567890",
        "description": "Native route https://suchen.mobile.de/home/vip?id=1234567890",
    }
    assert projection.Projection().document(original) == original


def test_embedded_astro_json_contact_description_preserves_typed_numeric_references():
    from html import escape, unescape

    native = {
        "description": [0, "Telefon +49 0000000000"],
        "id": [0, "1234"],
        "price": [0, 19800],
    }
    original = (
        '<astro-island props="'
        + escape(json.dumps(native), quote=True)
        + '"></astro-island>'
    )
    result = projection.Projection().text(original, "fixture.html")
    assert "+49 0000000000" not in result
    assert '"id": [0, "1234"]' in unescape(result)
    assert '"price": [0, 19800]' in unescape(result)


def test_nested_json_and_html_contact_payloads_do_not_mask_svg_path_geometry():
    geometry = "M+49 00 00 00 00 00 00 0"
    html = '<svg><path d="' + geometry + '"></path></svg><p>Telefon +49 0000000000</p>'
    native = {"own_UI": html, "nested": json.dumps({"GSM": "Telefon +49 0000000000"})}
    result = projection.Projection().document(native)
    assert geometry in result["own_UI"]
    assert "+49 0000000000" not in result["own_UI"]
    assert "+49 0000000000" not in result["nested"]


def test_partial_hydration_records_mask_contact_child_not_svg_geometry():
    geometry = "M+49 00 00 00 00 00 00 0"
    partial = (
        '20:{"children":"Telefon +49 0000000000","d":"' + geometry + '","id":"1234"}'
    )
    original = "<script>self.__next_f.push([1," + json.dumps(partial) + "]);</script>"
    result = projection.Projection().text(original, "fixture.html")
    assert geometry in result
    assert "+49 0000000000" not in result
    assert "1234" in result


def test_sensitive_scalar_projection_preserves_literal_negative_mutation_targets():
    original = '{  "id" : "1234", "date":"01.10.2026", "price" : 19800, "description" : "Bel 06000000000", "image":"image@2x.png" }'
    result = projection.Projection().text(original, "fixture.json")
    sensitive = '"Bel 06000000000"'
    projected = json.dumps(json.loads(result)["description"])
    assert result == original.replace(sensitive, projected)
    for mutation in ('"date":"01.10.2026"', '"price" : 19800', '"id" : "1234"'):
        assert mutation in result
        assert result.replace(mutation, "null") != result


def test_unrelated_json_escape_spelling_and_nested_hydration_are_preserved():
    original = r'{"title":"BMW\u0020218i", "uri":"https:\/\/example.invalid\/1234", "description":"Bel 06000000000, BMW\u0020218i"}'
    result = projection.Projection().text(original, "fixture.json")
    assert r'"title":"BMW\u0020218i"' in result
    assert r'"uri":"https:\/\/example.invalid\/1234"' in result
    assert r"BMW\u0020218i" in result
    assert "06000000000" not in result


def test_paired_mobile_contact_spans_agree_in_dom_and_own_schema_name():
    label = "BMW 218i Active Tourer 0600000000 / 0611111111"
    original = (
        "<h1>"
        + label
        + '</h1><script type="application/ld+json">'
        + json.dumps({"@type": "Car", "name": label})
        + "</script>"
    )
    p = projection.Projection()
    result = p.text(original, "fixture.html")
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(result, "html.parser")
    assert soup.h1.text == json.loads(soup.script.string)["name"]
    assert soup.h1.text.startswith("BMW 218i Active Tourer ")
    assert "0600000000" not in result and "0611111111" not in result
