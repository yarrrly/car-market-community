import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from car_market.autowereld_details import parse_autowereld_detail
from car_market.db import Database

FIXTURES = Path(__file__).parent / "fixtures" / "autowereld_details"
PROVENANCE = json.loads((FIXTURES / "provenance.json").read_text())


def fixture(own_id="43593530"):
    name = f"own-{own_id}.html"
    return (FIXTURES / name).read_text(), PROVENANCE[name]["url"]


def test_real_own_detail_recovers_specs_description_and_own_photos():
    body, url = fixture()
    item = parse_autowereld_detail(body, url).listings[0]
    assert (item.source, item.url, item.external_id) == ("autowereld", url, url)
    assert (item.kenteken, item.price, item.mileage, item.year) == ("6ZSK83", 7350, 168205, 2015)
    assert (item.transmission, item.fuel, item.seller, item.city) == (
        "manual",
        "petrol",
        "FLEVO Mobiel",
        "Dronten",
    )
    assert item.scope == "target"
    assert "Onderhoudsboekjes: Aanwezig" in item.description
    assert "zekerheidspakketten" in item.description
    assert len(item.images) == 6
    assert item.raw["detail_complete"] is True
    assert item.raw["specs"]["Datum deel 1"] == "19-03-2015"


@pytest.mark.parametrize("own_id", ["43311872", "43326519", "42085709"])
def test_real_escaped_quotes_decode_static_js_layer_without_eval(own_id):
    body, url = fixture(own_id)
    item = parse_autowereld_detail(body, url).listings[0]
    assert item.raw["identity_evidence"]["nr"] == own_id
    assert '"' in item.title


def test_real_unknown_plate_remains_unknown():
    body, url = fixture("42516257")
    item = parse_autowereld_detail(body, url).listings[0]
    assert item.kenteken is None
    assert item.raw["plate_evidence"]["conflict"] is False


def test_real_invalid_control_character_rejects_despite_visible_plate():
    body, url = fixture("42224801")
    assert "JJK-39-B" in body
    assert parse_autowereld_detail(body, url) is None


@pytest.mark.parametrize("mutation", ["url", "canonical", "canonical_host", "json", "heading"])
def test_identity_mismatch_rejects_instead_of_returning_related_vehicle(mutation):
    body, url = fixture()
    soup = BeautifulSoup(body, "html.parser")
    if mutation == "url":
        url = url.replace("43593530", "99999999")
    elif mutation == "canonical":
        soup.select_one("link[rel=canonical]")["href"] = url.replace("43593530", "99999999")
    elif mutation == "canonical_host":
        soup.select_one("link[rel=canonical]")["href"] = url.replace(
            "www.autowereld.nl", "example.com"
        )
    elif mutation == "heading":
        soup.select_one("#global-info h1 a[href$='details.html']")["href"] = url.replace(
            "43593530", "99999999"
        )
    else:
        script = soup.find("script")
        script.string = script.string.replace('"nr":"43593530"', '"nr":"99999999"')
    assert parse_autowereld_detail(str(soup), url) is None


@pytest.mark.parametrize(
    "mutation", ["missing_json", "malformed", "dynamic", "duplicate", "missing_canonical"]
)
def test_no_generic_fallback_for_missing_or_unsafe_identity(mutation):
    body, url = fixture()
    soup = BeautifulSoup(body, "html.parser")
    script = soup.find("script")
    if mutation == "missing_json":
        script.decompose()
    elif mutation == "missing_canonical":
        soup.select_one("link[rel=canonical]").decompose()
    elif mutation == "duplicate":
        soup.head.append(BeautifulSoup(str(script), "html.parser"))
    elif mutation == "dynamic":
        script.string = script.string.replace('"nr":"43593530"', '"nr":"${43593530}"')
    else:
        script.string = script.string.replace('"nr":"43593530"', '"nr":BROKEN')
    assert parse_autowereld_detail(str(soup), url) is None


def test_conflicting_own_plate_is_rejected():
    body, url = fixture()
    soup = BeautifulSoup(body, "html.parser")
    term = next(n for n in soup.select("#global-info > .features td.term") if n.text == "Kenteken")
    term.find_next_sibling("td").string = "8-XRR-49"
    assert parse_autowereld_detail(str(soup), url) is None


