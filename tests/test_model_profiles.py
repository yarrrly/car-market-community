import pytest
from car_market.db import Database
from car_market.model_profiles import (
    BMW_PROFILE,
    MERCEDES_PROFILE,
    bind_profile,
    qualify_mercedes,
    use_profile,
    validate_profile_path,
)
from car_market.parse import Listing


def config(country):
    return {
        "scope": {
            "country": country,
            "model_profile": MERCEDES_PROFILE,
            "generation": "C205",
            "transmissions": ["automatic"],
        }
    }


def item(country="NL", **changes):
    values = {
        "source": "autoscout24" if country == "NL" else "autoscout24_de",
        "external_id": "own-ad",
        "url": "https://example.test/own-ad",
        "title": "Mercedes-Benz C180 Coupé C205",
        "price": 22000,
        "year": 2018,
        "mileage": 40000,
        "transmission": "automatic",
        "fuel": "petrol",
        "raw": {"market_country": country},
    }
    values.update(changes)
    return Listing(**values)


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_mercedes_context_qualifies_and_legacy_bmw_still_works(country):
    with use_profile(MERCEDES_PROFILE, country):
        own = item(country).qualify()
    assert own.scope == "target"
    assert own.raw["model_profile"] == MERCEDES_PROFILE
    bmw = Listing(
        "autoscout24", "bmw", "https://example.test/bmw", "BMW 218i Active Tourer", fuel="petrol"
    )
    assert bmw.qualify().scope == "target"


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "Mercedes C200 Coupé C205"},
        {"title": "Mercedes C180 Cabriolet A205"},
        {"transmission": "manual"},
        {"fuel": "diesel"},
        {"year": 2015},
        {"title": "BMW C180 Coupé C205"},
        {"raw": {"market_country": "BE", "generation": "C205"}},
    ],
)
def test_wrong_vehicle_cannot_be_target(changes):
    assert qualify_mercedes(item(**changes), "NL").scope == "excluded"


def test_old_generation_cannot_pass_on_late_registration():
    own = item(year=2018, raw={"generation": "C204", "market_country": "NL"})
    assert qualify_mercedes(own, "NL").scope == "excluded"


def test_year_alone_does_not_prove_generation():
    own = item(title="Mercedes C180 Coupé", year=2021)
    assert qualify_mercedes(own, "NL").scope == "needs_confirmation"


def test_vin_binds_coupe_generation_without_title_tag():
    own = item(title="Mercedes C180 Coupé", vin="WDD2053401F123456")
    assert qualify_mercedes(own, "NL").scope == "target"
    assert own.raw["model_scope_evidence"]["generation"]["kind"] == "own_vin_series"


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_profile_binding_rejects_cross_model_before_identity_writes(tmp_path, country):
    db = Database(tmp_path / "market.sqlite3")
    bind_profile(db, config(country))
    run = db.start_run("test")
    wrong = Listing(
        "autoscout24", "bmw", "https://example.test/bmw", "BMW 218i Active Tourer", fuel="petrol"
    ).qualify()
    with pytest.raises(ValueError, match="model profile"):
        db.observe(run, wrong, "2026-10-08T14:00:00+00:00")
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0
    with pytest.raises(ValueError, match="another model"):
        validate_profile_path(db.path, country, BMW_PROFILE)
    db.conn.close()


def test_country_unknown_rejected_before_registry_write(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    bind_profile(db, config("NL"))
    run = db.start_run("test")
    own = item(raw={})
    qualify_mercedes(own, "NL")
    with pytest.raises(ValueError, match="country proof"):
        db.observe(run, own, "2026-10-08T14:00:00+00:00")
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0
    db.conn.close()


def test_enrichment_never_reclassifies_mercedes_as_bmw(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    bind_profile(db, config("NL"))
    with use_profile(MERCEDES_PROFILE, "NL"):
        first = item(description="Own C180 Coupe description").qualify()
        db.observe(db.start_run("test"), first, "2026-10-08T14:00:00+00:00")
        second = item(description=None).qualify()
        db.observe(db.start_run("test"), second, "2026-10-08T15:00:00+00:00")
    own = db.conn.execute("SELECT scope,transmission FROM market_current").fetchone()
    assert tuple(own) == ("target", "automatic")
    assert db.conn.execute("SELECT COUNT(*) FROM purchase_candidates").fetchone()[0] == 1
    db.conn.close()
