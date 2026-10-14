"""Render a reproducible, explicitly synthetic example with the real report core.

Run with Python 3.12 and the existing reports extra. No provider or production
input is accepted. Only sample-report.png/pdf are written into this directory.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
REPOSITORY = Path(__file__).resolve().parents[2]
CORE_INPUT_PIN = "cc7b69d51aae49de71462637c0157ab204088092"
CAPTURE = "2026-01-15T10:00:00+00:00"
AS_OF = "2026-01-15T12:00:00+00:00"
GENERATION = "2026-01-15T12:05:00+00:00"
DEMO_LABEL = "DEMO / ILLUSTRATIVE DATA"
# year, gearbox, midpoint km, rows per country, NL price, DE price
COHORTS = (
    (2016, "manual", 135_000, 10, 11_200, 10_800),
    (2018, "manual", 115_000, 10, 14_800, 14_100),
    (2018, "automatic", 75_000, 14, 17_000, 15_200),
    (2020, "manual", 65_000, 10, 19_800, 20_400),
    (2020, "automatic", 65_000, 18, 23_200, 21_800),
    (2022, "automatic", 35_000, 14, 28_900, 27_200),
    (2023, "automatic", 20_000, 10, 32_300, 30_500),
)


def digest(wire: bytes) -> str:
    return hashlib.sha256(wire).hexdigest()


def demo_rows(country: str) -> list[dict]:
    rows = []
    for year, gearbox, mileage, count, nl_price, de_price in COHORTS:
        for offset in range(count):
            centered = offset - (count - 1) / 2
            rows.append(
                {
                    "id": f"demo-{country.lower()}-{len(rows) + 1:03d}",
                    "title": "BMW 218i Active Tourer",
                    "year": year,
                    "mileage": round(mileage + centered * 1_200),
                    "transmission": gearbox,
                    "price": round(
                        (nl_price if country == "NL" else de_price)
                        - centered * 35
                        + ((offset * 7) % 5 - 2) * 250
                        + (offset % 3 - 1) * 130
                    ),
                }
            )
    for name, fields in [
        ("missing-price", {"price": None, "year": 2019, "mileage": 72_000}),
        ("missing-year", {"price": 16_000, "year": None, "mileage": 85_000}),
        ("missing-mileage", {"price": 22_000, "year": 2020, "mileage": None}),
        (
            "pending-model",
            {
                "price": 18_500,
                "year": 2019,
                "mileage": 78_000,
                "title": "BMW Active Tourer (model unspecified)",
            },
        ),
    ]:
        rows.append(
            {
                "id": f"demo-{country.lower()}-{name}",
                "title": "BMW 218i Active Tourer",
                "transmission": "automatic",
                **fields,
            }
        )
    return rows


def seed_database(path: Path, country: str, rows: list[dict]) -> dict:
    from car_market.configuration import bind
    from car_market.db import Database
    from car_market.parse import Listing
    from car_market.search_profile import GENERIC_PROFILE, qualify

    scope = {
        "model_profile": GENERIC_PROFILE,
        "country": country,
        "selector": {
            "make": "BMW",
            "model_terms": [["218i"], ["Active Tourer"]],
            "fuels": ["petrol"],
        },
    }
    db = Database(path)
    try:
        bind(db, {"scope": scope})
        run = db.start_run("synthetic_demo", timestamp=CAPTURE)
        for row in rows:
            item = Listing(
                "Synthetic sample " + country,
                row["id"],
                "https://demo.invalid/" + row["id"],
                row["title"],
                price=row["price"],
                price_type="advertised_price",
                mileage=row["mileage"],
                year=row["year"],
                transmission=row["transmission"],
                fuel="petrol",
                raw={
                    "location": {"countryCode": country},
                    "model_brand": "BMW",
                    "synthetic_illustration": True,
                },
            )
            db.observe(run, qualify(item, scope), CAPTURE)
        db.finish_run(
            run,
            "partial",
            {
                "synthetic": True,
                "national_coverage": None,
                "reason": "Illustrative rows include an unresolved model.",
            },
        )
        counts = dict(
            db.conn.execute("SELECT scope,COUNT(*) FROM listings GROUP BY scope")
        )
        assert db.conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 0
        assert (
            db.conn.execute(
                "SELECT COUNT(*) FROM vehicles WHERE vin IS NOT NULL OR kenteken IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        return {
            "input_rows": len(rows),
            "stored_scope_counts": counts,
            "HTTP_rows": 0,
            "VIN_or_plate_values": 0,
        }
    finally:
        db.conn.close()


def disclosure_banner(width: float) -> tuple[bytes, bytes]:
    """One additive disclosure band; charts and core report layout are untouched."""
    import matplotlib.pyplot as plt

    from car_market.editorial_report import setup, text

    setup()
    fig = plt.figure(figsize=(width / 72, 1), facecolor="#202D29", dpi=200)
    text(fig, 0.045, 0.82, DEMO_LABEL, 20, color="#F8F7F3", bold=True)
    text(
        fig,
        0.045,
        0.32,
        "Seeded example only. No provider requests, live prices or national coverage.",
        9,
        color="#DFE3DD",
    )
    png, pdf = io.BytesIO(), io.BytesIO()
    fig.savefig(png, format="png", dpi=200, facecolor="#202D29")
    fig.savefig(pdf, format="pdf", facecolor="#202D29")
    plt.close(fig)
    return png.getvalue(), pdf.getvalue()


def publish_examples(generation: Path, media: Path) -> dict:
    from PIL import Image
    from pypdf import PageObject, PdfReader, PdfWriter

    original_png = Image.open(generation / "comparison-editorial.png").convert("RGB")
    overview = PdfReader(generation / "comparison-editorial.pdf")
    width = float(overview.pages[0].mediabox.width)
    png_banner, _ = disclosure_banner(width)
    band = Image.open(io.BytesIO(png_banner)).convert("RGB")
    assert band.width == original_png.width
    poster = Image.new("RGB", (original_png.width, original_png.height + band.height))
    poster.paste(band, (0, 0))
    poster.paste(original_png, (0, band.height))
    png_path, pdf_path = media / "sample-report.png", media / "sample-report.pdf"
    assert not png_path.is_symlink() and not pdf_path.is_symlink()
    poster.save(png_path, format="PNG", optimize=True)
    writer = PdfWriter()
    for name in ["comparison-editorial.pdf", "comparison-editorial-details.pdf"]:
        for page in PdfReader(generation / name).pages:
            page_width, page_height = (
                float(page.mediabox.width),
                float(page.mediabox.height),
            )
            _, banner_pdf = disclosure_banner(page_width)
            framed = PageObject.create_blank_page(
                width=page_width, height=page_height + 72
            )
            framed.merge_page(page)
            framed.merge_translated_page(
                PdfReader(io.BytesIO(banner_pdf)).pages[0], 0, page_height
            )
            writer.add_page(framed)
    stamp = "D:20260115120500+00'00'"
    writer.add_metadata(
        {
            "/Title": DEMO_LABEL + " | BMW 218i Active Tourer",
            "/Subject": "Fully synthetic NL/DE example. No live market evidence.",
            "/Creator": "Car Market core renderer with demo disclosure band",
            "/CreationDate": stamp,
            "/ModDate": stamp,
        }
    )
    with pdf_path.open("wb") as stream:
        writer.write(stream)
    pages = PdfReader(pdf_path).pages
    assert all(DEMO_LABEL in page.extract_text() for page in pages)
    assert len(pages) == 9
    return {
        "PNG": {
            "sha256": digest(png_path.read_bytes()),
            "bytes": png_path.stat().st_size,
            "pixels": list(poster.size),
            "disclosure_band_added": True,
        },
        "PDF": {
            "sha256": digest(pdf_path.read_bytes()),
            "bytes": pdf_path.stat().st_size,
            "pages": len(pages),
            "all_pages_have_DEMO_label": True,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--proof-dir",
        type=Path,
        help="Optional receipt directory outside the source worktree",
    )
    args = parser.parse_args()
    if args.proof_dir and args.proof_dir.resolve().is_relative_to(REPOSITORY):
        parser.error("Keep proof/scratch outside the source worktree.")
    actual_started = datetime.now(UTC).isoformat()
    sys.path.insert(0, str(REPOSITORY))
    with tempfile.TemporaryDirectory(
        prefix="car-market-synthetic-report-"
    ) as temporary:
        scratch = Path(temporary)
        os.environ["MPLCONFIGDIR"] = str(scratch / "matplotlib")
        os.environ["SOURCE_DATE_EPOCH"] = str(
            int(datetime.fromisoformat(GENERATION).timestamp())
        )
        from car_market import analytics
        from car_market import db as database_module

        assert (
            Path(analytics.__file__).resolve().is_relative_to(REPOSITORY / "car_market")
        )
        assert (
            Path(database_module.__file__)
            .resolve()
            .is_relative_to(REPOSITORY / "car_market")
        )

        class ExampleClock(datetime):
            @classmethod
            def now(cls, tz=None):
                fixed = datetime.fromisoformat(GENERATION)
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        identities = iter(
            uuid.uuid5(uuid.NAMESPACE_URL, f"https://demo.invalid/catalog/{index}")
            for index in range(1, 1_000)
        )
        rows = {country: demo_rows(country) for country in ["NL", "DE"]}
        with (
            patch.object(database_module, "now", return_value=CAPTURE),
            patch.object(
                database_module.uuid, "uuid4", side_effect=lambda: next(identities)
            ),
            patch.object(analytics, "datetime", ExampleClock),
        ):
            seeds = {
                country: seed_database(
                    scratch / f"{country.lower()}.sqlite3", country, own
                )
                for country, own in rows.items()
            }
            generated = analytics.generate(
                scratch / "nl.sqlite3",
                secondary=scratch / "de.sqlite3",
                output=scratch / "reports",
                title="BMW 218i Active Tourer / DEMO",
                as_of=AS_OF,
                freshness_hours=13,
                minimum=5,
                mileage_bin=50_000,
            )
        folder = Path(generated["directory"])
        analysis = json.loads((folder / "analysis.json").read_text())
        assert len(analysis["matched_cells"]) == 7
        for market in analysis["markets"].values():
            assert market["known_identifier_ids"] == 0
            assert market["priced_ids"] == 88 and market["live_ids"] == 89
        assets = publish_examples(folder, Path(__file__).resolve().parent)
        result = {
            "kind": "SYNTHETIC_ILLUSTRATION_ONLY",
            "core_input_pin": CORE_INPUT_PIN,
            "core_import": {
                "analytics": str(analytics.__file__),
                "db": str(database_module.__file__),
            },
            "core_source_sha256": {
                name: digest((REPOSITORY / "car_market" / name).read_bytes())
                for name in [
                    "analytics.py",
                    "editorial_report.py",
                    "db.py",
                    "search_profile.py",
                ]
            },
            "fixed_example_capture": CAPTURE,
            "fixed_example_as_of": AS_OF,
            "fixed_example_generation": GENERATION,
            "actual_render_started": actual_started,
            "actual_render_finished": datetime.now(UTC).isoformat(),
            "dataset_sha256": digest(
                json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
            ),
            "dataset": seeds,
            "matched_groups": analysis["matched_cells"],
            "market_summary": {
                country: {
                    key: market[key]
                    for key in [
                        "live_ids",
                        "priced_ids",
                        "provisional_ids",
                        "unknown_year_ids",
                        "unknown_mileage_ids",
                        "gear_counts",
                        "price_stats",
                    ]
                }
                for country, market in analysis["markets"].items()
            },
            "assets": assets,
            "no_http_production_native_inputs": True,
            "clock_policy": "Synthetic capture/as-of/generation clocks are distinct and fixed. Actual execution clocks are receipt-only. Neither is live market freshness.",
            "render_policy": "Real analytics/generator/renderer; only an additive DEMO band and PDF concatenation for the published examples. Runtime files are unchanged.",
            "scratch_removed_on_exit": True,
        }
        if args.proof_dir:
            args.proof_dir.mkdir(parents=True, exist_ok=True)
            (args.proof_dir / "RENDER.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )
        print(
            json.dumps(
                {
                    "assets": assets,
                    "groups": 7,
                    "synthetic_rows": sum(len(x) for x in rows.values()),
                    "capture": CAPTURE,
                    "as_of": AS_OF,
                    "example_generation": GENERATION,
                }
            )
        )


if __name__ == "__main__":
    main()
