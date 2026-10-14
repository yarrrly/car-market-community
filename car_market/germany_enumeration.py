"""Bounded AutoScout DE census beyond the public cap of 200×20.

Contract verified with tests/fixtures/germany_autoscout responses from 07.10.2026:
5790 at the root, 2469 up to €20000 and 3321 from €20001. Only the calling
collector starts network traversal; importing this module starts nothing.
"""

import math
import re
import statistics
from collections import deque
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .db import now
from .germany_autoscout import PAGE_CAP, PAGE_SIZE, _state, autoscout_de_page_url


def autoscout_next_url(url, page, first):
    """Same /lst path and query, with the Next build taken from the first page."""
    checked = autoscout_de_page_url(url, page)
    filters = getattr(first, "search_filters", {})
    if filters.get("country") != "DE" or filters.get("cy") not in ("D", "DE"):
        raise ValueError("autoscout_de_country_required")
    build = filters.get("build_id")
    if not isinstance(build, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", build):
        raise ValueError("autoscout_next_build_unavailable")
    parsed = urlsplit(checked)
    path = parsed.path.rstrip("/")
    if path.startswith("/_next/data/"):
        match = re.fullmatch(r"/_next/data/[^/]+(/lst/[^?]+)\.json", path)
        if not match:
            raise ValueError("invalid_autoscout_next_path")
        path = match[1]
    if (
        not re.fullmatch(r"/lst/[A-Za-z0-9_/-]+", path)
        or "//" in path
        or any(x in (".", "..") for x in path.split("/"))
    ):
        raise ValueError("invalid_autoscout_search_path")
    return urlunsplit(parsed._replace(path=f"/_next/data/{build}{path}.json", fragment=""))


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _price(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def _bounded_setting(query, name, default, maximum):
    value = query.get(name, default)
    if _integer(value) is None or value > maximum:
        raise ValueError("invalid_" + name)
    return value


def _interval_url(url, lower, upper):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query.pop("page", None)
    for name, value in (("pricefrom", lower), ("priceto", upper)):
        if value is None:
            query.pop(name, None)
        else:
            query[name] = [str(value)]
    return urlunsplit(parsed._replace(query=urlencode(query, doseq=True), fragment=""))


def _base_filters(page):
    return {
        k: v
        for k, v in page.search_filters.items()
        if k not in ("pricefrom", "priceto", "build_id")
    }


def _same_filters(first, second):
    # The deployment version may change; source filters and price bounds must not.
    return {k: v for k, v in first.search_filters.items() if k != "build_id"} == {
        k: v for k, v in second.search_filters.items() if k != "build_id"
    }


def _html_page_url(url, page):
    checked = autoscout_de_page_url(url, page)
    parsed = urlsplit(checked)
    if parsed.path.startswith("/_next/data/"):
        match = re.fullmatch(r"/_next/data/[^/]+(/lst/[^?]+)\.json", parsed.path)
        if not match:
            raise ValueError("invalid_autoscout_next_path")
        checked = urlunsplit(parsed._replace(path=match[1]))
    return checked


def _blocked_response(response):
    body = getattr(response, "body", "")
    text = (
        str(getattr(response, "error", "") or "")
        + " "
        + (body[:4096] if isinstance(body, str) else "")
    ).lower()
    return getattr(response, "status", None) in (403, 429) or any(
        token in text
        for token in (
            "captcha",
            "challenge",
            "http_403",
            "http_429",
            "just a moment",
            "nur einen moment",
        )
    )


def _wrong_next_page(collector, source, response, number, first):
    """Only explicitly contradictory source payloads, not mere parsing failures."""
    try:
        preview = collector._parse(source, response.body or "", response.final_url)
    except Exception:  # noqa: BLE001 — a different format does not prove a wrong page
        preview = None
    if preview is not None and _integer(preview.page) is not None:
        if preview.page != number:
            return "next_page_mismatch"
        if preview.search_filters.get("country") == "DE" and not _same_filters(preview, first):
            return "next_filter_mismatch"
    # The AS parser deliberately returns an empty Page when pageQuery.page != URL.
    body = getattr(response, "body", None)
    if not isinstance(body, (str, bytes)):
        return None
    state, _, _, _ = _state(body)
    if not isinstance(state, dict) or not isinstance(state.get("listings"), list):
        return None
    own_query = state.get("pageQuery")
    if not isinstance(own_query, dict) or own_query.get("cy") not in ("D", "DE"):
        return None
    own_page = str(own_query.get("page", "1"))
    if own_page.isdigit() and 1 <= int(own_page) <= PAGE_CAP and int(own_page) != number:
        return "next_page_mismatch"
    return None


def collect_capacity_partitions(collector, run, query):
    """Retain publication observations; infer disappearance only after a complete census.

    Validate interval tree counts first, then leaf pages.
    All responses, including controls, pass through normal ingest_response.
    """
    source, key, started = query["source"], query["key"], now()
    errors, nodes, splits, leaves = [], {}, [], []
    fetched, expected = 0, None
    root = None
    root_ids, root_after_ids, observed_ids, leaf_ids, target_ids = set(), set(), set(), set(), set()
    request_limit, depth_limit, page_limit = 0, 0, PAGE_CAP
    fallbacks = []

    def error(note):
        if note not in errors:
            errors.append(note)

    def fetch(url, refresh=False):
        nonlocal fetched
        if fetched >= request_limit:
            raise ValueError("partition_request_limit")
        fetched += 1
        return collector.fetcher.get(url, refresh=refresh)

    def ingest(response):
        page, _ = collector.ingest_response(run, source, key, response)
        observed_ids.update(item.external_id for item in page.listings)
        return page

    def request(url, refresh=False):
        return ingest(fetch(url, refresh=refresh))

    def leaf_request(leaf, number):
        url = autoscout_next_url(leaf["url"], number, leaf.get("next_first", leaf["first"]))
        response = fetch(url)
        status = getattr(response, "status", None)
        reason = None
        if not _blocked_response(response):
            if status in (404, 410):
                reason = "next_build_expired"
            elif status == 200 and not getattr(response, "error", None):
                reason = _wrong_next_page(collector, source, response, number, leaf["first"])
        if not reason:
            return ingest(response), url, None
        # Retain the invalid Next response before the alternative; it cannot add foreign IDs
        # to the census. Allow exactly one alternative, charged to the shared request budget.
        collector.fetch_record(run, source, response, retain_raw=True)
        html_url = _html_page_url(leaf["url"], number)
        recovery = {
            "page": number,
            "next_url": url,
            "next_status": status,
            "reason": reason,
            "html_url": html_url,
            "recovered": False,
        }
        fallbacks.append(recovery)
        return request(html_url, refresh=True), html_url, recovery

    def validate(page, url, number, reference=None):
        valid = True

        def bad(reason):
            nonlocal valid
            valid = False
            error(reason + ":" + url)

        if _integer(page.total) is None:
            bad("partition_counter_unavailable")
        if page.page != number:
            bad("page_number_mismatch")
        if page.search_filters.get("country") != "DE" or any(
            item.raw.get("market_country") != "DE" for item in page.listings
        ):
            bad("country_unconfirmed")
        if reference is not None and not _same_filters(page, reference):
            bad("search_filters_changed")
        if root is not None and _base_filters(page) != _base_filters(root):
            bad("partition_filters_changed")
        requested = parse_qs(urlsplit(url).query)
        for name in ("pricefrom", "priceto"):
            values = requested.get(name)
            actual = page.search_filters.get(name)
            if (values and (len(values) != 1 or str(actual) != values[0])) or (
                not values and actual not in (None, "")
            ):
                bad("partition_price_filter_changed")
        if (
            page.search_filters.get("page_size") != PAGE_SIZE
            or page.search_filters.get("page_cap") != PAGE_CAP
        ):
            bad("unverified_source_capacity")
        total = _integer(page.total)
        if total is not None:
            pages = min(math.ceil(total / PAGE_SIZE), PAGE_CAP)
            if page.pages not in ((0, 1) if total == 0 else (pages,)):
                bad("source_page_count_mismatch")
            expected_cards = min(PAGE_SIZE, max(0, total - (number - 1) * PAGE_SIZE))
            ids = {item.external_id for item in page.listings}
            if (
                len(ids) != len(page.listings)
                or len(ids) != expected_cards
                or any(not isinstance(identity, str) or not identity for identity in ids)
            ):
                bad("page_card_count_mismatch")
        return valid

    def add_leaf_page(leaf, page):
        current = {item.external_id for item in page.listings}
        if current & leaf["ids"]:
            error("repeated_page_ids:" + leaf["url"])
        other_leaf_ids = leaf_ids - leaf["ids"]
        # Also check overlap for each leaf: subtracting one set must not hide
        # an overlap that already exists between two leaves.
        if current & other_leaf_ids or any(
            current & other["ids"] for other in leaves if other is not leaf
        ):
            error("partition_id_overlap:" + leaf["url"])
        for item in page.listings:
            amount = _price(item.price)
            lower, upper = leaf["bounds"]
            if amount is None and (lower is not None or upper is not None):
                error("unknown_price_in_partition:" + item.external_id)
            elif amount is not None and (
                (lower is not None and amount < lower) or (upper is not None and amount > upper)
            ):
                error("price_outside_partition:" + item.external_id)
            if item.scope != "excluded" and item.raw.get("market_country") == "DE":
                target_ids.add(item.external_id)
        leaf["ids"].update(current)
        leaf_ids.update(current)

    try:
        if source != "autoscout24_de" or getattr(collector, "country", None) != "DE":
            raise ValueError("capacity_partition_requires_autoscout_de")
        request_limit = _bounded_setting(query, "partition_request_limit", 512, 2000)
        depth_limit = _bounded_setting(query, "partition_depth_limit", 12, 20)
        for name in ("max_pages", "page_limit"):
            if name in query:
                page_limit = min(page_limit, _bounded_setting(query, name, PAGE_CAP, PAGE_CAP))
        if query.get("coverage", "enumerate") != "enumerate":
            error("pagination_not_certified")
        # This call validates the public host, cy=D/DE and route before network access.
        seed = autoscout_de_page_url(query["url"], 1)
        parsed = parse_qs(urlsplit(seed).query)
        bounds = []
        for name in ("pricefrom", "priceto"):
            values = parsed.get(name)
            if values is not None and (len(values) != 1 or not values[0].isdigit()):
                raise ValueError("invalid_partition_bounds")
            bounds.append(int(values[0]) if values else None)
        lower, upper = bounds
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("invalid_partition_bounds")
        pending = deque([(seed, lower, upper, 0)])
        while pending:
            if fetched >= request_limit:
                error("partition_request_limit")
                break
            url, lower, upper, depth = pending.popleft()
            try:
                page = request(url)
                if root is None:
                    root = page
                    expected = _integer(page.total)
                    root_ids.update(item.external_id for item in page.listings)
                valid = validate(page, url, 1)
                interval = (lower, upper)
                nodes[interval] = {"total": _integer(page.total), "url": url}
                if not valid:
                    continue
                if page.total <= PAGE_CAP * PAGE_SIZE:
                    leaf = {
                        "bounds": interval,
                        "url": url,
                        "first": page,
                        "total": page.total,
                        "ids": set(),
                        "pages_received": 1,
                    }
                    add_leaf_page(leaf, page)
                    leaves.append(leaf)
                    continue
                if depth >= depth_limit:
                    error("partition_depth_limit:" + url)
                    continue
                if upper is not None and upper <= (lower or 0):
                    error("unsplittable_price_capacity:" + url)
                    continue
                prices = [_price(item.price) for item in page.listings]
                if any(value is None for value in prices):
                    error("unknown_price_in_split:" + url)
                known = [value for value in prices if value is not None]
                if (
                    depth == 0
                    and (lower is None or lower <= 20000)
                    and (upper is None or upper > 20000)
                ):
                    pivot = 20000
                elif upper is not None:
                    pivot = ((lower or 0) + upper) // 2
                elif known:
                    pivot = max(lower or 0, int(statistics.median(known)))
                else:
                    error("price_interval_not_enumerable:" + url)
                    continue
                if upper is not None:
                    pivot = min(upper - 1, pivot)
                if lower is not None:
                    pivot = max(lower, pivot)
                if any(pivot < value < pivot + 1 for value in known):
                    error("fractional_price_partition_gap:" + url)
                left, right = (lower, pivot), (pivot + 1, upper)
                splits.append((interval, left, right))
                pending.extend(
                    [
                        (_interval_url(url, *left), *left, depth + 1),
                        (_interval_url(url, *right), *right, depth + 1),
                    ]
                )
            except Exception as exc:  # noqa: BLE001 — the neighboring branch remains available
                error(str(exc))
        for parent, left, right in splits:
            counts = [nodes.get(interval, {}).get("total") for interval in (parent, left, right)]
            if any(value is None for value in counts):
                error("partition_branch_incomplete:" + str(parent))
            elif counts[1] + counts[2] != counts[0]:
                error(f"partition_counter_mismatch:{counts[1]}+{counts[2]}/{counts[0]}")
        # Traverse pages only in leaves; do not enumerate the capped root.
        for leaf in leaves:
            total_pages = math.ceil(leaf["total"] / PAGE_SIZE)
            if total_pages > page_limit:
                error("page_limit_exceeded:" + leaf["url"])
            for number in range(2, min(total_pages, page_limit) + 1):
                if fetched >= request_limit:
                    error("partition_request_limit")
                    break
                try:
                    page, url, recovery = leaf_request(leaf, number)
                    if not validate(page, url, number, leaf["first"]):
                        continue
                    if page.total != leaf["total"]:
                        error(f"counter_changed:{leaf['total']}->{page.total}")
                        continue
                    if recovery is not None:
                        recovery["recovered"] = True
                        # The next Next request uses the build from the validated HTML.
                        build = page.search_filters.get("build_id")
                        if isinstance(build, str) and re.fullmatch(
                            r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", build
                        ):
                            leaf["next_first"] = page
                    add_leaf_page(leaf, page)
                    leaf["pages_received"] += 1
                except Exception as exc:  # noqa: BLE001 — record incompleteness without inferring disappearance
                    error(str(exc))
            if len(leaf["ids"]) != leaf["total"]:
                error(f"leaf_count_mismatch:{len(leaf['ids'])}/{leaf['total']}:{leaf['url']}")
        leaf_total = sum(leaf["total"] for leaf in leaves)
        if expected is None:
            error("root_counter_unavailable")
        if leaf_total != expected:
            error(f"partition_sum_mismatch:{leaf_total}/{expected}")
        if len(leaf_ids) != expected:
            error(f"count_mismatch:{len(leaf_ids)}/{expected}")
        if not root_ids.issubset(leaf_ids):
            error("root_ids_not_covered")
        if not observed_ids.issubset(leaf_ids):
            error("branch_ids_not_covered")
        if not errors:
            # Repeating the root detects count/filter drift and changes in the visible sample.
            again = request(seed, refresh=True)
            if not validate(again, seed, 1, root) or again.total != expected:
                error("root_changed_during_census")
            root_after_ids.update(item.external_id for item in again.listings)
            if not root_after_ids.issubset(leaf_ids):
                error("root_after_ids_not_covered")
    except Exception as exc:  # noqa: BLE001 — even a root returning 403 remains partial
        error(str(exc))
    verified = expected is not None and not errors
    status = "complete" if verified else "partial"
    infer_missing = verified and query.get("infer_missing", True)
    collector.db.finish_source(
        run,
        source,
        key,
        started,
        status,
        expected,
        len(leaf_ids),
        len(target_ids),
        fetched,
        "; ".join(errors) or None,
        infer_missing=infer_missing,
    )
    return {
        "source": source,
        "query": key,
        "status": status,
        "expected": expected,
        "received": len(leaf_ids),
        "targets": len(target_ids),
        "pages": fetched,
        "verified_census": verified,
        "infer_missing": infer_missing,
        "fallbacks": fallbacks,
        "partition_sum": sum(leaf["total"] for leaf in leaves),
        "ids": sorted(leaf_ids),
        "root_ids": sorted(root_ids),
        "root_after_ids": sorted(root_after_ids),
        "leaves": [
            {
                "lower": leaf["bounds"][0],
                "upper": leaf["bounds"][1],
                "expected": leaf["total"],
                "received": len(leaf["ids"]),
                "pages_received": leaf["pages_received"],
            }
            for leaf in leaves
        ],
        "errors": errors,
    }
