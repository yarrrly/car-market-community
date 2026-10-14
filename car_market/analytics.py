"""Reproducible descriptive market analysis from read-only SQLite snapshots."""

import collections
import csv
import hashlib
import json
import math
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .db import Database
from .identity import normalized_vin
from .model_profiles import BMW_PROFILE, MERCEDES_PROFILE, bound_scope, own_country
from .provenance import timestamp
from .search_profile import normalized
from .vehicle_catalog import catalog

COUNTRIES = {"NL": "Netherlands", "DE": "Germany"}
CAUTIONS = [
    "Collected sample; market coverage is not proven.",
    "Catalog IDs are not a census of unique physical cars. Provisional identities may be duplicates.",
    "Asking prices are not transaction prices. One minimum eligible full purchase quote per catalog ID.",
    "Comparable groups control year, transmission and mileage band only; generation, trim and condition are not controlled.",
    "Declared current damage is excluded from comparable groups. Absence of a damage flag is not evidence of good condition.",
]
COMPARISON_CAUTION = "Germany is shown before import costs. Differences between samples are descriptive, not estimated savings or depreciation."


def cautions(markets):
    """Cautions for this report; the import-cost note only applies to an NL/DE comparison."""
    return CAUTIONS + ([COMPARISON_CAUTION] if {"NL", "DE"} <= set(markets) else [])


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def quantiles(values):
    values = sorted(
        v
        for v in values
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    )
    if not values:
        return {
            "n": 0,
            "min": None,
            "q1": None,
            "median": None,
            "q3": None,
            "max": None,
        }

    def at(fraction):
        position = (len(values) - 1) * fraction
        lo, hi = math.floor(position), math.ceil(position)
        return values[lo] + (values[hi] - values[lo]) * (position - lo)

    return {
        "n": len(values),
        "min": values[0],
        "q1": at(0.25),
        "median": at(0.5),
        "q3": at(0.75),
        "max": values[-1],
    }


def damaged(payload):
    from .publication_state import own_damage

    current = own_damage(payload)
    if current is not None:
        return current
    return ((payload.get("raw") or {}).get("damage_evidence") or {}).get(
        "value"
    ) is True


def geography(raw, country):
    own, evidence = own_country(raw)
    if (
        own is None
        and evidence.get("kind") != "conflicting_own_country"
        and country == "NL"
    ):
        from .mercedes_nl_sources import own_nl_evidence

        native = own_nl_evidence("analytics", raw)
        address = native.get("dealer_address") or {}
        proof = {
            "country_code": native.get("country"),
            "dealerAddress": {
                "postalCode": address.get("postal_code"),
                "country": address.get("country"),
            },
        }
        own, evidence = own_country(proof)
    return own, evidence


def scope_label(scope):
    selector = (scope or {}).get("selector")
    if selector:
        return (
            selector["make"]
            + " "
            + " ".join(group[0] for group in selector["model_terms"])
        )
    if (scope or {}).get("model_profile") == MERCEDES_PROFILE:
        return "Mercedes-Benz C180 Coupe C205"
    return "BMW 218i Active Tourer"


def scope_key(scope):
    scope = scope or {}
    if scope.get("selector"):
        return json.dumps(scope["selector"], sort_keys=True)
    return scope.get("model_profile", BMW_PROFILE)


def snapshot(source, target):
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError("Database does not exist: " + str(source))
    db = Database.read_only(source)
    try:
        db.conn.execute("BEGIN")
        db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()
        captured = datetime.now(UTC).isoformat()
        with sqlite3.connect(target) as destination:
            db.conn.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Snapshot failed SQLite integrity check")
            if destination.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Snapshot has foreign key violations")
    finally:
        db.conn.close()
    return {
        "source": str(source),
        "snapshot": Path(target).name,
        "captured_at": captured,
        "sha256": file_sha256(target),
    }


