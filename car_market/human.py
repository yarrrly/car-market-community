"""Readable terminal summaries; scripts and pipes keep the JSON output."""

import re
import shutil

NOTES = [
    (r"^page_limit_exceeded$", "stopped at the page limit; raise --max-pages to read more"),
    (r"^count_mismatch:(\d+)/(\d+)$", r"read \1 of the \2 results the site reports"),
    (
        r"^model_route_not_found_used_make_route$",
        "the site has no page for this model name; searched the whole make instead",
    ),
    (
        r"^make_route_not_found$",
        "the site has no page for this make either; check the spelling of --make and --model",
    ),
    (r"^access_response_held$", "the site refused access (login, CAPTCHA or block page)"),
    (r"^http_429$", "the site asked to slow down (HTTP 429); later passes back off"),
    (r"^http_403$", "the site refused access (HTTP 403)"),
    (r"^http_(\d+)$", r"the site answered HTTP \1"),
    (r"^(\d+) deferred$", r"\1 detail pages left for the next pass"),
    (r"^(\d+) errors$", r"\1 detail errors"),
    (r"^(\d+) in backoff$", r"\1 detail pages waiting after errors"),
    (r"^detail_backlog:(\d+)$", r"\1 detail pages left for the next pass"),
]


def explain(error):
    """Turn a stored source note into plain sentences."""
    out = []
    for part in re.split(r";\s*", error or ""):
        part = part.strip()
        if not part or part in {"0 errors", "0 in backoff", "0 deferred"}:
            continue
        for pattern, text in NOTES:
            if re.match(pattern, part):
                out.append(re.sub(pattern, text, part))
                break
        else:
            out.append(part)
    return out


def when(value):
    """2026-10-14T07:40:43.61+00:00 -> 2026-10-14 07:40 UTC."""
    match = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", value or "")
    return f"{match.group(1)} {match.group(2)} UTC" if match else (value or "not finished")


def euro(value):
    return "—" if value is None else "€" + f"{value:,.0f}".replace(",", " ")


def km(value):
    return "—" if value is None else f"{value:,.0f}".replace(",", " ") + " km"


def width():
    return max(80, min(shutil.get_terminal_size((120, 24)).columns, 160))


def listings(result):
    rows = result.get("advertisements", [])
    hidden = result.get("hidden_not_seen_recently") or 0
    stale_hint = (
        f"{hidden} older listings are hidden because they were not seen in the last pass window; "
        "show them with: car-market list --include-stale"
    )
    if not rows:
        return (
            (stale_hint + "\n" if hidden else "")
            + "No matching listings yet.\n"
            "Try: car-market list --include-pending   (also show listings that still need a check)\n"
            "     car-market status                   (see what the last pass did)"
        )
    total = width()
    title_width = max(20, total - 80)
    lines = [
        f"{'ID':8}  {'PRICE':>9}  {'YEAR':4}  {'MILEAGE':>10}  {'GEAR':9}  {'FUEL':8}  {'TITLE':{title_width}}  CITY",
    ]
    for row in rows:
        title = " ".join((row.get("title") or "").split())
        if len(title) > title_width:
            title = title[: title_width - 1] + "…"
        lines.append(
            f"{(row.get('vehicle_id') or '—')[:8]:8}  {euro(row.get('price_eur')):>9}  {str(row.get('year') or '—'):4}  "
            f"{km(row.get('mileage_km')):>10}  {(row.get('transmission') or '—'):9}  "
            f"{(row.get('fuel') or '—'):8}  {title:{title_width}}  {(row.get('city') or '—').title()}"
        )
    lines += [
        "",
        f"{len(rows)} listings (publications; one car can appear on several sites).",
        *([stale_hint] if hidden else []),
        "Details, links and history: car-market vehicle <ID>   ·   everything: car-market list --json",
    ]
    return "\n".join(lines)


