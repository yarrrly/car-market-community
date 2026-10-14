"""National BMW stock through an ordinary public page without a saved profile."""

import json
from urllib.parse import parse_qs, urlsplit

from .db import now
from .fetch import Response
from .germany_bmw import (
    bmw_page_request,
    bmw_price_partition_url,
    bmw_ui_session,
    browser_bmw_ui_next,
    validate_bmw_price_request,
)


def enumerate_bmw(collector, run, query, first_response, first_request, fetch):
    started, source, key = now(), query["source"], query["key"]
    ids, targets, errors = set(), set(), []
    expected, pages = None, 0
    try:
        first, _ = collector.ingest_response(run, source, key, first_response)
        expected, pages = first.total, 1
        ids.update(item.external_id for item in first.listings)
        targets.update(
            item.external_id
            for item in first.listings
            if item.scope in {"target", "needs_confirmation"}
        )
        if first.page != 1 or first.total is None or not first.pages:
            raise ValueError("BMW_native_counter_or_page_unverified")
        limit = query.get("max_pages", 200)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("invalid_BMW_page_budget")
        if first.pages > limit:
            errors.append("BMW_page_limit")
        for number in range(2, min(first.pages, limit) + 1):
            request = bmw_page_request(first_request["url"], number, first_request["request_body"])
            response = fetch(request)
            try:
                page, _ = collector.ingest_response(run, source, key, response)
                if page.page != number or page.total != expected:
                    errors.append("BMW_page_or_counter_changed:" + str(number))
                current = {item.external_id for item in page.listings}
                if not current or current & ids:
                    errors.append("BMW_empty_or_repeated_page:" + str(number))
                if page.search_filters != first.search_filters:
                    errors.append("BMW_filters_changed:" + str(number))
                ids.update(current)
                targets.update(
                    item.external_id
                    for item in page.listings
                    if item.scope in {"target", "needs_confirmation"}
                )
                pages += 1
            except Exception as error:  # noqa: BLE001 — BMW failure does not stop other sources
                errors.append("BMW_page_" + str(number) + ":" + str(error))
                if response.status in {403, 429}:
                    break
        if len(ids) != expected:
            errors.append(f"BMW_count_mismatch:{len(ids)}/{expected}")
        if query.get("pagination_certified") is not True:
            errors.append("BMW_browser_pagination_not_certified")
    except Exception as error:  # noqa: BLE001 — ingest_response retained the original response
        errors.append(str(error))
    result = {
        "source": source,
        "query": key,
        "expected": expected,
        "received": len(ids),
        "targets": len(targets),
        "pages": pages,
        "status": "partial" if errors else "complete",
        "errors": errors,
        "ids": sorted(ids),
        "target_ids": sorted(targets),
    }
    collector.db.finish_source(
        run,
        source,
        key,
        started,
        result["status"],
        expected,
        len(ids),
        len(targets),
        pages,
        "; ".join(errors) or None,
        infer_missing=False,
    )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in {"ids", "target_ids"}}, ensure_ascii=False
        ),
        flush=True,
    )
    return result


