"""C180 C205 collection with the same history and publication evidence contracts."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit

from .collect import Collector, fair_detail_jobs
from .db import now
from .model_profiles import (
    MERCEDES_PROFILE,
    bind_profile,
    qualify_mercedes,
    use_profile,
)
from .parse import Listing


class MercedesCollector(Collector):
    def __init__(self, db, config, browser=False):
        bind_profile(db, config)
        # DE's legacy binder deliberately cannot overwrite a model binding.
        super().__init__(db, config, browser)
        bind_profile(db, config)

    def _source(self, url):
        from .mercedes_official import DE_HOST, HEDIN_HOST, MOSSEL_HOST, NL_HOST

        host = urlsplit(url).hostname
        if host == DE_HOST:
            return "mercedes_benz_de"
        if host in {NL_HOST, HEDIN_HOST, MOSSEL_HOST}:
            return "mercedes_benz_nl"
        return super()._source(url)

    def _parse(self, source, body, url):
        with use_profile(MERCEDES_PROFILE, self.country):
            if source in {"autoscout24", "autoscout24_de"}:
                from .mercedes_autoscout import parse_mercedes_autoscout

                page = parse_mercedes_autoscout(body, url, self.country)
            elif source in {"mercedes_benz_nl", "mercedes_benz_de"}:
                from .mercedes_official import parse_mercedes_official

                page = parse_mercedes_official(body, url)
            elif source == "autohero" and self.country == "NL":
                from .mercedes_autohero_nl import parse_mercedes_autohero_nl

                page = parse_mercedes_autohero_nl(body, url)
            elif self.country == "DE":
                from .mercedes_de_sources import parse_mercedes_de_sources

                page = parse_mercedes_de_sources(source, body, url)
            else:
                from .mercedes_nl_sources import parse_mercedes_nl_page

                page = parse_mercedes_nl_page(source, body, url)
            for item in page.listings:
                qualify_mercedes(item, self.country)
            return page

    def _observe(self, run, item, stamp, fetch_id=None, key=None):
        with use_profile(MERCEDES_PROFILE, self.country):
            qualify_mercedes(item, self.country)
            if item.raw.get("market_country") != self.country:
                self.failures.append(
                    "country_unconfirmed:" + item.source + ":" + item.external_id
                )
                self._queue_unconfirmed(item)
                return None
            if self.country == "NL":
                from .germany import observation_transaction
                from .provenance import timestamp

                with observation_transaction(self.db):
                    old = self.db.conn.execute(
                        "SELECT availability,last_seen FROM listings WHERE source=? AND external_id=?",
                        (item.source, item.external_id),
                    ).fetchone()
                    found = self.db.observe(run, item, stamp, fetch_id, key)
                    declared = item.raw.get("availability_status")
                    state = None
                    if old and timestamp(stamp) <= timestamp(old["last_seen"]):
                        state = old["availability"]
                    elif item.raw.get("sold") is True or declared == "sold":
                        state = "seller_marked_sold"
                    elif item.raw.get("reserved") is True or declared == "reserved":
                        state = "seller_reserved"
                    elif (
                        old
                        and old["availability"]
                        in {"seller_marked_sold", "seller_reserved"}
                        and declared != "available"
                    ):
                        state = old["availability"]
                    if state and found:
                        self.db.conn.execute(
                            "UPDATE listings SET availability=? WHERE id=?",
                            (state, found),
                        )
                return found
            return super()._observe(run, item, stamp, fetch_id, key)

    def _queue_unconfirmed(self, item):
        if item.source == "marktnet" and item.scope != "excluded":
            from .collect import canonical_id

            identity = canonical_id("marktnet", item.url)
            if identity and identity.isdigit():
                self.detail_jobs.setdefault(
                    "https://www.marktnet.nl/advertentie.xml?id=" + identity,
                    (item.source, None, item),
                )
                return
        if item.scope != "excluded" and "#" not in item.url:
            self.detail_jobs.setdefault(item.url, (item.source, None, item))

    def ingest_response(
        self, run, source, key, response, queue=True, *, request_body=None
    ):
        # Own country is required for registration, but discovery IDs and raw are retained.
        if response.error or not response.body:
            self.fetch_record(run, source, response)
            raise ValueError(response.error or "empty_response")
        fetch_id = self.fetch_record(run, source, response)
        page = self._parse(source, response.body, response.final_url)
        targets = 0
        ledger = []
        for item in page.listings:
            ledger.append(
                {
                    "external_id": item.external_id,
                    "url": item.url,
                    "scope": item.scope,
                    "country": item.raw.get("market_country"),
                    "fetch_id": fetch_id,
                    "fetched_at": response.timestamp,
                }
            )
            if item.scope == "excluded":
                if item.raw.get("market_country") == self.country:
                    self._observe(run, item, response.timestamp, fetch_id, key)
                continue
            listing_id = self._observe(run, item, response.timestamp, fetch_id, key)
            targets += int(item.scope == "target" and listing_id is not None)
            if queue and listing_id:
                self.queue_details(item, listing_id)
            elif queue:
                self._queue_unconfirmed(item)
        # Keep each own page/filter separate from query membership and raw.
        requested = urlsplit(response.url)
        normalized = requested._replace(
            query=urlencode(
                sorted(
                    parse_qsl(requested.query, keep_blank_values=True),
                    key=lambda pair: pair[0],
                )
            ),
            fragment="",
        ).geturl()
        context = json.dumps(
            [normalized, request_body], sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(context.encode()).hexdigest()
        self.db.conn.execute(
            "INSERT OR REPLACE INTO monitor_state VALUES (?,?)",
            (
                "model_discovery:"
                + source
                + ":"
                + key
                + ":"
                + str(page.page or 1)
                + ":"
                + digest,
                json.dumps(
                    {
                        "run_id": run,
                        "page": page.page or 1,
                        "request_url": response.url,
                        "final_url": response.final_url,
                        "request_context_hash": digest,
                        "fetch_id": fetch_id,
                        "fetched_at": response.timestamp,
                        "total": page.total,
                        "listings": ledger,
                    }
                ),
            ),
        )
        self.db.conn.commit()
        return page, targets

    def _detail_matches(self, source, item_url, final_url, body, advertisement_id=None):
        with use_profile(MERCEDES_PROFILE, self.country):
            if source == "dealer:wiveda.de" and self.country == "DE":
                from .mercedes_wiveda_details import detail_matches

                return detail_matches(self, item_url, final_url, body, advertisement_id)
            if source in {"autoscout24", "autoscout24_de"}:
                from .mercedes_autoscout import autoscout_identity

                expected = autoscout_identity(item_url, self.country)
                page = self._parse(source, body, final_url)
                return bool(
                    expected
                    and autoscout_identity(final_url, self.country) == expected
                    and len(page.listings) == 1
                    and page.listings[0].external_id == expected
                )
            if source in {"mercedes_benz_nl", "mercedes_benz_de"}:
                page = self._parse(source, body, final_url)
                from .mercedes_official import mercedes_official_publication_id

                expected = mercedes_official_publication_id(item_url)
                return bool(
                    expected
                    and mercedes_official_publication_id(final_url) == expected
                    and len(page.listings) == 1
                    and mercedes_official_publication_id(page.listings[0].url)
                    == expected
                )
            if source == "autohero" and self.country == "NL":
                from .mercedes_autohero_nl import _identity

                expected = _identity(item_url)
                page = self._parse(source, body, final_url)
                return bool(
                    expected
                    and _identity(final_url) == expected
                    and len(page.listings) == 1
                    and page.listings[0].external_id == expected
                )
            return super()._detail_matches(
                source, item_url, final_url, body, advertisement_id
            )

    def _collect_autohero_nl(self, run, query):
        from .mercedes_autohero_nl import autohero_nl_page_request

        started = now()
        ids, errors = set(), []
        pages = targets = 0
        expected = None
        for number in range(1, min(query.get("max_pages", 200), 200) + 1):
            request = autohero_nl_page_request(
                query["url"], number, query["request_body"]
            )
            response = self.fetcher.get(
                request["url"], refresh=True, request_body=request["body"]
            )
            try:
                page, count = self.ingest_response(
                    run,
                    query["source"],
                    query["key"],
                    response,
                    request_body=request["body"],
                )
                current = {item.external_id for item in page.listings}
                if number == 1:
                    expected = page.total
                elif page.total != expected:
                    errors.append("counter_changed")
                if current & ids:
                    errors.append("duplicate_native_ids")
                ids.update(current)
                pages += 1
                targets += count
                if expected == 0 or (page.pages and number >= page.pages):
                    break
                if not current or not page.pages:
                    errors.append("pagination_uncertified")
                    break
            except Exception as error:  # noqa: BLE001 — preserve independent source failure
                errors.append(str(error))
                break
        if expected is None or len(ids) != expected:
            errors.append("native_counter_mismatch")
        properties = query["request_body"]["variables"]["search"].get("properties", {})
        if properties.get("firstPublishedDays"):
            errors.append("observed_recent_query_only")
        self.db.finish_source(
            run,
            query["source"],
            query["key"],
            started,
            "partial" if errors else "zero_results" if expected == 0 else "complete",
            expected,
            len(ids),
            targets,
            pages,
            "; ".join(errors) or None,
            infer_missing=False,
        )

    def details(self, run):
        from .bmw_detail_policy import BMWDetailPolicy, DetailDeferred
        from .germany_retained import _raw_body
        from .provenance import timestamp

        jobs = fair_detail_jobs(list(self.detail_jobs.items()))
        expected = len(jobs)
        from .mercedes_wiveda_details import collect_details

        def wiveda_job(job):
            return self.country == "DE" and self._source(job[0]) == "dealer:wiveda.de"

        native = {
            "cached": 0,
            "backoff": 0,
            "errors": 0,
            "enriched": 0,
            "completed": 0,
            "operations": 0,
            "deferred": 0,
            "held": 0,
        }
        cached = backoff = 0
        policy = BMWDetailPolicy(self, run)

        def host_deferred(job):
            nonlocal backoff
            source = job[1][0]
            try:
                # Share the accepted native exact-origin and bounded refusal lookup.
                policy._host_backoff(source, job[0], datetime.now(UTC))
            except DetailDeferred as error:
                backoff += 1
                stats = self.detail_stats.setdefault(
                    source, {"attempted": 0, "parsed": 0, "errors": 0}
                )
                stats["backoff"] = stats.get("backoff", 0) + 1
                self.failures.append(str(error) + ":" + source)
                return True
            return False

        eligible = []
        clock = datetime.now(UTC)
        for job in jobs:
            source, _, original = job[1]
            stats = self.detail_stats.setdefault(
                source, {"attempted": 0, "parsed": 0, "errors": 0}
            )
            if wiveda_job(job):
                eligible.append(job)
                continue
            if host_deferred(job):
                continue
            row = self.db.conn.execute(
                "SELECT * FROM fetches WHERE url=? OR final_url=? ORDER BY julianday(fetched_at) DESC,id DESC LIMIT 1",
                (job[0], job[0]),
            ).fetchone()
            failed = bool(row and (row["error"] or row["status"] != 200))
            hours = (
                self.config.get("detail_error_backoff_hours", 0)
                if failed
                else self.config.get("detail_cache_hours", 0)
            )
            observed = timestamp(row["fetched_at"]) if row else None
            if (
                observed
                and hours
                and not self._detail_revision_refresh(source, row, clock)
                and 0 <= (clock - observed).total_seconds() < hours * 3600
            ):
                if failed:
                    backoff += 1
                    stats["backoff"] = stats.get("backoff", 0) + 1
                    self.failures.append("detail_backoff:" + source)
                    continue
                try:
                    body = _raw_body(
                        self.db.path.parent, row["raw_path"], row["content_hash"]
                    )
                    actual = self._source(row["final_url"])
                    page = self._parse(actual, body, row["final_url"])
                    if len(page.listings) == 1:
                        own = page.listings[0]
                        if (
                            actual == "autokopen"
                            and self.country == "NL"
                            and row["url"] == job[0]
                        ):
                            from .mercedes_nl_sources import (
                                normalize_autokopen_detail_id,
                            )

                            normalize_autokopen_detail_id(
                                own, job[0], row["final_url"], body
                            )
                        identity = (
                            original.external_id
                            if actual == source
                            else own.external_id
                        )
                        if self.country == "NL" and actual == source == "autouncle":
                            from .mercedes_autouncle_cache import accepted_capture

                            accepted = accepted_capture(
                                self, row, own, original, job[0], body, clock
                            )
                        else:
                            accepted = self._accepted_detail_capture(
                                actual, identity, row
                            )
                        if (
                            accepted
                            and own.external_id == identity
                            and own.raw.get("market_country") == self.country
                            and self._detail_matches(
                                actual, job[0], row["final_url"], body, identity
                            )
                        ):
                            cached += 1
                            stats["cached"] = stats.get("cached", 0) + 1
                            continue
                except (ValueError, OSError):
                    pass
            eligible.append(job)
        jobs = eligible

        def remaining():
            return max(
                0,
                self.config.get("detail_limit", 4000)
                - getattr(self, "_secondary_detail_attempts", 0)
                - sum(
                    stats.get("attempted", 0) for stats in self.detail_stats.values()
                ),
            )

        errors = enriched = completed = generic_deferred = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            cursor = 0
            while cursor < len(jobs):
                if wiveda_job(jobs[cursor]):
                    outcome = collect_details(self, run, [jobs[cursor]])
                    for key, count in outcome.items():
                        native[key] += count
                    cursor += 1
                    continue
                if remaining() <= 0:
                    # Keep scanning: a later native accepted cache needs no HTTP.
                    job = jobs[cursor]
                    cursor += 1
                    if not host_deferred(job):
                        source, _, original = job[1]
                        self.detail_stats[source]["deferred"] = (
                            self.detail_stats[source].get("deferred", 0) + 1
                        )
                        generic_deferred += 1
                        self.failures.append(
                            "detail_budget_deferred:"
                            + source
                            + ":"
                            + original.external_id
                        )
                    continue
                batch = []
                while cursor < len(jobs) and len(batch) < min(
                    self.workers, remaining()
                ):
                    if wiveda_job(jobs[cursor]):
                        break
                    job = jobs[cursor]
                    cursor += 1
                    # Recheck after recording each bounded in-flight group. Skipped
                    # origins do not occupy HTTP slots; other origins can fill them.
                    if not host_deferred(job):
                        batch.append(job)
                if not batch:
                    if cursor < len(jobs) and wiveda_job(jobs[cursor]):
                        continue
                    break
                for (url, (source, listing_id, original)), response in zip(
                    batch, pool.map(self._detail_get, [job[0] for job in batch])
                ):
                    completed += 1
                    actual = self._source(response.final_url)
                    fetch_id = self.fetch_record(run, actual, response)
                    stats = self.detail_stats.setdefault(
                        source, {"attempted": 0, "parsed": 0, "errors": 0}
                    )
                    stats["attempted"] += 1
                    try:
                        if (
                            response.status != 200
                            or response.error
                            or not response.body
                        ):
                            if (
                                response.status in {404, 410}
                                and listing_id
                                and self._de_removal_allowed(
                                    listing_id, original, url, response
                                )
                            ):
                                self.db.conn.execute(
                                    "UPDATE listings SET availability='removed' WHERE id=?",
                                    (listing_id,),
                                )
                                self.db.conn.execute(
                                    "INSERT INTO events(run_id,listing_id,observed_at,kind,current_value) VALUES (?,?,?,?,?)",
                                    (
                                        run,
                                        listing_id,
                                        response.timestamp,
                                        "http_removed",
                                        str(response.status),
                                    ),
                                )
                            raise ValueError(response.error or "empty_detail")
                        page = self._parse(actual, response.body, response.final_url)
                        if len(page.listings) != 1 or page.total not in {None, 1}:
                            raise ValueError("detail_unparsed")
                        item = page.listings[0]
                        if (
                            actual == "autokopen"
                            and self.country == "NL"
                            and response.url == url
                        ):
                            from .mercedes_nl_sources import (
                                normalize_autokopen_detail_id,
                            )

                            normalize_autokopen_detail_id(
                                item, url, response.final_url, response.body
                            )
                        expected_id = (
                            original.external_id
                            if actual == original.source
                            else item.external_id
                        )
                        if not self._detail_matches(
                            actual, url, response.final_url, response.body, expected_id
                        ):
                            raise ValueError("detail_identity_mismatch")
                        if (
                            actual == original.source
                            and item.external_id != original.external_id
                        ):
                            raise ValueError("detail_publication_id_changed")
                        found = self._observe(run, item, response.timestamp, fetch_id)
                        if found is None:
                            raise ValueError("detail_not_accepted")
                        portal = original.source == "gaspedaal" and any(
                            isinstance(ref, dict) and ref.get("klikUrl") == url
                            for ref in original.raw.get("portalen", [])
                        )
                        if (
                            found
                            and listing_id
                            and portal
                            and (item.kenteken or item.vin)
                        ):
                            self.db.attach_identity(
                                listing_id,
                                item.kenteken,
                                item.vin,
                                evidence={
                                    "kind": "gaspedaal_portal_reference",
                                    "fetch_id": fetch_id,
                                    "detail_listing_id": found,
                                    "requested_url": url,
                                    "final_url": response.final_url,
                                },
                            )
                        enriched += int(found is not None)
                        stats["parsed"] += int(found is not None)
                    except Exception as error:  # noqa: BLE001 — retain source failure and continue other publications
                        errors += 1
                        stats["errors"] += 1
                        self.failures.append("detail:" + source + ":" + str(error))
                        self.db.conn.execute(
                            "UPDATE fetches SET error=? WHERE id=?",
                            (str(error), fetch_id),
                        )
                    self.db.conn.commit()
        cached += native["cached"]
        backoff += native["backoff"]
        errors += native["errors"]
        enriched += native["enriched"]
        deferred = len(jobs) - cursor + native["deferred"] + generic_deferred
        for _, (source, _, _) in jobs[cursor:]:
            stats = self.detail_stats[source]
            stats["deferred"] = stats.get("deferred", 0) + 1
        self.db.finish_source(
            run,
            "portal_details",
            "discovered_links",
            now(),
            "partial"
            if errors or backoff or deferred or native["held"]
            else "complete",
            expected,
            cached + completed + native["completed"],
            enriched,
            completed + native["operations"],
            f"{errors} errors; {backoff} backoff; {deferred} budget deferred; {cached} cached; {native['held']} held"
            if errors or backoff or deferred or native["held"]
            else None,
            infer_missing=False,
        )

    def _detail_get(self, url):
        if self._source(url) in {"mercedes_benz_nl", "mercedes_benz_de"}:
            from .mercedes_official import fetch_mercedes_official

            return fetch_mercedes_official(url, browser=self.browser_enabled)
        return self.fetcher.get(url, refresh=True)

    def _detail_revision_refresh(self, source, row, clock):
        from .provenance import timestamp

        if (
            not row
            or row["status"] != 200
            or row["error"] not in (None, "detail_unparsed", "detail_parse_error")
        ):
            return False
        observed = timestamp(row["fetched_at"])
        cutoffs = (
            getattr(self, "parser_refresh_since", None),
            getattr(self, "source_parser_refresh_since", {}).get(source),
        )
        return bool(
            observed
            and any(
                cutoff and observed < cutoff <= clock
                for cutoff in map(timestamp, cutoffs)
            )
        )

    def _accepted_detail_capture(self, source, external_id, row):
        # Unchanged observations may retain an earlier fetch with the identical native body.
        return self.db.conn.execute(
            "SELECT o.fetch_id,o.observed_at FROM observations o "
            "JOIN listings l ON l.id=o.listing_id JOIN fetches f ON f.id=o.fetch_id "
            "WHERE l.source=? AND l.external_id=? AND f.source=? AND f.status=200 "
            "AND f.error IS NULL AND f.content_hash=? "
            "AND julianday(o.observed_at)=julianday(f.fetched_at) LIMIT 1",
            (source, external_id, source, row["content_hash"]),
        ).fetchone()

    def _secondary_detail_eligible(self, item):
        import copy
        import re

        probe = qualify_mercedes(copy.deepcopy(item), self.country)
        if probe.scope != "excluded":
            return True
        # Missing make/model text is not a contradictory own search-card fact.
        native = item.raw.get("vehicle")
        native = native if isinstance(native, dict) else {}
        brand = item.raw.get("model_brand") or native.get("make")
        explicit = (
            brand
            or re.search(r"\bmercedes(?:[- ]benz)?\b", item.title, re.IGNORECASE)
            or item.fuel in {"diesel", "hybrid", "electric"}
            or item.transmission == "manual"
            or item.year is not None
            and item.year < 2016
        )
        return not explicit

    def _secondary_detail_get(self, run, source, url, *, original):
        from .fetch import Response
        from .germany_retained import _raw_body
        from .provenance import timestamp

        clock = datetime.now(UTC)
        row = self.db.conn.execute(
            "SELECT * FROM fetches WHERE source=? AND (url=? OR final_url=?) "
            "ORDER BY julianday(fetched_at) DESC,id DESC LIMIT 1",
            (source, url, url),
        ).fetchone()
        blocked = self.db.conn.execute(
            "SELECT * FROM fetches WHERE source=? AND status IN (403,429) "
            "AND (COALESCE(NULLIF(final_url,''),url) LIKE ? "
            "OR COALESCE(NULLIF(final_url,''),url) LIKE ?) "
            "ORDER BY julianday(fetched_at) DESC,id DESC LIMIT 1",
            (source,)
            + tuple(
                "https://" + (urlsplit(url).hostname or "") + suffix
                for suffix in ("/%", ":443/%")
            ),
        ).fetchone()

        def defer(reason, capture=None):
            self._secondary_detail_deferrals.append(
                {
                    "url": url,
                    "external_id": original.external_id,
                    "reason": reason,
                    "fetch_id": capture["id"] if capture else None,
                    "fetched_at": capture["fetched_at"] if capture else None,
                    "content_hash": capture["content_hash"] if capture else None,
                }
            )
            raise ValueError(reason)

        if source == "carwow_de":
            from .bmw_detail_policy import BMWDetailPolicy, DetailDeferred
            from .mercedes_carwow_details import request_matches, route_from_capture

            if not request_matches(self, source, url, original):
                defer("carwow_detail_owner_mismatch")
            if not self.specialized_details_enabled:
                defer("details_disabled")
            if (
                not self._secondary_detail_eligible(original)
                or original.raw.get("market_country") not in (None, "DE")
                or original.raw.get("field_conflicts")
            ):
                defer("detail_conclusively_excluded")
            try:
                BMWDetailPolicy(self, run)._host_backoff(source, url, clock)
            except DetailDeferred as error:
                capture = (
                    self.db.conn.execute(
                        "SELECT * FROM fetches WHERE id=?", (error.capture["fetch_id"],)
                    ).fetchone()
                    if error.capture
                    else None
                )
                defer(error.reason, capture)
            route = route_from_capture(self, url, row, original, clock)
            if route is not None:
                return route

        if source == "dealer:wiveda.de" and self.country == "DE":
            from .bmw_detail_policy import BMWDetailPolicy, DetailDeferred
            from .mercedes_wiveda_details import route_from_capture

            try:
                BMWDetailPolicy(self, run)._host_backoff(source, url, clock)
            except DetailDeferred as error:
                capture = (
                    self.db.conn.execute(
                        "SELECT * FROM fetches WHERE id=?", (error.capture["fetch_id"],)
                    ).fetchone()
                    if error.capture
                    else None
                )
                defer(error.reason, capture)
            if not self.specialized_details_enabled:
                defer("details_disabled")
            if route_from_capture(self, url, row, clock):
                body = _raw_body(
                    self.db.path.parent, row["raw_path"], row["content_hash"]
                )
                response = Response(
                    row["url"], row["final_url"], 200, body, row["fetched_at"]
                )
                response.secondary_fetch_id = row["id"]
                response.secondary_route_evidence = True
                return response
        blocked_at = timestamp(blocked["fetched_at"]) if blocked else None
        backoff = self.config.get("detail_error_backoff_hours", 6)
        if blocked_at and 0 <= (clock - blocked_at).total_seconds() < backoff * 3600:
            defer("detail_host_backoff:" + str(blocked["status"]), blocked)
        observed = timestamp(row["fetched_at"]) if row else None
        if observed and observed <= clock:
            failed = bool(row["error"] or row["status"] != 200)
            hours = backoff if failed else self.config.get("detail_cache_hours", 6)
            previous = self.db.conn.execute(
                "SELECT o.price_eur,o.mileage_km,o.year,o.fuel,o.transmission,o.vin FROM listings l JOIN latest_observations o "
                "ON o.listing_id=l.id WHERE l.source=? AND l.external_id=?",
                (source, original.external_id),
            ).fetchone()
            changed = previous and any(
                value is not None and value != previous[field]
                for field, value in (
                    ("price_eur", original.price),
                    ("mileage_km", original.mileage),
                    ("year", original.year),
                    ("fuel", original.fuel),
                    ("transmission", original.transmission),
                    ("vin", original.vin),
                )
            )
            refresh = bool(changed or self._detail_revision_refresh(source, row, clock))
            if (clock - observed).total_seconds() < hours * 3600 and not refresh:
                if failed:
                    defer("detail_error_backoff", row)
                if row["raw_path"]:
                    try:
                        body = _raw_body(
                            self.db.path.parent, row["raw_path"], row["content_hash"]
                        )
                        page = self._parse(source, body, row["final_url"])
                        matches = [
                            item
                            for item in page.listings
                            if item.external_id == original.external_id
                        ]
                    except (ValueError, OSError):
                        defer("detail_cache_unverified", row)
                    if len(matches) != 1:
                        defer("detail_cached_unparsed", row)
                    accepted = self._accepted_detail_capture(
                        source, original.external_id, row
                    )
                    if source == "dealer:wiveda.de" and self.country == "DE":
                        from .bmw_detail_policy import detail_fact_conflicts
                        from .mercedes_wiveda_details import _facts

                        accepted = BMWDetailPolicy(self, run)._accepted(
                            source, original.external_id, row
                        )
                        if accepted:
                            publication = json.loads(accepted["details_json"])
                            if (
                                publication.get("source") != source
                                or publication.get("external_id")
                                != original.external_id
                            ):
                                defer("detail_cached_publication_binding", row)
                        if detail_fact_conflicts(
                            _facts(original, original.external_id), vars(matches[0])
                        ):
                            accepted = None
                    if accepted:
                        response = Response(
                            row["url"], row["final_url"], 200, body, row["fetched_at"]
                        )
                        response.secondary_fetch_id = row["id"]
                        response.secondary_cached_accepted = {
                            "external_id": original.external_id,
                            "fetch_id": row["id"],
                            "fetched_at": row["fetched_at"],
                            "content_hash": row["content_hash"],
                            "accepted_fetch_id": accepted["fetch_id"],
                            "accepted_observed_at": accepted["observed_at"],
                        }
                        return response
        used = self._secondary_detail_attempts + sum(
            stats.get("attempted", 0) for stats in self.detail_stats.values()
        )
        if used >= self.config.get("detail_limit", 4000):
            defer("detail_budget_deferred")
        self._secondary_detail_attempts += 1
        response = self._secondary_get(url, refresh=True)
        response.secondary_fetch_id = self.fetch_record(run, source, response)
        if (
            source == "dealer:wiveda.de"
            and self.country == "DE"
            and response.status == 200
            and not response.error
            and response.body
        ):
            from .mercedes_wiveda_details import (
                _access_denied,
                detail_matches,
                owned_id,
            )

            reason = None
            completed_at = timestamp(response.timestamp)
            if completed_at is None or completed_at > datetime.now(UTC):
                reason = "wiveda_capture_clock_unverified"
            if owned_id(url, payload=True):
                try:
                    matches = response.final_url == url and detail_matches(
                        self,
                        original.url,
                        response.final_url,
                        response.body,
                        original.external_id,
                    )
                except (ValueError, TypeError, AttributeError):
                    matches = False
                if not matches:
                    reason = "wiveda_detail_identity_mismatch"
            elif owned_id(response.final_url) != original.external_id or _access_denied(
                response.body, response.final_url
            ):
                reason = "wiveda_html_owner_or_access_mismatch"
            if reason:
                self.db.conn.execute(
                    "UPDATE fetches SET error=? WHERE id=?",
                    (reason, response.secondary_fetch_id),
                )
                self.db.conn.commit()
                raise ValueError(reason)
        return response

    def _quoka_discovery_context(self, query, url=None):
        from urllib.parse import parse_qsl, urlencode, urlunsplit

        if self.country != "DE" or query["source"] != "quoka_de":
            return None
        parsed = urlsplit(url or query["url"])
        # Quoka's own retained page links use pag; every inventory filter stays bound.
        params = sorted(
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k != "pag"
        )
        root = urlunsplit(parsed._replace(query=urlencode(params), fragment=""))
        context = [self.country, query["source"], root, query.get("request_body")]
        return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()

    def _quoka_legacy_discovery(self, query, previous):
        from .germany_retained import _raw_body

        run = previous.get("run_id")
        if not isinstance(run, int) or isinstance(run, bool):
            return []
        older = self.db.conn.execute(
            "SELECT run_id FROM source_runs WHERE source=? AND query_key=? "
            "AND run_id<? AND received_count>0 ORDER BY run_id DESC,id DESC LIMIT 1",
            (query["source"], query["key"], run),
        ).fetchone()
        context = self._quoka_discovery_context(query)
        root = urlsplit(query["url"])._replace(query="", fragment="").geturl() + "?%"
        recovered = {}
        # One-time v2 bootstrap reads at most two already retained query passes.
        # It never fetches a VDP or attributes these old cards to the current pass.
        for old_run in ([older[0]] if older else []) + [run]:
            pass_items = {}
            captures = self.db.conn.execute(
                "SELECT * FROM fetches WHERE source=? AND run_id=? AND status=200 "
                "AND error IS NULL AND raw_path IS NOT NULL AND url LIKE ? ORDER BY id",
                (query["source"], old_run, root),
            )
            matched = 0
            for capture in captures:
                if self._quoka_discovery_context(query, capture["url"]) != context:
                    continue
                if (
                    not capture["final_url"]
                    or self._quoka_discovery_context(query, capture["final_url"])
                    != context
                ):
                    continue
                if matched >= 200:
                    break
                matched += 1
                try:
                    body = _raw_body(
                        self.db.path.parent,
                        capture["raw_path"],
                        capture["content_hash"],
                    )
                    page = self._parse(query["source"], body, capture["final_url"])
                except (ValueError, OSError):
                    continue
                for item in page.listings:
                    pass_items.setdefault(
                        item.external_id,
                        {
                            "external_id": item.external_id,
                            "url": item.url,
                            "scope": item.scope,
                            "fetch_id": capture["id"],
                            "fetched_at": capture["fetched_at"],
                            "content_hash": capture["content_hash"],
                            "country": item.raw.get("market_country"),
                            "qualification_missing": item.raw.get(
                                "model_scope_evidence", {}
                            ).get("missing", []),
                            "qualification_excluded": item.scope == "excluded",
                            "qualification_generation": {
                                k: v
                                for k, v in item.raw.get("model_scope_evidence", {})
                                .get("generation", {})
                                .items()
                                if k in {"kind", "value", "series", "fetch_id"}
                            },
                            "discovery_run_id": old_run,
                        },
                    )
            recovered.update(pass_items)
        return list(recovered.values())

    def _quoka_retained_listings(self, query, current):
        context = self._quoka_discovery_context(query)
        if context is None:
            return []
        row = self.db.conn.execute(
            "SELECT value FROM monitor_state WHERE key=?",
            (
                "model_discovery_retained:"
                + query["source"]
                + ":"
                + query["key"]
                + ":"
                + context,
            ),
        ).fetchone()
        if row is None:
            row = self.db.conn.execute(
                "SELECT value FROM monitor_state WHERE key=?",
                ("model_discovery:" + query["source"] + ":" + query["key"],),
            ).fetchone()
        try:
            previous = json.loads(row[0]) if row else {}
        except (ValueError, TypeError):
            previous = {}
        if not isinstance(previous, dict):
            return []
        old_context = previous.get("query_context")
        if old_context not in (None, context):
            return []
        recovered = (
            self._quoka_legacy_discovery(query, previous) if old_context is None else []
        )
        candidates = {item["external_id"]: item for item in recovered}
        proven_ids = set(candidates)
        for field in ("retained_listings", "listings"):
            for item in (
                previous.get(field, []) if isinstance(previous.get(field), list) else []
            ):
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("external_id"), str)
                    and (old_context == context or item["external_id"] in proven_ids)
                ):
                    candidates[item["external_id"]] = {
                        "discovery_run_id": previous.get("run_id"),
                        **item,
                    }
        fields = {
            "external_id",
            "url",
            "scope",
            "fetch_id",
            "fetched_at",
            "content_hash",
            "country",
            "qualification_missing",
            "qualification_excluded",
            "qualification_generation",
            "accepted_detail_cache",
            "discovery_capture",
            "discovery_run_id",
        }
        present = {item["external_id"] for item in current}
        captures = {}
        fetch_ids = sorted(
            {
                item["fetch_id"]
                for item in candidates.values()
                if isinstance(item.get("fetch_id"), int)
                and not isinstance(item["fetch_id"], bool)
            }
        )
        for offset in range(0, len(fetch_ids), 256):
            batch = fetch_ids[offset : offset + 256]
            placeholders = ",".join("?" for _ in batch)
            for capture in self.db.conn.execute(
                "SELECT * FROM fetches WHERE id IN (" + placeholders + ")", batch
            ):
                captures[capture["id"]] = capture
        retained = []
        for eid in sorted(candidates):
            if eid in present:
                continue
            item = candidates[eid]
            if not isinstance(item.get("fetch_id"), int) or isinstance(
                item["fetch_id"], bool
            ):
                continue
            capture = captures.get(item["fetch_id"])
            if (
                not capture
                or capture["source"] != query["source"]
                or capture["status"] != 200
                or capture["error"]
            ):
                continue
            if (
                item.get("fetched_at") != capture["fetched_at"]
                or item.get("content_hash") != capture["content_hash"]
            ):
                continue
            record = {k: v for k, v in item.items() if k in fields}
            nested = {
                "qualification_generation": {"kind", "value", "series", "fetch_id"},
                "accepted_detail_cache": {
                    "external_id",
                    "fetch_id",
                    "fetched_at",
                    "content_hash",
                    "accepted_fetch_id",
                    "accepted_observed_at",
                },
                "discovery_capture": {"fetch_id", "fetched_at", "content_hash"},
            }
            for name, allowed in nested.items():
                if isinstance(record.get(name), dict):
                    record[name] = {
                        k: v for k, v in record[name].items() if k in allowed
                    }
            retained.append(record)
        return retained

    def _collect_secondary(self, run, query):
        from .mercedes_de_sources import collect_mercedes_de_source

        started = now()
        if getattr(self, "_secondary_policy_run", None) != run:
            self._secondary_policy_run = run
            self._secondary_detail_attempts = 0
        self._secondary_detail_deferrals = []
        with use_profile(MERCEDES_PROFILE, self.country):
            result = collect_mercedes_de_source(
                self._secondary_get,
                query,
                max_pages=query.get("max_pages", self.config.get("max_pages", 100)),
                max_requests=query.get("request_limit", 2000),
                max_details=query.get("detail_limit", 500),
                detail_eligible=self._secondary_detail_eligible,
                detail_getter=lambda url, original: self._secondary_detail_get(
                    run, query["source"], url, original=original
                ),
            )
        result["detail_deferrals"] = self._secondary_detail_deferrals
        origins = {}
        for response in result.pop("responses"):
            fetch_id = getattr(response, "secondary_fetch_id", None)
            if fetch_id is None:
                fetch_id = self.fetch_record(run, query["source"], response)
            if not response.error and response.body:
                key = (
                    response.url,
                    response.final_url,
                    response.timestamp,
                    hashlib.sha256(response.body.encode("utf-8")).hexdigest(),
                )
                origins[key] = (fetch_id, response.timestamp)
        search_items = result.pop("search_listings", [])
        # A cached VDP must not hide newly known facts in this pass's own search.
        # Keep each response's clock and proof separate; unchanged cache replays
        # still produce no new observations.
        for search in search_items:
            proof = search.raw.get("mercedes_capture_origin", {})
            origin = origins.get(
                tuple(
                    proof.get(k)
                    for k in (
                        "requested_uri",
                        "final_uri",
                        "fetched_at",
                        "content_hash",
                    )
                )
            )
            if not origin or search.raw.get("market_country") != self.country:
                continue
            previous = self.db.conn.execute(
                "SELECT o.price_eur,o.mileage_km,o.year,o.fuel,o.transmission,o.vin "
                "FROM listings l JOIN latest_observations o ON o.listing_id=l.id "
                "WHERE l.source=? AND l.external_id=?",
                (search.source, search.external_id),
            ).fetchone()
            if previous and any(
                value is not None and value != previous[field]
                for field, value in (
                    ("price_eur", search.price),
                    ("mileage_km", search.mileage),
                    ("year", search.year),
                    ("fuel", search.fuel),
                    ("transmission", search.transmission),
                    ("vin", search.vin),
                )
            ):
                self._observe(run, search, origin[1], origin[0], query["key"])
        items = result.pop("listings")
        targets = 0
        ledger = []
        for item in items:
            qualify_mercedes(item, self.country)
            proof = item.raw.get("mercedes_capture_origin", {})
            origin = origins.get(
                tuple(
                    proof.get(k)
                    for k in (
                        "requested_uri",
                        "final_uri",
                        "fetched_at",
                        "content_hash",
                    )
                )
            )
            ledger.append(
                {
                    "external_id": item.external_id,
                    "url": item.url,
                    "scope": item.scope,
                    "fetch_id": origin[0] if origin else None,
                    "fetched_at": origin[1] if origin else None,
                    "content_hash": proof.get("content_hash"),
                    "country": item.raw.get("market_country"),
                    "qualification_missing": item.raw.get(
                        "model_scope_evidence", {}
                    ).get("missing", []),
                    "qualification_excluded": item.scope == "excluded",
                    "qualification_generation": {
                        key: value
                        for key, value in item.raw.get("model_scope_evidence", {})
                        .get("generation", {})
                        .items()
                        if key in {"kind", "value", "series", "fetch_id"}
                    },
                    "accepted_detail_cache": item.raw.get("secondary_cached_accepted"),
                }
            )
            if item.raw.get("secondary_cached_accepted"):
                discovery = item.raw.get("discovery_record", {}).get(
                    "mercedes_capture_origin", {}
                )
                search_origin = origins.get(
                    tuple(
                        discovery.get(k)
                        for k in (
                            "requested_uri",
                            "final_uri",
                            "fetched_at",
                            "content_hash",
                        )
                    )
                )
                ledger[-1]["discovery_capture"] = {
                    "fetch_id": search_origin[0] if search_origin else None,
                    "fetched_at": discovery.get("fetched_at"),
                    "content_hash": discovery.get("content_hash"),
                }
            if not origin or item.raw.get("market_country") != self.country:
                continue
            if item.raw.get("secondary_cached_accepted"):
                targets += int(item.scope == "target")
                continue
            found = self._observe(run, item, origin[1], origin[0], query["key"])
            if (
                found is not None
                and item.source == "dealer:wiveda.de"
                and self.country == "DE"
            ):
                from .bmw_detail_policy import BMWDetailPolicy
                from .mercedes_wiveda_details import owned_id

                if (
                    owned_id(proof.get("requested_uri"), payload=True)
                    == item.external_id
                ):
                    BMWDetailPolicy(self, run).accept_observation(
                        item, origin[1], origin[0], found, query["key"]
                    )
            targets += int(found is not None and item.scope == "target")
        discovery = {**result, "schema_version": 2, "run_id": run, "listings": ledger}
        context = self._quoka_discovery_context(query)
        if context is not None:
            discovery.update(
                schema_version=3,
                query_context=context,
                retained_listings=self._quoka_retained_listings(query, ledger),
            )
            # A changed filter can reuse a configured key; keep each frontier bound.
            retained_context = {
                k: discovery[k]
                for k in (
                    "schema_version",
                    "query_context",
                    "run_id",
                    "listings",
                    "retained_listings",
                )
            }
            self.db.conn.execute(
                "INSERT OR REPLACE INTO monitor_state VALUES (?,?)",
                (
                    "model_discovery_retained:"
                    + query["source"]
                    + ":"
                    + query["key"]
                    + ":"
                    + context,
                    json.dumps(retained_context),
                ),
            )
        self.db.conn.execute(
            "INSERT OR REPLACE INTO monitor_state VALUES (?,?)",
            (
                "model_discovery:" + query["source"] + ":" + query["key"],
                json.dumps(discovery),
            ),
        )
        self.db.conn.commit()
        self.db.finish_source(
            run,
            query["source"],
            query["key"],
            started,
            result["status"],
            result.get("expected"),
            result.get("received", 0),
            targets,
            len(result.get("pages", [])),
            "; ".join(result["errors"]) or None,
            infer_missing=False,
        )
        print(
            json.dumps(
                {
                    "source": query["source"],
                    "query": query["key"],
                    "targets": targets,
                    **result,
                }
            ),
            flush=True,
        )

    def _secondary_get(self, url, refresh=False, request_body=None):
        # Overlapping queries can share the exact owned VDP capture within a run.
        # Its original timestamp survives; search counters are always fetched afresh.
        cached = (
            self.fetcher._cache.get(url)
            if request_body is None and not refresh
            else None
        )
        if cached and cached.body and not cached.error:
            source = self._source(cached.final_url)
            page = self._parse(source, cached.body, cached.final_url)
            if (
                len(page.listings) == 1
                and page.listings[0].url == url
                and page.total in {None, 1}
            ):
                return cached
        return self.fetcher.get(url, refresh=refresh, request_body=request_body)

    def _collect_official(self, run, query):
        from .mercedes_official import DE_HOST, fetch_mercedes_official

        started = now()
        ids = set()
        errors = []
        expected = None
        pages = targets = 0
        for number in range(1, query.get("max_pages", 200) + 1):
            if number > 1 and self.country == "DE":
                url = "https://" + DE_HOST + "/ajxvl"
                response = fetch_mercedes_official(
                    url,
                    {
                        "page": str(number),
                        "type": "vehiclelist",
                        "q": urlsplit(query["url"]).query,
                    },
                    page=number,
                    browser=True,
                )
            else:
                response = fetch_mercedes_official(
                    query["url"], page=number, browser=self.browser_enabled
                )
            try:
                page, count = self.ingest_response(
                    run, query["source"], query["key"], response
                )
                targets += count
                pages += 1
                current = {v.external_id for v in page.listings}
                if number == 1:
                    expected = page.total
                elif page.total != expected:
                    errors.append("counter_changed")
                if page.page is not None and page.page != number:
                    errors.append("native_page_mismatch")
                    break
                if current & ids:
                    errors.append("duplicate_native_ids")
                ids.update(current)
                if page.total == 0 or (page.pages and number >= page.pages):
                    break
                if not page.pages:
                    errors.append("pagination_uncertified")
                    break
            except Exception as error:  # noqa: BLE001 — preserve independent source failure
                errors.append(str(error))
                break
        if expected is None or len(ids) != expected:
            errors.append("native_counter_mismatch")
        status = (
            "partial" if errors else "zero_results" if expected == 0 else "complete"
        )
        self.db.finish_source(
            run,
            query["source"],
            query["key"],
            started,
            status,
            expected,
            len(ids),
            targets,
            pages,
            "; ".join(errors) or None,
            infer_missing=False,
        )
        print(
            json.dumps(
                {
                    "source": query["source"],
                    "query": query["key"],
                    "status": status,
                    "expected": expected,
                    "received": len(ids),
                    "errors": errors,
                }
            ),
            flush=True,
        )

    def collect_query(self, run, query):
        try:
            with use_profile(MERCEDES_PROFILE, self.country):
                if query["source"] == "autohero" and self.country == "NL":
                    return self._collect_autohero_nl(run, query)
                if query["source"] in {"mercedes_benz_nl", "mercedes_benz_de"}:
                    return self._collect_official(run, query)
                if self.country == "DE" and query["source"] not in {
                    "autoscout24_de",
                    "mercedes_benz_de",
                }:
                    return self._collect_secondary(run, query)
                return super().collect_query(run, query)
        except Exception as error:  # noqa: BLE001 — retain source failure and continue other publications
            self.failures.append("query_failed:" + query["source"] + ":" + query["key"])
            self.db.finish_source(
                run,
                query["source"],
                query["key"],
                now(),
                "error",
                None,
                0,
                0,
                0,
                str(error),
                infer_missing=False,
            )

    def rdw(self, run):
        if self.country == "DE":
            return
        records = self.db.conn.execute("""SELECT v.* FROM vehicles v WHERE v.kenteken IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM vehicle_checks c WHERE c.vehicle_id=v.id AND c.provider='rdw'
          AND julianday(c.checked_at)>julianday('now','-1 day'))""").fetchall()
        records = records[: self.config.get("rdw_limit", 200)]
        urls = [
            "https://opendata.rdw.nl/resource/m9d7-ebf2.json?kenteken=" + r["kenteken"]
            for r in records
        ]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for vehicle, response in zip(records, pool.map(self.fetcher.get, urls)):
                fetch_id = self.fetch_record(run, "rdw", response)
                if response.error or not response.body:
                    continue
                data = json.loads(response.body)
                if not data or data[0].get("kenteken") != vehicle["kenteken"]:
                    continue
                with self.db.transaction():
                    self.db.conn.execute(
                        "INSERT INTO vehicle_checks(vehicle_id,checked_at,provider,data_json,fetch_id) VALUES (?,?,?,?,?)",
                        (
                            vehicle["id"],
                            response.timestamp,
                            "rdw",
                            json.dumps(data[0]),
                            fetch_id,
                        ),
                    )
                    for row in self.db.conn.execute(
                        "SELECT l.id,o.details_json FROM listings l JOIN latest_observations o ON o.listing_id=l.id WHERE l.vehicle_id=?",
                        (vehicle["id"],),
                    ).fetchall():
                        item = Listing(**json.loads(row["details_json"]))
                        item.raw["rdw_model_evidence"] = {
                            "kenteken": vehicle["kenteken"],
                            "fetch_id": fetch_id,
                            "checked_at": response.timestamp,
                            "data": data[0],
                        }
                        qualify_mercedes(item, self.country)
                        self.db.conn.execute(
                            "UPDATE listings SET scope=? WHERE id=?",
                            (item.scope, row["id"]),
                        )
                        self.db.conn.execute(
                            "INSERT OR REPLACE INTO model_scope_verifications VALUES (?,?,?,?,?)",
                            (
                                row["id"],
                                item.scope,
                                response.timestamp,
                                fetch_id,
                                json.dumps(
                                    {
                                        "rdw": item.raw["rdw_model_evidence"],
                                        "model_scope": item.raw["model_scope_evidence"],
                                        "source": item.source,
                                        "external_id": item.external_id,
                                    }
                                ),
                            ),
                        )

    def run(self, *args, **kwargs):
        cache = getattr(self.fetcher, "_cache", None)
        if isinstance(cache, dict):
            cache.clear()
        with use_profile(MERCEDES_PROFILE, self.country):
            return super().run(*args, **kwargs)