def status(result):
    summary = result.get("summary", {})
    lines = [
        f"Search configuration: {result.get('config', 'unknown')}",
        f"Database: {result.get('database', 'unknown')}",
        f"Background collector: {result.get('daemon', 'unknown').replace('_', ' ')}",
    ]
    if result.get("permission_error"):
        lines.append("Problem: " + result["permission_error"])
    if summary:
        lines += [
            "",
            f"Listings saved: {summary.get('listings', 0)}",
            f"Match your search: {summary.get('purchase_candidates', 0)}",
            f"Still need a check: {result.get('pending_discoveries', 0)} discoveries",
        ]
        sources = summary.get("latest_sources") or []
        if sources:
            lines += ["", "Last pass by source:"]
        for source in sources:
            expected = source.get("expected_count")
            counts = f"{source.get('received_count', 0)}" + (
                f" of {expected}" if expected is not None else ""
            )
            lines.append(
                f"  {source.get('source')}: {source.get('status')}, {counts} read"
                f" ({when(source.get('finished_at') or source.get('started_at'))})"
            )
            for note in explain(source.get("error")):
                lines.append("    - " + note)
    else:
        lines += ["", "No data yet. Start with: car-market research --resume  or  car-market collect"]
    lines += ["", "Full details: car-market status --json"]
    return "\n".join(lines)


def coverage(result):
    lines = []
    for query in result.get("queries", []):
        expected = query.get("expected_count")
        percent = query.get("query_enumeration_percent")
        lines.append(f"{query.get('source')} ({query.get('query_key')}): {query.get('status') or 'not run'}")
        lines.append(f"  {query.get('url')}")
        if expected is not None:
            lines.append(
                f"  read {query.get('received_count', 0)} of {expected} results"
                + (f" ({percent}%)" if percent is not None else "")
            )
        lines.append(
            f"  {query.get('target_ads', 0)} match your search, "
            f"{query.get('unconfirmed_ads', 0)} still need a check"
        )
        for note in explain(query.get("error")):
            lines.append("  - " + note)
        if query.get("stale"):
            lines.append("  - this result is older than the freshness limit")
    if not lines:
        lines.append("No configured searches have run yet.")
    lines += [
        "",
        "The percentage describes this one search on this site, not the whole market.",
        "Full details: car-market coverage --json",
    ]
    return "\n".join(lines)


def quality(result):
    lines = []
    for source in result.get("sources", []):
        lines.append(
            f"{source.get('source')}: {source.get('observed_ads', 0)} listings, "
            f"{source.get('target_ads', 0)} match, {source.get('unconfirmed_ads', 0)} need a check"
        )
        fields = source.get("fields", {})
        filled = [
            f"{name} {values.get('present_percent', 0):.0f}%"
            for name, values in fields.items()
            if isinstance(values, dict) and "present_percent" in values
        ]
        if filled:
            lines.append("  filled: " + ", ".join(filled))
        problems = {
            "model_conflict_ads": "listings contradict the model",
            "stale_ads": "listings not seen recently",
            "invalid_calendar_year_ads": "listings with an impossible year",
            "country_proof_missing_ads": "listings without a confirmed country",
        }
        for key, text in problems.items():
            if source.get(key):
                lines.append(f"  - {source[key]} {text}")
    if not lines:
        lines.append("No listings yet.")
    lines += ["", "Full details: car-market quality --json"]
    return "\n".join(lines)


SOURCE_NOTES = {
    "autoscout24": "Default for NL. Search pages and listing details.",
    "autoscout24_de": "Default for DE. Use cy=D in copied URLs.",
    "marktplaats": "Pass a /q/... search URL or the /lrp/api/search URL with --url.",
    "gaspedaal": "Discovery only: cards do not confirm country; details stay pending.",
    "autotrack": "Country is confirmed from each listing's own detail page.",
}


def sources(adapters):
    lines = [f"{'ID':16}{'COUNTRY':9}{'SITE':22}NOTES"]
    for name, adapter in adapters.items():
        site = sorted(adapter["hosts"], key=len)[-1]
        lines.append(f"{name:16}{adapter['country']:9}{site:22}{SOURCE_NOTES.get(name, '')}")
    lines += [
        "",
        "Add a site to a search with: car-market init ... --url <search URL copied from that site>",
    ]
    return "\n".join(lines)