def analyze_market(
    path, country, *, as_of, freshness_hours=13, price_floor=1, price_ceiling=None
):
    path = Path(path).resolve()
    wal = Path(str(path) + "-wal")
    mode = "mode=ro" if wal.exists() and wal.stat().st_size else "mode=ro&immutable=1"
    conn = sqlite3.connect(path.as_uri() + "?" + mode, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    try:
        scope = bound_scope(conn) or {"model_profile": BMW_PROFILE, "country": country}
        if scope.get("country", country) != country:
            raise ValueError("Report country does not match database binding")
        config = {
            "scope": scope,
            "as_of": as_of,
            "freshness_hours": freshness_hours,
            "include_history": False,
        }
        cards = catalog(conn, config)
        latest = {
            row["listing_id"]: json.loads(row["details_json"])
            for row in conn.execute(
                "SELECT listing_id,details_json FROM latest_observations"
            )
        }
        all_sources = dict(
            conn.execute(
                "SELECT source,COUNT(*) FROM listings GROUP BY source ORDER BY source"
            )
        )
        source_health = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM source_health ORDER BY source,query_key"
            )
        ]
        cars, excluded = [], []
        eligible_publications = 0
        clock = timestamp(as_of)
        selector = scope.get("selector") or {
            "make": "Mercedes-Benz"
            if scope.get("model_profile") == MERCEDES_PROFILE
            else "BMW"
        }
        for card in cards:
            offers = []
            own_payloads = []
            root_hold = False
            identity_conflict = False
            unavailable_clocks, available_clocks = [], []
            for offer in card["offers"]:
                raw = latest.get(offer["listing_id"], {}).get("raw") or {}
                own_clock = timestamp(offer["observed_at"])
                if own_clock is None or own_clock > clock:
                    continue
                state = raw.get("availability_evidence") or {}
                state_clock = (
                    timestamp(state.get("observed_at")) if state else own_clock
                )
                status = str(
                    state.get("value") or raw.get("availability_status") or ""
                ).casefold()
                if state_clock is None or state_clock > clock:
                    continue
                if scope.get("model_profile") == "configured_vehicle":
                    contradictions = set(
                        (raw.get("model_scope_evidence") or {}).get("reasons") or []
                    )
                    if contradictions & {
                        "different_make",
                        "excluded_variant",
                        "different_fuel",
                        "different_transmission",
                        "outside_year_range",
                        "foreign_stock_country",
                    }:
                        root_hold = True
                    if contradictions & {"different_make", "excluded_variant"}:
                        identity_conflict = True
                if (
                    raw.get("sold") is True
                    or raw.get("reserved") is True
                    or status in {"sold", "reserved", "verkocht", "verkauft"}
                ):
                    unavailable_clocks.append(state_clock)
                if (
                    raw.get("availableNow") is True
                    or raw.get("reserved") is False
                    or status == "available"
                ):
                    available_clocks.append(state_clock)
            if unavailable_clocks and (
                not available_clocks or max(unavailable_clocks) >= max(available_clocks)
            ):
                root_hold = True
            for offer in card["offers"]:
                if not offer["is_live"]:
                    if offer["availability"] in {
                        "seller_marked_sold",
                        "seller_reserved",
                    }:
                        excluded.append(
                            {
                                "country": country,
                                "vehicle_id": card["id"],
                                "listing_id": offer["listing_id"],
                                "url": offer["url"],
                                "reasons": ["own_sold_or_reserved"],
                            }
                        )
                    continue
                payload = latest.get(offer["listing_id"], {})
                raw = payload.get("raw") or {}
                own, proof = geography(raw, country)
                reasons = []
                if offer["scope"] == "model_conflict":
                    root_hold = True
                    identity_conflict = True
                if offer["scope"] != "target":
                    reasons.append("not_target_scope")
                if own != country:
                    reasons.append(
                        "country_unconfirmed"
                        if own is None
                        else "foreign_stock_country"
                    )
                status = str(
                    (raw.get("availability_evidence") or {}).get("value")
                    or raw.get("availability_status")
                    or raw.get("status")
                    or ""
                ).casefold()
                if (
                    raw.get("reserved") is True
                    or raw.get("sold") is True
                    or status in {"sold", "reserved", "verkocht", "verkauft"}
                ):
                    reasons.append("own_sold_or_reserved")
                if reasons:
                    excluded.append(
                        {
                            "country": country,
                            "vehicle_id": card["id"],
                            "listing_id": offer["listing_id"],
                            "url": offer["url"],
                            "reasons": reasons,
                        }
                    )
                    continue
                eligible_publications += 1
                offers.append(offer)
                own_payloads.append(payload)
            if not offers:
                continue
            for check in card.get("checks", []):
                checked = timestamp(check.get("checked_at"))
                data = check.get("data") or {}
                if (
                    check["provider"] == "rdw"
                    and checked is not None
                    and checked <= clock
                    and data.get("merk")
                ):
                    brands = [selector["make"], *selector.get("make_aliases", [])]
                    if normalized(data["merk"]) not in {normalized(b) for b in brands}:
                        root_hold = True
                        identity_conflict = True
            for verification in card.get("model_scope_verifications", []):
                if verification.get("scope") in {"excluded", "model_conflict"}:
                    root_hold = True
                    identity_conflict = True
            good = []
            for offer in offers:
                reasons = []
                raw = latest.get(offer["listing_id"], {}).get("raw") or {}
                price = offer["price_eur"]
                if root_hold:
                    reasons.append(
                        "identity_or_model_conflict"
                        if identity_conflict
                        else "root_sold_reserved_without_newer_available"
                    )
                if not offer["current_price"]:
                    reasons.append("not_current_full_purchase_quote")
                if (
                    price is None
                    or price <= price_floor
                    or (price_ceiling is not None and price > price_ceiling)
                ):
                    reasons.append("outside_configured_price_bounds")
                if raw.get("price_currency") not in {None, "EUR"}:
                    reasons.append("non_euro_quote")
                if reasons:
                    excluded.append(
                        {
                            "country": country,
                            "vehicle_id": card["id"],
                            "listing_id": offer["listing_id"],
                            "url": offer["url"],
                            "price_eur": price,
                            "reasons": reasons,
                        }
                    )
                else:
                    good.append(offer)
            selected = (
                min(good, key=lambda o: (o["price_eur"], o["listing_id"]))
                if good
                else None
            )
            representative = selected or max(offers, key=lambda o: o["last_seen"])
            cars.append(
                {
                    "vehicle_id": card["id"],
                    "identity_verified": card["identity_status"] == "confirmed"
                    and not identity_conflict,
                    "vin": normalized_vin(card.get("vin")),
                    "kenteken": card.get("kenteken") if country == "NL" else None,
                    "price_eur": selected["price_eur"] if selected else None,
                    "year": card.get("year"),
                    "mileage_km": card.get("mileage_km"),
                    "transmission": card.get("transmission"),
                    "fuel": card.get("fuel"),
                    "damage_declared": any(damaged(p) for p in own_payloads),
                    "source": representative["source"],
                    "url": representative["url"],
                    "listing_id": representative["listing_id"],
                    "live_publications": len(offers),
                    "price_verified_at": (
                        selected["price_evidence"].get("observed_at")
                        if selected
                        else None
                    ),
                }
            )
        priced = [c for c in cars if c["price_eur"] is not None]
        years = sorted({int(c["year"]) for c in priced if c["year"] is not None})
        return {
            "country": country,
            "name": COUNTRIES[country],
            "scope": scope,
            "all_publications": sum(all_sources.values()),
            "all_source_counts": all_sources,
            "live_target_publications": eligible_publications,
            "live_ids": len(cars),
            "known_identifier_ids": sum(c["identity_verified"] for c in cars),
            "provisional_ids": sum(not c["identity_verified"] for c in cars),
            "priced_ids": len(priced),
            "unpriced_ids": len(cars) - len(priced),
            "unknown_year_ids": sum(c["year"] is None for c in priced),
            "unknown_mileage_ids": sum(c["mileage_km"] is None for c in priced),
            "damage_declared_ids": sum(c["damage_declared"] for c in cars),
            "price_stats": quantiles([c["price_eur"] for c in priced]),
            "year_stats": quantiles([c["year"] for c in priced]),
            "mileage_stats": quantiles([c["mileage_km"] for c in priced]),
            "gear_counts": dict(
                collections.Counter(c["transmission"] or "unknown" for c in cars)
            ),
            "year_price_stats": {
                str(y): quantiles([c["price_eur"] for c in priced if c["year"] == y])
                for y in years
            },
            "source_health": source_health,
            "cars": cars,
            "excluded": excluded,
        }
    finally:
        conn.close()