def collect_bmw(collector, run, query):
    if collector.country != "DE" or query["source"] != "bmw_de":
        raise ValueError("BMW_requires_German_collector")
    if query.get("partition") == "bmw_native_price":
        return collect_bmw_price_partitions(collector, run, query)
    models = query.get("marketing_model_ranges", ["2_U06"])
    session = {"ui_url": query["url"], "model_ranges": models}
    with bmw_ui_session(session) as (page, native):
        if query.get("price_bounds") is not None:
            validate_bmw_price_request(native["request_body"], *query["price_bounds"])
        first_request = {
            "url": native["final_url"],
            "request_body": native["request_body"],
            "model_ranges": models,
        }
        directory = collector.db.path.parent / "enumeration"
        directory.mkdir(exist_ok=True)
        (directory / (str(run) + "-bmw-native-request.json")).write_text(
            json.dumps(first_request, ensure_ascii=False, indent=2)
        )

        def retained(value):
            stamp = now()
            if value.get("native_body"):
                collector.fetch_record(
                    run,
                    "bmw_de",
                    Response(
                        value["final_url"],
                        value["final_url"],
                        value["status"],
                        value["native_body"],
                        stamp,
                        transport="browser_native_UI_api",
                    ),
                )
            return Response(
                value["final_url"],
                value["final_url"],
                value["status"],
                value.get("body"),
                stamp,
                value.get("error"),
                "browser_native_UI_bundle",
            )

        def fetch(request):
            offset = int(parse_qs(urlsplit(request["url"]).query)["startIndex"][0])
            native = browser_bmw_ui_next(page, first_request, offset // 12 + 1)
            if query.get("price_bounds") is not None and native["status"] == 200:
                validate_bmw_price_request(native["request_body"], *query["price_bounds"])
            return retained(native)

        result = enumerate_bmw(collector, run, query, retained(native), first_request, fetch)
        (directory / (str(run) + "-" + query["key"] + "-bmw-leaf.json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        return result


def _retained(collector, run, native):
    stamp = now()
    if native.get("native_body"):
        collector.fetch_record(
            run,
            "bmw_de",
            Response(
                native["final_url"],
                native["final_url"],
                native["status"],
                native["native_body"],
                stamp,
                transport="browser_native_UI_api",
            ),
        )
    return Response(
        native["final_url"],
        native["final_url"],
        native["status"],
        native.get("body"),
        stamp,
        native.get("error"),
        "browser_native_UI_bundle",
    )


def collect_bmw_price_partitions(collector, run, query):
    """Smaller native UI searches avoid the large accumulating browser result list."""
    from collections import deque
    from itertools import pairwise

    models = query.get("marketing_model_ranges", ["2_U06"])
    started, ids, targets, leaves, errors = now(), set(), set(), [], []
    sessions = 0
    expected, root_ids, root_after = None, set(), None
    page_count = 0
    root_scope = None
    budget = query.get("partition_session_limit", 40)
    capacity = query.get("partition_capacity", 400)
    if not isinstance(budget, int) or isinstance(budget, bool) or not 3 <= budget <= 100:
        raise ValueError("invalid_BMW_partition_session_budget")
    if not isinstance(capacity, int) or isinstance(capacity, bool) or not 12 <= capacity <= 420:
        raise ValueError("invalid_BMW_partition_capacity")

    def scope_signature(body):
        contexts = []
        for original in body["searchContext"]:
            context = {key: value for key, value in original.items() if key != "price"}
            if "buNos" in context:
                context["buNos"] = sorted(context["buNos"])
            contexts.append(json.dumps(context, sort_keys=True))
        return sorted(contexts)

    def control():
        nonlocal sessions, page_count, root_scope
        sessions += 1
        with bmw_ui_session({"ui_url": query["url"], "model_ranges": models}) as (_, native):
            first = _retained(collector, run, native)
            if first.status != 200 or first.error:
                collector.fetch_record(run, "bmw_de", first)
                raise ValueError(first.error or "BMW_root_HTTP_not_200")
            validate_bmw_price_request(native["request_body"], None, None)
            signature = scope_signature(native["request_body"])
            if root_scope is None:
                root_scope = signature
            elif signature != root_scope:
                raise ValueError("BMW_root_native_scope_changed")
            page, _ = collector.ingest_response(run, "bmw_de", query["key"], first)
            page_count += 1
            return page

    try:
        root = control()
        expected = root.total
        root_ids = {item.external_id for item in root.listings}
        if root.page != 1 or expected is None:
            raise ValueError("BMW_root_native_counter_unverified")
        cuts = query.get("price_partition_cuts", [20000, 25000, 27500, 30000])
        if (
            not isinstance(cuts, list)
            or not cuts
            or any(type(x) is not int or x < 1 for x in cuts)
            or cuts != sorted(set(cuts))
        ):
            raise ValueError("invalid_BMW_partition_cuts")
        bounds = [None, *cuts, None]
        pending = deque(pairwise(bounds))
        while pending:
            low, high = pending.popleft()
            if sessions >= budget - 1:
                errors.append("BMW_partition_session_limit")
                break
            sessions += 1
            ui_url = bmw_price_partition_url(query["url"], low, high)
            try:
                with bmw_ui_session({"ui_url": ui_url, "model_ranges": models}) as (page, native):
                    first = _retained(collector, run, native)
                    if first.status != 200 or first.error:
                        collector.fetch_record(run, "bmw_de", first)
                        raise ValueError(first.error or "BMW_leaf_HTTP_not_200")
                    validate_bmw_price_request(native["request_body"], low, high)
                    if scope_signature(native["request_body"]) != root_scope:
                        raise ValueError("BMW_leaf_native_scope_changed")
                    parsed = collector._parse("bmw_de", first.body, first.final_url)
                    if parsed.total is None:
                        collector.fetch_record(run, "bmw_de", first)
                        raise ValueError("BMW_leaf_counter_unverified")
                    if parsed.total == 0 and not parsed.listings:
                        collector.fetch_record(run, "bmw_de", first)
                        key = query["key"] + f":price:{low}:{high}"
                        leaf = {
                            "source": "bmw_de",
                            "query": key,
                            "expected": 0,
                            "received": 0,
                            "targets": 0,
                            "pages": 1,
                            "ids": [],
                            "target_ids": [],
                            "bounds": [low, high],
                            "status": "complete",
                            "errors": [],
                        }
                        collector.db.finish_source(
                            run,
                            "bmw_de",
                            key,
                            now(),
                            "zero_results",
                            0,
                            0,
                            0,
                            1,
                            None,
                            infer_missing=False,
                        )
                        leaves.append(leaf)
                        page_count += 1
                        continue
                    if parsed.total > capacity:
                        collector.ingest_response(run, "bmw_de", query["key"], first)
                        page_count += 1
                        midpoint = (
                            (low + high) // 2
                            if low is not None and high is not None
                            else high // 2
                            if low is None
                            else low + 5000
                        )
                        if (low is not None and midpoint <= low) or (
                            high is not None and midpoint >= high
                        ):
                            raise ValueError("BMW_price_tie_bucket_over_capacity")
                        pending.extend([(low, midpoint), (midpoint, high)])
                        continue
                    request = {
                        "url": native["final_url"],
                        "request_body": native["request_body"],
                        "model_ranges": models,
                    }

                    def fetch(spec, request=request, low=low, high=high, page=page):
                        offset = int(parse_qs(urlsplit(spec["url"]).query)["startIndex"][0])
                        value = browser_bmw_ui_next(page, request, offset // 12 + 1)
                        if value["status"] == 200:
                            validate_bmw_price_request(value["request_body"], low, high)
                            if scope_signature(value["request_body"]) != root_scope:
                                raise ValueError("BMW_page_native_scope_changed")
                        return _retained(collector, run, value)

                    leaf_query = {
                        **query,
                        "key": query["key"] + f":price:{low}:{high}",
                        "pagination_certified": True,
                    }
                    leaf = enumerate_bmw(collector, run, leaf_query, first, request, fetch)
                    leaf["bounds"] = [low, high]
                    ids.update(leaf["ids"])
                    targets.update(leaf["target_ids"])
                    page_count += leaf["pages"]
                    leaves.append(leaf)
                    if leaf["status"] != "complete":
                        errors.append(f"BMW_partial_price_leaf:{low}:{high}")
            except Exception as error:  # noqa: BLE001 — keep completed leaves, continue independent ranges
                errors.append(f"BMW_price_leaf:{low}:{high}:{error}")
        last = control()
        root_after = last.total
        if root_after != expected:
            errors.append(f"BMW_root_counter_changed:{expected}:{root_after}")
        if not root_ids <= ids or not {item.external_id for item in last.listings} <= ids:
            errors.append("BMW_root_sample_IDs_missing")
        if len(ids) != expected:
            errors.append(f"BMW_union_counter_mismatch:{len(ids)}/{expected}")
    except Exception as error:  # noqa: BLE001 — report retained partial census rather than stop other sources
        errors.append(str(error))
    result = {
        "source": "bmw_de",
        "query": query["key"],
        "expected": expected,
        "root_after": root_after,
        "received": len(ids),
        "targets": len(targets),
        "pages": page_count,
        "sessions": sessions,
        "leaves": leaves,
        "ids": sorted(ids),
        "status": "partial" if errors else "complete",
        "errors": errors,
    }
    directory = collector.db.path.parent / "enumeration"
    directory.mkdir(exist_ok=True)
    (directory / f"{run}-bmw-price-census.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    collector.db.finish_source(
        run,
        "bmw_de",
        query["key"],
        started,
        result["status"],
        expected,
        len(ids),
        len(targets),
        page_count,
        "; ".join(errors) or None,
        infer_missing=False,
    )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in {"ids", "leaves"}}, ensure_ascii=False
        ),
        flush=True,
    )
    return result
