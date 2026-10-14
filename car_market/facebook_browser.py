"""Refresh known Marketplace cards through the owner's ordinary browser session."""

import json
import pathlib
import re

from .db import now
from .dom import explicit_gearbox
from .facebook import identity, observations
from .parse import number, plate_from_text


class CardUnavailable(ValueError):
    pass


def visible_observation(text, url, observed_at):
    """Read only the item's visible text, never authenticated HTML or session state."""
    identity(url)
    if "Seller's description" not in text:
        raise CardUnavailable("description_section_unavailable")
    header, description = text.split("Seller's description", 1)
    header = header.split("Message", 1)[0]
    description = (
        description.split("Seller information", 1)[0].split("Recently listed", 1)[0].strip()
    )
    title = header.splitlines()[0].strip()
    if not re.search(r"(?i)\bBMW\b", title):
        raise CardUnavailable("own_heading_unknown")
    location = re.search(r"\bin ([^\n]+)", header)
    city = location.group(1).strip() if location else None
    if not city or not re.search(r",\s*(?:NH|NB|ZH|UT|GE|FR|GR|DR|OV|FL|LI|ZE)\b", city):
        raise CardUnavailable("nl_location_unconfirmed")
    price = re.search(r"^€\s*([\d.,]+)", header, re.MULTILINE)
    year = re.search(r"(?i)(?:bouwjaar|year)\s*[:\n]\s*(20\d{2}|19\d{2})", description)
    year = year or re.search(r"\b(20\d{2}|19\d{2})\b", title)
    mileage = re.search(
        r"(?i)(?:mileage|kilometerstand|tellerstand|driven)\s*[:\n]?\s*(?:circa\s+)?([\d., ]+)\s*(?:km|kilomet)",
        description,
    ) or re.search(r"(?i)\bkm\s*[:=]?\s*(\d[\d., ]*)", title)
    transmission = explicit_gearbox(description)
    if re.search(r"(?i)(?:transmission\s*:\s*automatic|automatic gearbox)", description):
        transmission = "automatic"
    record = {
        "url": url,
        "title": title,
        "description": description,
        "observed_at": observed_at,
        "country": "NL",
        "city": city,
        "price": number(price.group(1)) if price else None,
        "currency": "EUR",
        "year": int(year.group(1)) if year else None,
        "mileage": int(number(mileage.group(1))) if mileage else None,
        "transmission": transmission,
        "fuel": "petrol"
        if re.search(r"(?i)\b(?:benzine|petrol|gasoline)\b", description)
        else None,
        "kenteken": plate_from_text(description),
    }
    if re.search(r"(?im)^sold\s*$", header):
        record["availability"] = "seller_marked_sold"
    observations([record])
    return record


def read_known(urls, profile):
    profile = pathlib.Path(profile).expanduser()
    if not profile.is_dir():
        return [], ["session_profile_missing"]
    from playwright.sync_api import sync_playwright

    records, errors = [], []
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=False, locale="nl-NL"
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            for url in urls:
                identity(url)
                response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if (response and response.status in (403, 429)) or any(
                    marker in page.url
                    for marker in ("checkpoint", "login", "two_step_verification")
                ):
                    errors.append("access_required")
                    break
                try:
                    page.get_by_text("Seller's description", exact=True).wait_for(timeout=8000)
                    headings = [
                        node.inner_text().strip()
                        for node in page.locator("h1").all()
                        if node.is_visible() and re.search(r"(?i)BMW|218|tourer", node.inner_text())
                    ]
                    if len(headings) != 1:
                        raise CardUnavailable("own_heading_unknown")
                    body = page.locator("body").inner_text()
                    start = body.find(headings[0])
                    if start < 0:
                        raise CardUnavailable("own_heading_unknown")
                    end = body.find("Seller information", start)
                    if end < 0:
                        raise CardUnavailable("own_description_boundary_unknown")
                    records.append(visible_observation(body[start:end], url, now()))
                except CardUnavailable as error:
                    errors.append(str(error))
                except Exception as error:  # noqa: BLE001 — preserve unknown data and continue other known cards
                    errors.append(type(error).__name__)
        finally:
            context.close()
    return records, errors


def refresh_known(db, run, config):
    started = now()
    urls = [
        row[0]
        for row in db.conn.execute(
            "SELECT url FROM listings WHERE source='facebook' ORDER BY last_seen,id LIMIT ?",
            (config.get("limit", 25),),
        )
    ]
    records, errors = [], []
    if urls:
        try:
            records, errors = read_known(urls, config["profile_path"])
        except Exception as error:  # noqa: BLE001 — browser/session failures must not interrupt other sources
            errors = ["browser_unavailable:" + type(error).__name__]
    accepted = 0
    for item, observed in observations(records) if records else []:
        if item.scope == "excluded":
            continue
        fetch = db.save_fetch(
            run,
            "facebook",
            item.url,
            item.url,
            200,
            json.dumps(item.__dict__, ensure_ascii=False),
            observed,
            "authenticated_visible_card",
        )
        listing = db.observe(run, item, observed, fetch, query_key="known_refresh")
        db.conn.execute(
            "UPDATE listings SET availability=? WHERE id=?",
            (item.raw["seller_availability"], listing),
        )
        db.conn.commit()
        accepted += 1
    # Known URLs are a subset, even when every requested card was refreshed successfully.
    auth_error = any(
        error in {"access_required", "session_profile_missing"}
        or error.startswith("browser_unavailable:")
        for error in errors
    )
    status = "access_required" if auth_error and not records else "partial"
    db.finish_source(
        run,
        "facebook",
        "known_refresh",
        started,
        status,
        None,
        len(records),
        accepted,
        len(urls),
        "; ".join(["known_subset; total_inventory_unknown", *errors]),
        infer_missing=False,
    )
    return {
        "source": "facebook",
        "status": status,
        "requested": len(urls),
        "refreshed": accepted,
        "errors": errors,
    }
