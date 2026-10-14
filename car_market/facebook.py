"""Import explicitly supplied Facebook observations without account credentials."""

import json
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .db import now
from .parse import Listing, plate

FIELDS = {
    "availability",
    "url",
    "title",
    "description",
    "observed_at",
    "published_at",
    "country",
    "currency",
    "price",
    "mileage",
    "year",
    "transmission",
    "fuel",
    "kenteken",
    "vin",
    "seller",
    "city",
}


def timestamp(value):
    if not isinstance(value, str):
        raise TypeError("An explicit observation timestamp is required")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed > datetime.now(UTC):
        raise ValueError("Timestamps must have a timezone and cannot be in the future")
    return parsed.astimezone(UTC).isoformat()


def identity(url):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname
        not in {
            "facebook.com",
            "www.facebook.com",
            "m.facebook.com",
        }
        or parts.username
        or parts.password
        or parts.port
    ):
        raise ValueError("An HTTPS Facebook listing or post URL is required")
    match = re.fullmatch(
        r"/(marketplace/(?:np/)?item/\d+|groups/[^/]+/posts/\d+|[^/]+/posts/\d+)/?", parts.path
    )
    if not match:
        raise ValueError("Use a permalink to a Marketplace item, group post or Page post")
    path = match.group(1)
    external = path.replace("marketplace/np/item/", "marketplace/item/")
    return external, "https://www.facebook.com/" + path + "/"


def observations(document):
    if not isinstance(document, list) or not document:
        raise ValueError("Expected a nonempty JSON array of Facebook observations")
    result = []
    for record in document:
        if not isinstance(record, dict) or set(record) - FIELDS:
            raise ValueError("Unknown fields; supply listing facts only, never session data")
        if record.get("country") != "NL":
            raise ValueError("Each observation must explicitly confirm country NL")
        if not isinstance(record.get("title"), str) or not record["title"].strip():
            raise ValueError("The observed listing title is required")
        observed = timestamp(record.get("observed_at"))
        availability = record.get("availability", "observed")
        if availability not in {"observed", "seller_marked_sold"}:
            raise ValueError("Invalid seller availability")
        if re.search(r"(?i)\bverkocht+\b|\bsold\b", record["title"]):
            availability = "seller_marked_sold"
        published = timestamp(record["published_at"]) if record.get("published_at") else None
        if published and published > observed:
            raise ValueError("Publication cannot be later than observation")
        external, url = identity(record["url"])
        if record.get("price") is not None and record.get("currency") != "EUR":
            raise ValueError("Prices must explicitly be in EUR")
        for field in ("price", "mileage", "year"):
            value = record.get(field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value < float("inf")
                or (
                    field != "price"
                    and (not isinstance(value, int) or value == 0 and field == "year")
                )
            ):
                raise ValueError(f"Invalid numeric {field}")
        for field, allowed in (
            ("transmission", {None, "manual", "automatic"}),
            ("fuel", {None, "petrol", "diesel", "hybrid", "electric"}),
        ):
            if record.get(field) not in allowed:
                raise ValueError(f"Invalid {field}")
        for field in ("description", "kenteken", "vin", "seller", "city"):
            if record.get(field) is not None and not isinstance(record[field], str):
                raise ValueError(f"Invalid text {field}")
        registration = plate(record.get("kenteken"))
        if record.get("kenteken") and not registration:
            raise ValueError("Invalid registration")
        vin = record.get("vin")
        if vin:
            vin = vin.upper()
            if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
                raise ValueError("Invalid VIN")
        facts = {
            k: record[k]
            for k in (
                "price",
                "mileage",
                "year",
                "transmission",
                "fuel",
                "description",
                "seller",
                "city",
            )
            if k in record
        }
        item = Listing(
            "facebook",
            external,
            url,
            record["title"],
            kenteken=registration,
            vin=vin,
            raw={
                "access": "user_supplied",
                "country": "NL",
                "observed_at": observed,
                "published_at": published,
                "coverage": "partial",
                "seller_availability": availability,
            },
            **facts,
        ).qualify()
        result.append((item, observed))
    return result


def import_observations(db, document):
    # Validate the whole batch before writing any rows. A subset never implies disappearance.
    records = observations(document)
    accepted = 0
    with db.transaction():
        run = db.start_run("facebook_import")
        for item, observed in records:
            if item.scope == "excluded":
                continue
            evidence = json.dumps(item.__dict__, ensure_ascii=False, sort_keys=True)
            fetch = db.save_fetch(
                run,
                "facebook",
                item.url,
                item.url,
                None,
                evidence,
                observed,
                "user_supplied",
            )
            listing_id = db.observe(run, item, observed, fetch, query_key="supplied_observations")
            db.conn.execute(
                "UPDATE listings SET availability=? WHERE id=?",
                (item.raw["seller_availability"], listing_id),
            )
            accepted += 1
        db.finish_source(
            run,
            "facebook",
            "supplied_observations",
            now(),
            "partial",
            None,
            len(records),
            accepted,
            0,
            "User-supplied subset; total inventory unknown",
            infer_missing=False,
        )
        db.candidates()
        result = {
            "source": "facebook",
            "status": "partial",
            "submitted": len(records),
            "accepted": accepted,
            "excluded": len(records) - accepted,
        }
        db.finish_run(run, "imported", result)
    return result
