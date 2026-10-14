"""Finite synthetic channel/role regressions; captured values stay private."""

import gzip
import json

import pytest

from tools.check_public_source_privacy import scan_member
from tools.public_fixture_projection import Projection


@pytest.mark.parametrize(
    "name,body",
    [
        ("fixtures/source.gz", "<p>Telefon +49 0000000000</p>"),
        (
            "fixtures/source.xml",
            "<description><![CDATA[Bel 0600000000]]></description>",
        ),
        (
            "fixtures/source.html",
            "<script>number.innerHTML = ' +49 0000000000';</script>",
        ),
        ("fixtures/source.html", '<a href="tel:0600000000" title="0600000000">Bel</a>'),
        (
            "fixtures/source.html",
            '<astro-island props="{&quot;description&quot;:[0,&quot;Bel 0600000000&#39;!&quot;],&quot;id&quot;:[0,&quot;1234&quot;]}"></astro-island>',
        ),
        (
            "fixtures/source.html",
            "<script>self.__next_f.push([1,"
            + json.dumps(
                '1:{"children":"+49 0000000000","href":"tel:+49 0000000000","id":"1234"}'
            )
            + "])</script>",
        ),
    ],
)
def test_contact_projection_covers_exact_residual_serialization_boundaries(name, body):
    result = Projection().text(body, name)
    assert "0000000000" not in result and "0600000000" not in result
    if '"1234"' in body or "&quot;1234&quot;" in body:
        assert '"1234"' in result or "&quot;1234&quot;" in result


@pytest.mark.parametrize(
    "path",
    [
        "/Users/example/private/raw.html",
        "file:///Users/example/private/raw.html",
        "C:/Users/example/private/raw.html",
        r"C:\Users\example\private\raw.html",
    ],
)
def test_all_delivered_private_path_encodings_hold(path):
    raw = json.dumps({"original_path": path}).encode()
    assert scan_member("docs/probe.txt", raw, {})["categories"].get(
        "private_filesystem_path"
    )


@pytest.mark.parametrize(
    "name,body",
    [
        ("docs/probe.txt", "Contact phone: +49 0000000000"),
        ("docs/probe.txt", "Contact email: private-person@example.test"),
        ("docs/probe.html", "<p>My wife and our children used this car.</p>"),
        ("fixtures/probe.json", json.dumps({"contactName": "Example Person"})),
        (
            "fixtures/probe.json",
            json.dumps({"sessionId": "example-noncredential-session"}),
        ),
        (
            "fixtures/probe.json",
            json.dumps({"cookieValue": "example-noncredential-cookie"}),
        ),
        (
            "docs/probe.html",
            '<meta name="session-id" content="example-noncredential-session">',
        ),
    ],
)
def test_personal_role_channels_are_not_implicitly_admitted(name, body):
    row = scan_member(name, body.encode(), {})
    assert row["categories"]
    assert "Example Person" not in json.dumps(row)
    assert "example-noncredential-session" not in json.dumps(row)


def test_family_spans_removed_from_html_json_arrays_without_changing_car_facts():
    text = "BMW 218i, 2019. My wife and our children used this car. Mileage 40000 km."
    for doc in (
        {"description": text, "year": 2019, "price": 19800},
        {"UI": "<p>" + text + "</p>"},
        {"parts": [text, None, 214]},
    ):
        result = Projection().document(doc)
        assert "wife" not in json.dumps(result) and "children" not in json.dumps(result)
        assert "BMW 218i" in json.dumps(result) and "40000" in json.dumps(result)
        if "year" in doc:
            assert result["year"] == 2019 and result["price"] == 19800
        if "parts" in doc:
            assert result["parts"][1:] == [None, 214]


@pytest.mark.parametrize(
    "text",
    [
        "<pubDate>07/10/2026 20:14</pubDate><p>Telephone information unavailable.</p>",
        "<p>Erstzulassung: 01/2020\n218 PS. Telefon unavailable.</p>",
        '{"description":"Service recorded 04.2016 218 PS; mileage 42000 km."}',
        '{"id":"0600000000","phone":null,"url":"https://example.invalid/0600000000"}',
    ],
)
def test_date_engine_and_exact_native_id_roles_are_not_phone_authorizations(text):
    row = scan_member("fixtures/role.html", text.encode(), {})
    assert not row["categories"]


def test_true_phone_next_to_pubdate_still_holds():
    raw = b"<pubDate>07/10/2026 20:14</pubDate><p>Telefon +49 0000000000</p>"
    assert scan_member("fixtures/role.html", raw, {})["categories"]


def test_gzip_channel_has_same_personal_role_policy():
    raw = json.dumps({"contactName": "Example Person"}).encode()
    assert scan_member("fixtures/role.json.gz", gzip.compress(raw, mtime=0), {})[
        "categories"
    ]