def discoveries(rows):
    if not rows:
        return "No listings are waiting for a check."
    lines = [f"{len(rows)} listings still need a check before they count:"]
    for row in rows[:50]:
        reasons = ", ".join(row.get("reasons") or []) if isinstance(row, dict) else ""
        title = (row.get("title") if isinstance(row, dict) else None) or ""
        lines.append(f"  {title[:70]}" + (f"  ({reasons})" if reasons else ""))
    if len(rows) > 50:
        lines.append(f"  … and {len(rows) - 50} more (car-market discoveries --json)")
    return "\n".join(lines)


def report(result):
    lines = ["Report saved: " + str(result.get("directory"))]
    if result.get("summary_pdf"):
        lines.append("Overview PDF: " + result["summary_pdf"])
    if result.get("details_pdf"):
        lines.append("Details PDF:  " + result["details_pdf"])
    for country, market in (result.get("markets") or {}).items():
        lines.append(
            f"{country}: {market.get('priced_ids', 0)} priced listings"
            f" ({market.get('live_ids', 0)} in the sample)"
        )
    lines += [
        "",
        "With Docker, copy it to this computer with: ./start.sh reports  (Windows: .\\start.ps1 reports)",
    ]
    return "\n".join(lines)


def vehicle(card):
    if not card:
        return "No vehicle with this ID. Copy an ID from: car-market list"
    lines = [
        " ".join((card.get("title") or "Untitled").split()),
        "  ".join(
            x
            for x in [
                str(card.get("year") or "year unknown"),
                km(card.get("mileage_km")),
                card.get("fuel") or "fuel unknown",
                card.get("transmission") or "gear unknown",
            ]
        ),
    ]
    ident = [f"plate {card['kenteken']}" if card.get("kenteken") else None, f"VIN {card['vin']}" if card.get("vin") else None]
    if any(ident):
        lines.append(", ".join(x for x in ident if x))
    if card.get("seller") or card.get("city"):
        lines.append("Seller: " + ", ".join(x for x in [card.get("seller"), (card.get("city") or "").title()] if x))
    low, high = card.get("price_min_eur"), card.get("price_max_eur")
    if low is not None:
        lines.append("Asking price: " + (euro(low) if low == high else f"{euro(low)} – {euro(high)}"))
    offers = card.get("offers") or []
    if offers:
        lines += ["", "Listings:"]
        for offer in offers:
            state = "live" if offer.get("is_live") else offer.get("availability") or "not seen recently"
            lines.append(
                f"  {offer.get('source')}: {euro(offer.get('price_eur'))}, {state}, last seen {when(offer.get('last_seen'))}"
            )
            if offer.get("url"):
                lines.append("    " + offer["url"])
    conflicts = [k for k, v in (card.get("field_conflicts") or {}).items() if v]
    if conflicts:
        lines.append("Sources disagree on: " + ", ".join(conflicts))
    description = " ".join((card.get("description") or "").split())
    if description:
        lines += ["", (description[:400] + "…") if len(description) > 400 else description]
    if card.get("photos"):
        lines.append(f"{len(card['photos'])} photos (links: car-market vehicle {card.get('id')} --json)")
    lines += ["", "Full evidence: car-market vehicle " + str(card.get("id")) + " --json"]
    return "\n".join(lines)


def export(result):
    if not isinstance(result, dict):
        return str(result)
    lines = ["Exports written to " + str(result.get("directory", "exports/")) + ":"]
    for key, text in [
        ("listings", "listings"),
        ("vehicle_identities", "vehicles"),
        ("purchase_candidates", "matching your search"),
        ("observations", "observations (one per listing per pass)"),
    ]:
        if key in result:
            lines.append(f"  {result[key]} {text}")
    lines.append("CSV and JSON for spreadsheets, plus a consistent SQLite copy. Details: --json")
    return "\n".join(lines)