def test_photo_link_for_another_id_inside_preview_is_ignored():
    body, url = fixture()
    soup = BeautifulSoup(body, "html.parser")
    anchor = soup.select_one("#photo-container a[href]")
    wrong_image = anchor.select_one("img")["src"]
    anchor["href"] = anchor["href"].replace("43593530", "99999999")
    item = parse_autowereld_detail(str(soup), url).listings[0]
    assert wrong_image not in item.images and len(item.images) == 5


@pytest.mark.parametrize("mutation", ["ellipsis", "clamp"])
def test_partial_own_description_does_not_claim_complete(mutation):
    body, url = fixture()
    soup = BeautifulSoup(body, "html.parser")
    node = soup.select_one("#occasion-extra-info .extra-info-text")
    if mutation == "ellipsis":
        node.append("…")
    else:
        node["class"].append("line-clamp-3")
    item = parse_autowereld_detail(str(soup), url).listings[0]
    assert item.description and item.raw["detail_complete"] is False


@pytest.mark.parametrize("missing", ["json", "table", "both"])
def test_only_scoped_explicit_plate_evidence_is_used(missing):
    body, url = fixture()
    soup = BeautifulSoup(body, "html.parser")
    if missing in {"json", "both"}:
        script = soup.find("script")
        script.string = script.string.replace('"kenteken":"6ZSK83"', '"kenteken":null')
    if missing in {"table", "both"}:
        term = next(
            n for n in soup.select("#global-info > .features td.term") if n.text == "Kenteken"
        )
        term.parent.decompose()
    soup.body.append(
        BeautifulSoup(
            '<aside>Kenteken: 8-XRR-49 <table><tr><td class="term">Kenteken</td><td class="value">8-XRR-49</td></tr></table></aside>',
            "html.parser",
        )
    )
    item = parse_autowereld_detail(str(soup), url).listings[0]
    assert item.kenteken == (None if missing == "both" else "6ZSK83")


def test_related_description_seller_and_photo_cannot_fill_missing_own_data():
    body, url = fixture()
    soup = BeautifulSoup(body, "html.parser")
    soup.select_one("#occasion-extra-info").decompose()
    soup.select_one("#global-info > .provider").decompose()
    soup.select_one("#photo-container").decompose()
    trap = '<aside><p class="extra-info-text">Dealer history with enough words to trick a whole page scanner.</p><div class="provider"><div class="user"><a class="name">Wrong dealer</a><span class="zipcode">1234AB Wrongtown</span></div></div><a href="/bmw/2-serie-active-tourer/related-99999999/foto.html"><img src="https://example.com/related.jpg"></a></aside>'
    soup.select_one("#global-info #similar-occasions").append(BeautifulSoup(trap, "html.parser"))
    item = parse_autowereld_detail(str(soup), url).listings[0]
    assert item.description is None and item.seller is None and item.city is None
    assert item.images == [] and item.raw["detail_complete"] is False


def test_archive_observation_and_field_lineage_keep_original_fetch_time(tmp_path):
    body, url = fixture()
    timestamp = PROVENANCE["own-43593530.html"]["fetched_at"]
    database = Database(tmp_path / "archive-only.sqlite3")
    run = database.start_run("archive_replay", timestamp=timestamp)
    fetch_id = database.save_fetch(
        run, "autowereld", url, url, 200, body, timestamp, "retained_replay"
    )
    item = parse_autowereld_detail(body, url).listings[0]
    database.observe(run, item, timestamp, fetch_id)
    observation = database.conn.execute("SELECT * FROM observations").fetchone()
    assert observation["observed_at"] == timestamp
    assert database.conn.execute("SELECT fetched_at FROM fetches").fetchone()[0] == timestamp
    lineage = json.loads(observation["details_json"])["raw"]["field_evidence"]
    assert lineage["kenteken"]["observed_at"] == timestamp
    assert lineage["kenteken"]["fetch_id"] == fetch_id
    database.conn.close()