def test_private_seller_labels_preserve_exact_equality_without_merging_ids():
    doc = {
        "listings": [
            {
                "itemId": "a",
                "sellerInformation": {"sellerId": 1, "sellerName": "Example Person"},
            },
            {
                "itemId": "b",
                "sellerInformation": {"sellerId": 2, "sellerName": "Example Person"},
            },
            {
                "itemId": "c",
                "sellerInformation": {
                    "sellerId": 3,
                    "sellerName": "Another Example Person",
                },
            },
        ]
    }
    result = Projection().document(doc)
    assert [r["itemId"] for r in result["listings"]] == ["a", "b", "c"]
    labels = [r["sellerInformation"]["sellerName"] for r in result["listings"]]
    assert labels[0] == labels[1] != labels[2]
    assert [r["sellerInformation"]["sellerId"] for r in result["listings"]] == [1, 2, 3]
    assert "Example Person" not in json.dumps(result)


def test_auth_config_values_projected_without_touching_native_vehicle_literals():
    body = '<script>const config = {auth:"example-noncredential@example.test", stagingAuth:\'example-noncredential@example.test\', htaccess:"other-noncredential@example.test", carId:"1234", price:19800};</script>'
    result = Projection().text(body, "fixtures/auth.html")
    assert "example.test" not in result
    assert 'carId:"1234", price:19800' in result
    assert result.count("[private-authority-") == 3


def test_parent_owner_narrative_spans_are_removed_in_unknown_array_keys():
    doc = {
        "bodyText": [
            "Mijn moeder gebruikte deze BMW 218i. Mijn vader reed 40000 km.",
            214,
            None,
        ]
    }
    result = Projection().document(doc)
    assert "moeder" not in json.dumps(result) and "vader" not in json.dumps(result)
    assert "BMW 218i" in json.dumps(result) and "40000" in json.dumps(result)
    assert result["bodyText"][1:] == [214, None]


def test_reviewed_role_locus_never_authorizes_an_inserted_person_address():
    from tools.check_public_source_privacy import digest, structured_role_loci

    name = "fixtures/record.json"
    body = json.dumps(
        {
            "offers": {
                "seller": {
                    "@type": "Organization",
                    "address": {"streetAddress": "Synthetic business address"},
                }
            }
        }
    ).encode()
    locus = structured_role_loci(body.decode())[0]
    policy = {
        "reviewed_loci": {
            name: {
                "decoded_sha256": digest(body),
                "loci": [
                    {
                        **locus,
                        "role": "exact_declared_business_address",
                        "pointer": "/offers/seller/address/streetAddress",
                    }
                ],
            }
        }
    }
    assert not scan_member(name, body, policy)["categories"]
    for changed in [
        body.replace(b'"Organization"', b'"Person"'),
        body[:-1] + b',"contactName":"Example Person"}',
    ]:
        assert scan_member(name, changed, policy)["categories"]


def test_reviewed_taxonomy_role_cannot_authorize_root_fullname_or_new_body():
    from tools.check_public_source_privacy import digest, structured_role_loci

    name = "fixtures/record.json"
    body = b'{"searchCategoryOptions":[{"id":1,"key":"cars","fullName":"Cars"}]}'
    locus = structured_role_loci(body.decode())[0]
    policy = {
        "reviewed_loci": {
            name: {
                "decoded_sha256": digest(body),
                "loci": [
                    {
                        **locus,
                        "role": "exact_vehicle_taxonomy",
                        "pointer": "/searchCategoryOptions/0/fullName",
                    }
                ],
            }
        }
    }
    assert not scan_member(name, body, policy)["categories"]
    assert scan_member(name, b'{"fullName":"Example Person"}', policy)["categories"]
    assert scan_member(name, body.replace(b'"Cars"', b'"Example Person"'), policy)[
        "categories"
    ]


def test_sentry_projection_does_not_rewrite_own_vehicle_uri_or_retina_asset():
    doc = {
        "url": "https://example.invalid/car/1234",
        "dsn": "https://example@sentry.io/1234",
        "image": "https://example.invalid/image@2x.png",
    }
    result = Projection().document(doc)
    assert result["url"] == doc["url"] and result["image"] == doc["image"]
    assert result["dsn"].startswith("[private-telemetry-project-")


def test_comment_and_contact_onclick_title_are_only_local_contact_changes():
    original = '<!--Phone +49 0000000000 email person@example.test--><a onclick="document.location.href=\'tel: 0600000000\'" title="Bel ons op 0600000000" href="https://example.invalid/car/1234">BMW 218i</a>'
    result = Projection().text(original, "fixtures/source.html")
    assert (
        "0000000000" not in result
        and "0600000000" not in result
        and "example.test" not in result
    )
    assert 'href="https://example.invalid/car/1234"' in result and "BMW 218i" in result


