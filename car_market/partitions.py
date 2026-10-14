"""Traverse documented price intervals when pagination fails."""

import statistics
from urllib.parse import parse_qs, urlsplit

from .db import now
from .parse import Page, with_query


def _marktnet_categories(request, url, capacity, lower, upper, depth=0):
    """Actual categories from rubrieken.xml; the root checks their combined completeness.

    07.10.2026: BMW (40) and Schadeauto's (74) recovered 413 IDs.
    Evidence: tests/fixtures/marktnet_recovery/manifest.json.
    These two categories are not an exhaustive list of site categories.
    """

    def enumerate_category(category_url, lo, hi, level):
        page = request(category_url)
        if page.total is None:
            raise ValueError("category_counter_unavailable")
        if page.total <= capacity:
            if len({ad.external_id for ad in page.listings}) != page.total:
                raise ValueError("category_count_mismatch")
            return page
        prices = [ad.price for ad in page.listings if ad.price is not None]
        if level >= 20 or not prices or (lo is not None and hi is not None and hi <= lo):
            raise ValueError("category_price_interval_not_enumerable")
        pivot = int(statistics.median(prices))
        if lo is not None:
            pivot = max(lo, pivot)
        if hi is not None:
            pivot = min(hi - 1, pivot)
        left = enumerate_category(with_query(category_url, maxprice=pivot), lo, pivot, level + 1)
        right = enumerate_category(
            with_query(category_url, minprice=pivot + 1), pivot + 1, hi, level + 1
        )
        ads = left.listings + right.listings
        if (
            left.total + right.total != page.total
            or len({ad.external_id for ad in ads}) != page.total
        ):
            raise ValueError("category_partition_mismatch")
        return Page(ads, total=page.total)

    pages = [
        enumerate_category(with_query(url, cat_id=category), lower, upper, depth)
        for category in (40, 74)
    ]
    ads = [ad for page in pages for ad in page.listings]
    total = sum(page.total for page in pages)
    if len({ad.external_id for ad in ads}) != total:
        raise ValueError("category_overlap")
    return Page(ads, total=total)


def collect_price_partitions(collector, run, query):
    source, key, started = query["source"], query["key"], now()
    limit = query.get("partition_request_limit", 150)
    capacity = query.get("partition_page_size", 20)
    fetched, leaf_total, expected = 0, 0, None
    ids, target_ids, errors = set(), set(), []
    parsed = urlsplit(query["url"])
    marktnet_xml = (
        source == "marktnet"
        and parsed.hostname == "www.marktnet.nl"
        and parsed.path == "/zoeken.xml"
    )
    # Explicit documented c=20: on 07.10 the root without c reported 412,
    # while c=20 and p=1 reported 413; inner intervals also enumerated 413 IDs.
    root_url = (
        with_query(query["url"], c=capacity)
        if marktnet_xml and "c" not in parse_qs(parsed.query)
        else query["url"]
    )

    def request(url):
        nonlocal fetched
        if fetched >= limit:
            raise ValueError("partition_request_limit")
        fetched += 1
        page, _ = collector.ingest_response(run, source, key, collector.fetcher.get(url))
        return page

    # Bounds use whole euros; counts must match at every split.
    pending = [(root_url, None, None, 0)]
    counts, splits = {}, []
    while pending:
        if fetched >= limit:
            errors.append("partition_request_limit")
            break
        url, lower, upper, depth = pending.pop()
        recovered = False
        try:
            page = request(url)
        except Exception as error:  # noqa: BLE001 — retain incompleteness and continue with other intervals
            # Only public HTTP 500 in a general child interval.
            # 404/403 and category errors never mean an empty result set.
            if (
                marktnet_xml
                and depth > 0
                and "cat_id" not in parse_qs(urlsplit(url).query)
                and str(error) == "http_500"
            ):
                try:
                    page = _marktnet_categories(request, url, capacity, lower, upper, depth)
                    recovered = True
                except Exception as recovery_error:  # noqa: BLE001 — the root remains partial
                    errors.extend([str(error), "category_recovery_failed:" + str(recovery_error)])
                    continue
            else:
                errors.append(str(error))
                continue
        if expected is None:
            expected = page.total
        if page.total is None:
            errors.append("partition_counter_unavailable")
            continue
        counts[(lower, upper)] = page.total
        if page.total <= capacity or recovered:
            current = {ad.external_id for ad in page.listings}
            if len(current) != page.total:
                errors.append(f"partition_count_mismatch:{len(current)}/{page.total}")
            ids.update(current)
            from .model_profiles import is_mercedes

            target_ids.update(
                ad.external_id
                for ad in page.listings
                if ad.scope == "target" or (not is_mercedes(ad) and ad.scope != "excluded")
            )
            leaf_total += page.total
            continue
        prices = [ad.price for ad in page.listings if ad.price is not None]
        if (
            depth >= 20
            or not prices
            or (lower is not None and upper is not None and upper <= lower)
        ):
            errors.append("price_interval_not_enumerable:" + url)
            continue
        pivot = int(statistics.median(prices))
        if lower is not None:
            pivot = max(lower, pivot)
        if upper is not None:
            pivot = min(upper - 1, pivot)
        if lower is None and upper is not None and pivot >= upper:
            pivot = upper - 1
        # Fetch both child result sets before further splits; their total
        # checks fractional prices, unknown prices and market changes.
        left = with_query(url, maxprice=pivot)
        right = with_query(url, minprice=pivot + 1)
        # If the parent already has bounds, with_query preserves them.
        splits.append(((lower, upper), (lower, pivot), (pivot + 1, upper)))
        pending.extend([(right, pivot + 1, upper, depth + 1), (left, lower, pivot, depth + 1)])
    for parent, left, right in splits:
        if left not in counts or right not in counts:
            errors.append("partition_branch_incomplete:" + str(parent))
        elif counts[left] + counts[right] != counts[parent]:
            errors.append(
                f"partition_counter_mismatch:{counts[left]}+{counts[right]}/{counts[parent]}"
            )
    if leaf_total != expected:
        errors.append(f"partition_sum_mismatch:{leaf_total}/{expected}")
    if len(ids) != expected:
        errors.append(f"count_mismatch:{len(ids)}/{expected}")
    collector.db.finish_source(
        run,
        source,
        key,
        started,
        "complete" if not errors else "partial",
        expected,
        len(ids),
        len(target_ids),
        fetched,
        "; ".join(errors) or None,
    )
    return {
        "source": source,
        "query": key,
        "status": "complete" if not errors else "partial",
        "received": len(ids),
        "expected": expected,
        "targets": len(target_ids),
        "pages": fetched,
        "errors": errors,
    }