def comparable_groups(markets, *, minimum=5, mileage_bin=50000):
    groups = collections.defaultdict(dict)
    for country, market in markets.items():
        own = collections.defaultdict(list)
        for car in market["cars"]:
            if (
                car["price_eur"] is None
                or car["damage_declared"]
                or car["year"] is None
                or car["mileage_km"] is None
                or car["transmission"] not in {"manual", "automatic"}
            ):
                continue
            low = int(car["mileage_km"] // mileage_bin) * mileage_bin
            own[(int(car["year"]), car["transmission"], low)].append(car["price_eur"])
        for key, values in own.items():
            groups[key][country] = quantiles(values)
    rows = []
    for (year, gear, low), samples in groups.items():
        if not all(samples.get(c, {}).get("n", 0) >= minimum for c in markets):
            continue
        row = {
            "year": year,
            "transmission": gear,
            "mileage_low": low,
            "mileage_high": low + mileage_bin,
            "label": f"{year} · {gear} · {low / 1000:g}–{(low + mileage_bin) / 1000:g}k km",
            "markets": samples,
            "weight": min(samples[c]["n"] for c in markets),
        }
        if "NL" in markets and "DE" in markets:
            row["delta_eur"] = samples["DE"]["median"] - samples["NL"]["median"]
            row["delta_pct"] = 100 * (
                samples["DE"]["median"] / samples["NL"]["median"] - 1
            )
        rows.append(row)
    # Select by support, not by the largest apparent discount; show all cells in CSV.
    return sorted(
        rows,
        key=lambda r: (-r["weight"], -r["year"], r["transmission"], r["mileage_low"]),
    )


def write_csv(path, rows, columns):
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def generate(
    primary,
    *,
    country="NL",
    secondary=None,
    secondary_country="DE",
    output,
    title=None,
    as_of=None,
    freshness_hours=13,
    minimum=5,
    mileage_bin=50000,
    price_floor=1,
    price_ceiling=None,
):
    if country not in COUNTRIES or secondary_country not in COUNTRIES:
        raise ValueError("Report country must be NL or DE")
    if secondary and country == secondary_country:
        raise ValueError("A country comparison needs two different countries")
    if (
        minimum < 1
        or mileage_bin < 1
        or not math.isfinite(freshness_hours)
        or freshness_hours < 0
    ):
        raise ValueError("Invalid cohort size, mileage interval or freshness window")
    if (
        not math.isfinite(price_floor)
        or price_floor < 0
        or (
            price_ceiling is not None
            and (not math.isfinite(price_ceiling) or price_ceiling <= price_floor)
        )
    ):
        raise ValueError("Invalid report price bounds")
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = root / stamp
    with tempfile.TemporaryDirectory(prefix=".report-", dir=root) as temporary:
        stage = Path(temporary)
        manifest = {
            country: snapshot(primary, stage / (country.lower() + "-snapshot.sqlite3"))
        }
        if secondary:
            if Path(primary).resolve() == Path(secondary).resolve():
                raise ValueError("Cannot compare a database with itself")
            manifest[secondary_country] = snapshot(
                secondary, stage / (secondary_country.lower() + "-snapshot.sqlite3")
            )
        clock = as_of or datetime.now(UTC).isoformat()
        if timestamp(clock) is None or timestamp(clock) > datetime.now(UTC):
            raise ValueError("as_of must be a non-future ISO timestamp with timezone")
        clock = timestamp(clock).isoformat()
        markets = {
            c: analyze_market(
                stage / m["snapshot"],
                c,
                as_of=clock,
                freshness_hours=freshness_hours,
                price_floor=price_floor,
                price_ceiling=price_ceiling,
            )
            for c, m in manifest.items()
        }
        if len({scope_key(m["scope"]) for m in markets.values()}) != 1:
            raise ValueError("The databases belong to different vehicle selectors")
        groups = comparable_groups(markets, minimum=minimum, mileage_bin=mileage_bin)
        vin_sets = [
            {c["vin"] for c in m["cars"] if c["vin"] and c["identity_verified"]}
            for m in markets.values()
        ]
        common_vins = set.intersection(*vin_sets) if len(vin_sets) > 1 else set()
        analysis = {
            "schema_version": 1,
            "language": "en",
            "title": title or scope_label(next(iter(markets.values()))["scope"]),
            "as_of": clock,
            "generated_at": datetime.now(UTC).isoformat(),
            "manifest": manifest,
            "policy": {
                "freshness_hours": freshness_hours,
                "minimum_cohort_size": minimum,
                "mileage_bin_km": mileage_bin,
                "price_floor_exclusive": price_floor,
                "price_ceiling_inclusive": price_ceiling,
                "cohort_selection": "descending minimum sample support; not maximum price difference",
            },
            "markets": markets,
            "matched_cells": groups,
            "selected_cohorts": groups[:5],
            "vin_overlap_count": len(common_vins),
            "cautions": cautions(markets),
        }
        lines = [
            analysis["title"],
            " / ".join(m["name"] for m in markets.values()),
            "As of " + clock,
            "",
        ]
        for c, market in markets.items():
            median = market["price_stats"]["median"]
            amount = "unknown" if median is None else f"EUR {median:,.2f}"
            lines.append(
                f"{c}: median asking price {amount}; {market['priced_ids']} eligible prices from {market['live_ids']} catalog IDs. {market['provisional_ids']} IDs are provisional."
            )
        if groups:
            focus = groups[0]
            lines.extend(["", "Most supported comparable group: " + focus["label"]])
            for c, sample in focus["markets"].items():
                lines.append(
                    f"{c}: median EUR {sample['median']:,.2f}; n={sample['n']}; middle 50% EUR {sample['q1']:,.2f}–{sample['q3']:,.2f}."
                )
        else:
            lines.append(
                "No comparable group meets the sample threshold; no cross-market difference is inferred."
            )
        lines.extend(["", *cautions(markets)])
        (stage / "overview.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (stage / "analysis.json").write_text(
            json.dumps(analysis, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        for c, market in markets.items():
            write_csv(
                stage / (c.lower() + "-cars.csv"),
                market["cars"],
                [
                    "vehicle_id",
                    "identity_verified",
                    "price_eur",
                    "year",
                    "mileage_km",
                    "transmission",
                    "fuel",
                    "damage_declared",
                    "source",
                    "url",
                    "listing_id",
                    "price_verified_at",
                    "live_publications",
                ],
            )
            write_csv(
                stage / (c.lower() + "-exclusions.csv"),
                market["excluded"],
                ["country", "vehicle_id", "listing_id", "url", "price_eur", "reasons"],
            )
        cohort_rows = []
        for group in groups:
            row = {
                k: group[k]
                for k in [
                    "year",
                    "transmission",
                    "mileage_low",
                    "mileage_high",
                    "weight",
                ]
            }
            for c in markets:
                row.update(
                    {c.lower() + "_" + k: v for k, v in group["markets"][c].items()}
                )
            row.update({k: group.get(k) for k in ["delta_eur", "delta_pct"]})
            cohort_rows.append(row)
        columns = (
            ["year", "transmission", "mileage_low", "mileage_high", "weight"]
            + [
                c.lower() + "_" + k
                for c in markets
                for k in ["n", "min", "q1", "median", "q3", "max"]
            ]
            + ["delta_eur", "delta_pct"]
        )
        write_csv(stage / "comparable-groups.csv", cohort_rows, columns)
        from .editorial_report import render

        render(analysis, stage)
        checksums = {p.name: file_sha256(p) for p in stage.iterdir() if p.is_file()}
        (stage / "manifest.json").write_text(
            json.dumps(
                {
                    "generated_at": analysis["generated_at"],
                    "as_of": clock,
                    "files": checksums,
                    "snapshots": manifest,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        stage.rename(destination)
    return {
        "status": "generated",
        "language": "en",
        "directory": str(destination),
        "summary_png": str(destination / "comparison-editorial.png"),
        "summary_pdf": str(destination / "comparison-editorial.pdf"),
        "details_pdf": str(destination / "comparison-editorial-details.pdf"),
        "markets": {
            c: {k: m[k] for k in ["live_ids", "priced_ids", "provisional_ids"]}
            for c, m in markets.items()
        },
    }