def test_foreign_contact_prefix_in_street_never_changes_own_country_identity():
    doc = {
        "id": "1234",
        "location": {"countryCode": None, "street": "Contact +7 0000000000"},
    }
    result = Projection().document(doc)
    assert result["id"] == "1234" and result["location"]["countryCode"] is None
    assert "+7[private-phone-" in result["location"]["street"]


@pytest.mark.parametrize(
    "body",
    [
        "<pubDate>21/10/2026 20:14:42</pubDate><p>Telefon unavailable</p>",
        "<pubDate>10/21/2026 20:14:42</pubDate><p>Telefon unavailable</p>",
        "<p>EZ 01/2020 -- 18.000€ BMW 218i. Telefon unavailable.</p>",
        '{"description":"Service 01.2020 140.000km interval 2"}',
    ],
)
def test_precisely_declared_native_date_cash_mileage_roles_do_not_hide_contacts(body):
    assert not scan_member("fixtures/role.html", body.encode(), {})["categories"]
    changed = body + "<p>Telefon +49 0000000000</p>"
    assert scan_member("fixtures/role.html", changed.encode(), {})["categories"]


@pytest.mark.parametrize(
    "body",
    [
        "const settings={auth:'synthetic-authority'};",
        "const settings={'stagingAuth':'synthetic-authority'};",
        "const settings={htaccess:'synthetic-authority'};",
    ],
)
def test_javascript_authority_literal_has_no_code_channel_exemption(body):
    assert scan_member("docs/control.txt", body.encode(), {})["categories"].get(
        "unreviewed_session_literal"
    )


def test_clean_environment_module_preflight_binds_repository_tools(tmp_path):
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).parents[1]
    local = tmp_path / "repository"
    (local / "tools").mkdir(parents=True)
    for name in (
        "__init__.py",
        "check_public_source_privacy.py",
        "public_fixture_projection.py",
    ):
        shutil.copyfile(root / "tools" / name, local / "tools" / name)
    (local / "tools/public-source-privacy-policy.json").write_text("{}")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run(["git", "init", "--quiet", str(local)], check=True, env=env)
    subprocess.run(["git", "add", "tools"], cwd=local, check=True, env=env)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tools,tools.check_public_source_privacy as g;from pathlib import Path;assert Path(tools.__file__).resolve()==Path('tools/__init__.py').resolve();assert Path(g.__file__).resolve()==Path('tools/check_public_source_privacy.py').resolve()",
        ],
        cwd=local,
        env=env,
        capture_output=True,
        check=False,
    )
    assert probe.returncode == 0, "Repository tools import binding failed"
    output = tmp_path / "receipt.json"
    command = [
        sys.executable,
        "-m",
        "tools.check_public_source_privacy",
        "--repo",
        ".",
        "--output",
        str(output),
    ]

    clean = subprocess.run(
        command, cwd=local, env=env, capture_output=True, check=False
    )
    assert clean.returncode == 0, "Clean-environment module preflight failed"
    assert (
        json.loads(output.read_text())["tracked_source"]["status"]
        == "PASS_SCOPED_CONTACT_CONTAINMENT"
    )
    (local / "docs").mkdir()
    (local / "docs/control.txt").write_text("Contact phone +49 0000000000")
    subprocess.run(["git", "add", "docs/control.txt"], cwd=local, check=True, env=env)
    held = subprocess.run(command, cwd=local, env=env, capture_output=True, check=False)
    assert held.returncode == 1
    assert json.loads(output.read_text())["tracked_source"]["status"] == "HOLD"
    assert b"0000000000" not in held.stdout + held.stderr


def test_partial_flight_contact_label_does_not_project_adjacent_native_ids():
    inner = '1:{"id":"0600000000","price":19800,"year":2019,"country":"NL"},Tel: +49 0000000000 Email: person@example.test'
    original = "<script>self.__next_f.push([1," + json.dumps(inner) + "])</script>"
    result = Projection().text(original, "fixture.html")
    assert "example.test" not in result and "+49 0000000000" not in result
    assert '\\"id\\":\\"0600000000\\"' in result
    assert '\\"country\\":\\"NL\\"' in result and '\\"price\\":19800' in result


def test_call_button_value_is_contact_but_text_input_numeric_id_is_not():
    original = '<input type="button" onclick="location.href=\'tel:0600000000\'" value="0600000000"><input type="text" name="id" value="0600000000">'
    result = Projection().text(original, "fixture.html")
    assert 'value="[private-phone-1]"' in result
    assert '<input type="text" name="id" value="0600000000">' in result
    labelled = '<input type="submit" onclick="location.href=\'tel:0600000000\'" value="Bel 0600000000">'
    assert 'value="Bel [private-phone-1]"' in Projection().text(
        labelled, "fixture.html"
    )


def test_invalid_rss_calendar_literal_is_not_an_authorized_numeric_role():
    body = "<pubDate>01/32/2026 20:14:42</pubDate><p>Telefon unavailable</p>"
    assert scan_member("fixtures/role.xml", body.encode(), {})["categories"]
