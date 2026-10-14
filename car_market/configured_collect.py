"""Model-independent collector using tested history, fetch and identity contracts."""

from .collect import Collector, parse_market_page
from .configuration import bind
from .fetch import Fetcher
from .search_profile import qualify, search_context


class ConfiguredCollector(Collector):
    def __init__(self, db, config, browser=False, stopped=None):
        bind(db, config)
        from .configuration import ADAPTERS

        # A bounded parser refresh also retries previously unparsed 200 responses;
        # persisted 403/429 backoff remains intact. The user config is not rewritten.
        runtime_config = {
            **config,
            "detail_parser_revisions": {
                **config.get("detail_parser_revisions", {}),
                **{source: "configured-publication-state-v2" for source in ADAPTERS},
            },
        }
        super().__init__(db, runtime_config, browser)
        self.fetcher = Fetcher(
            browser=browser,
            locale="de-DE" if self.country == "DE" else "nl-NL",
            delay=config.get("request_delay", 1.5),
            timeout=config.get("request_timeout", 25),
        )
        self.stopped = stopped
        self.fetcher.stopped = stopped

    def fetch_record(self, run, source, response, retain_raw=True):
        # Generic selectors consume nested facts that the legacy search signature
        # cannot enumerate. Every confirmation must retain its own exact proof.
        return super().fetch_record(run, source, response, retain_raw=True)

    def _detail_matches(self, source, item_url, final_url, body, advertisement_id=None):
        if source in {"autoscout24", "autoscout24_de"}:
            from .mercedes_autoscout import autoscout_identity

            expected = autoscout_identity(item_url, self.country)
            if (
                not expected
                or expected != autoscout_identity(final_url, self.country)
                or expected != advertisement_id
            ):
                return False
            page = self._parse(source, body, final_url)
            return len(page.listings) == 1 and page.listings[0].external_id == expected
        return super()._detail_matches(
            source, item_url, final_url, body, advertisement_id
        )

    def _source_backoff_until(self, source):
        from datetime import UTC, datetime, timedelta

        from .provenance import timestamp

        evaluation = datetime.now(UTC)
        duration = timedelta(
            minutes=self.config.get("source_error_backoff_minutes", 60)
        )
        # Keep the existing source/time index bounded. SQLite rounds capture
        # fractions, so admit one extra second at each edge and decide in Python.
        rows = self.db.conn.execute(
            "SELECT fetched_at FROM fetches WHERE source=? AND status IN (403,429) "
            "AND julianday(fetched_at)>julianday(?) AND julianday(fetched_at)<=julianday(?) "
            "ORDER BY julianday(fetched_at) DESC,id DESC",
            (
                source,
                (evaluation - duration - timedelta(seconds=1)).isoformat(),
                (evaluation + timedelta(seconds=1)).isoformat(),
            ),
        )
        latest = None
        for row in rows:
            stamp = timestamp(row["fetched_at"])
            if (
                stamp is not None
                and stamp <= evaluation < stamp + duration
                and (latest is None or stamp > latest)
            ):
                latest = stamp
        return latest + duration if latest is not None else None

    def collect_query(self, run, query):
        from .db import now

        if self.stopped is not None and self.stopped.is_set():
            raise KeyboardInterrupt("Collector stopping")
        until = self._source_backoff_until(query["source"])
        if until is not None:
            self.db.finish_source(
                run,
                query["source"],
                query["key"],
                now(),
                "backoff",
                None,
                0,
                0,
                0,
                "access_backoff_until:" + until.isoformat(),
                infer_missing=False,
            )
            return
        super().collect_query(run, query)
        count = self.db.conn.execute(
            "SELECT COUNT(*) FROM query_membership q JOIN listings l ON l.id=q.listing_id WHERE q.source=? AND q.query_key=? AND q.last_run_id=? AND l.scope='target'",
            (query["source"], query["key"], run),
        ).fetchone()[0]
        self.db.conn.execute(
            "UPDATE source_runs SET target_count=? WHERE run_id=? AND source=? AND query_key=?",
            (count, run, query["source"], query["key"]),
        )
        self.db.conn.commit()

    def _parse(self, source, body, url):
        with search_context(self.config["scope"]):
            if source in {"autoscout24", "autoscout24_de"}:
                from .mercedes_autoscout import parse_mercedes_autoscout

                page = parse_mercedes_autoscout(body, url, self.country)
            else:
                page = parse_market_page(source, body, url)
            for item in page.listings:
                if self.country == "NL" and source not in {
                    "autoscout24",
                    "autoscout24_de",
                }:
                    from .mercedes_nl_sources import (
                        own_nl_evidence,
                        _retain_autotrack_dealer,
                    )

                    if (
                        source == "autotrack"
                        and len(page.listings) == 1
                        and "/a/" in url
                    ):
                        _retain_autotrack_dealer(item, body, url)
                    proof = own_nl_evidence(source, item.raw)
                    item.raw["native_fields_evidence"] = proof
                    for key, raw_key in [
                        ("brand", "model_brand"),
                        ("model_family", "model_family"),
                        ("model_variant", "model_variant"),
                        ("generation", "generation"),
                    ]:
                        if proof.get(key) is not None:
                            item.raw.setdefault(raw_key, proof[key])
                    if proof.get("country"):
                        item.raw.setdefault("country_code", proof["country"])
                    address = proof.get("dealer_address")
                    if address and address.get("bound_to_own_listing"):
                        item.raw["dealerAddress"] = {
                            "postalCode": address.get("postal_code"),
                            "country": address.get("country"),
                            "streetAddress": address.get("street_address"),
                            "city": address.get("city"),
                        }
                qualify(item, self.config["scope"])
            return page

    def _observe(self, run, item, stamp, fetch_id=None, key=None):
        with search_context(self.config["scope"]):
            qualify(item, self.config["scope"])
            if item.raw.get("market_country") != self.country:
                if item.raw.get("market_country"):
                    existing = self.db.conn.execute(
                        "SELECT id FROM listings WHERE source=? AND external_id=?",
                        (item.source, item.external_id),
                    ).fetchone()
                    if existing:
                        return self.db.observe(run, item, stamp, fetch_id, key)
                    return None
                self.failures.append(
                    "country_unconfirmed:" + item.source + ":" + item.external_id
                )
                # Retain a pending discovery record without inventing a physical vehicle identity.
                import json
                import dataclasses

                pending_key = "pending:" + item.source + ":" + item.external_id
                previous = self.db.conn.execute(
                    "SELECT value FROM monitor_state WHERE key=?", (pending_key,)
                ).fetchone()
                prior = json.loads(previous[0]) if previous else {}
                query_fetches = dict(prior.get("query_fetch_ids") or {})
                if prior.get("query_key") and prior.get("fetch_id"):
                    query_fetches.setdefault(prior["query_key"], prior["fetch_id"])
                if key and fetch_id is not None:
                    query_fetches[key] = fetch_id

                self.db.conn.execute(
                    "INSERT OR REPLACE INTO monitor_state VALUES (?,?)",
                    (
                        pending_key,
                        json.dumps(
                            {
                                **prior,
                                "url": item.url,
                                "observed_at": stamp,
                                "fetch_id": fetch_id,
                                "reasons": item.raw["model_scope_evidence"]["reasons"],
                                "candidate": dataclasses.asdict(item),
                                "query_key": key or prior.get("query_key"),
                                "query_fetch_ids": query_fetches,
                            }
                        ),
                    ),
                )
                self.db._commit()
                return None
            result = super()._observe(run, item, stamp, fetch_id, key)
            self.db.conn.execute(
                "DELETE FROM monitor_state WHERE key=?",
                ("pending:" + item.source + ":" + item.external_id,),
            )
            self.db._commit()
            return result

    def _restore_pending_memberships(self, pending, item, listing_id):
        """Credit only the original search proof, never the enrichment run."""
        from .provenance import timestamp

        if listing_id is None:
            return
        proofs = dict(pending.get("query_fetch_ids") or {})
        if pending.get("query_key") and pending.get("fetch_id"):
            proofs.setdefault(pending["query_key"], pending["fetch_id"])
        for key, fetch_id in proofs.items():
            if not isinstance(key, str) or type(fetch_id) is not int:
                continue
            searched = self.db.conn.execute(
                "SELECT f.run_id,f.fetched_at,s.started_at FROM fetches f "
                "JOIN source_runs s ON s.run_id=f.run_id AND s.source=f.source "
                "JOIN runs r ON r.id=f.run_id "
                "WHERE f.id=? AND f.source=? AND s.query_key=? "
                "AND f.status=200 AND f.error IS NULL AND f.content_hash IS NOT NULL "
                "AND r.kind <> 'discovery_enrichment'",
                (fetch_id, item.source, key),
            ).fetchone()
            if not searched:
                continue
            search_time = timestamp(searched["started_at"]) or timestamp(searched["fetched_at"])
            if search_time is None:
                continue
            previous = self.db.conn.execute(
                "SELECT q.last_run_id,COALESCE(s.started_at,r.started_at) AS started_at "
                "FROM query_membership q JOIN runs r ON r.id=q.last_run_id "
                "LEFT JOIN source_runs s ON s.run_id=q.last_run_id AND s.source=q.source "
                "AND s.query_key=q.query_key WHERE q.source=? AND q.query_key=? AND q.listing_id=?",
                (item.source, key, listing_id),
            ).fetchone()
            if previous:
                # Replaying old discovery proof cannot clear later missing-query
                # history or displace a newer real search presence.
                previous_time = timestamp(previous["started_at"])
                if (
                    previous["last_run_id"] == searched["run_id"]
                    or previous_time is None
                    or search_time <= previous_time
                ):
                    continue
            self.db.conn.execute(
                "INSERT INTO query_membership(source,query_key,listing_id,last_run_id) VALUES (?,?,?,?) "
                "ON CONFLICT(source,query_key,listing_id) DO UPDATE SET "
                "last_run_id=excluded.last_run_id,missed_complete_runs=0",
                (item.source, key, listing_id, searched["run_id"]),
            )
            self.db.conn.execute(
                "UPDATE source_runs SET target_count=(SELECT COUNT(*) FROM query_membership q "
                "JOIN listings l ON l.id=q.listing_id WHERE q.source=? AND q.query_key=? "
                "AND q.last_run_id=? AND l.scope='target') "
                "WHERE run_id=? AND source=? AND query_key=?",
                (item.source, key, searched["run_id"], searched["run_id"], item.source, key),
            )

    def ingest_response(self, run, source, key, response, queue=True):
        # ContextVars are established inside every parser call, including worker threads.
        with search_context(self.config["scope"]):
            if (
                source in {"autoscout24", "autoscout24_de"}
                and not response.error
                and response.body
                and response.url != response.final_url
            ):
                # The final route must not replace the inventory the user asked
                # for. Check original filters before any rows gain membership.
                try:
                    requested = self._parse(source, response.body, response.url)
                except Exception:
                    # Preserve the same raw-evidence contract as the core
                    # ingest boundary when a malformed redirected body fails.
                    self.fetch_record(run, source, response)
                    raise
                if not requested.listings and requested.total is None:
                    response.error = "search_query_redirect_mismatch"
            return super().ingest_response(run, source, key, response, queue=queue)

    def rdw(self, run):
        # The legacy RDW promotion recognises BMW only. Avoid changing generic model scope.
        return None

    def run(self, *args, **kwargs):
        with search_context(self.config["scope"]):
            result = super().run(*args, **kwargs)
            details_enabled = args[1] if len(args) > 1 else kwargs.get("details", True)
            if details_enabled:
                result["discovery_details"] = self._pending_details()
                result.update(self.db.summary())
                discovery = result["discovery_details"]
                if discovery["errors"] or discovery["unconfirmed"] or discovery["deferred"]:
                    result["status"] = "partial"
                    self.db.finish_run(result["run_id"], "partial", result)
            return result

    def _pending_details(self):
        import json
        from .db import now
        from .parse import Listing

        run = self.db.start_run("discovery_enrichment")
        limit = (
            self.config.get("fast_detail_limit", 20)
            if self.fast
            else self.config.get("detail_batch_limit", 50)
        )
        report = {"attempted": 0, "resolved": 0, "unconfirmed": 0, "errors": 0, "deferred": 0}
        used = sum(stats.get("attempted", 0) for stats in self.detail_stats.values())
        limit = max(0, min(limit, self.config.get("detail_limit", 50) - used))
        try:
            from .configuration import ADAPTERS

            blocked = {
                source for source in ADAPTERS if self._source_backoff_until(source)
            }
            # Sort keys only and load each selected candidate separately. Fetching
            # every JSON blob before slicing makes a tiny budget use market-sized RAM.
            records = self.db.conn.execute(
                "SELECT key FROM monitor_state WHERE key >= 'pending:' AND key < 'pending;' "
                "AND json_type(value,'$.candidate')='object' "
                "AND COALESCE(json_extract(value,'$.candidate.scope'),'') <> 'excluded' "
                # Gaspedaal has no accepted own VDP contract. Its fragments and
                # search routes remain discoveries, rather than repeated detail GETs.
                "AND COALESCE(json_extract(value,'$.candidate.source'),'') <> 'gaspedaal' "
                "AND instr(COALESCE(json_extract(value,'$.candidate.url'),''),'#')=0 "
                "AND (json_extract(value,'$.last_attempt_at') IS NULL OR "
                "julianday(json_extract(value,'$.last_attempt_at')) <= julianday(?) - ? / 24.0) "
                + (
                    "AND json_extract(value,'$.candidate.source') NOT IN ("
                    + ",".join("?" for _ in blocked) + ") "
                    if blocked else ""
                )
                + "ORDER BY json_extract(value,'$.last_attempt_at') IS NOT NULL, "
                "julianday(json_extract(value,'$.last_attempt_at')),key LIMIT ?",
                (now(), self.config.get("detail_error_backoff_hours", 6), *sorted(blocked), limit),
            ).fetchall()
            for row in records:
                if self.stopped is not None and self.stopped.is_set():
                    raise KeyboardInterrupt("Collector stopping")
                pending = json.loads(self.db.conn.execute(
                    "SELECT value FROM monitor_state WHERE key=?", (row["key"],)
                ).fetchone()[0])
                original = Listing(**pending["candidate"])
                if original.source in blocked:
                    continue
                pending.pop("attempt_error", None)
                pending.pop("last_attempt_fetch_id", None)
                pending["last_attempt_at"] = now()
                # Commit the attempt clock before I/O so restart cannot immediately
                # repeat an interrupted request. Keep the candidate until resolved.
                self.db.conn.execute(
                    "UPDATE monitor_state SET value=? WHERE key=?",
                    (json.dumps(pending), row["key"]),
                )
                self.db.conn.commit()
                report["attempted"] += 1
                try:
                    response = self.fetcher.get(original.url, refresh=True)
                    fetch_id = self.fetch_record(run, original.source, response)
                    pending["last_attempt_fetch_id"] = fetch_id
                    if response.status in (403, 429):
                        blocked.add(original.source)
                    if response.error or not response.body:
                        raise ValueError(response.error or "empty_response")
                    page = self._parse(
                        original.source, response.body, response.final_url
                    )
                    own = [
                        i
                        for i in page.listings
                        if i.external_id == original.external_id
                    ]
                    if len(own) != 1 or not self._detail_matches(
                        original.source,
                        original.url,
                        response.final_url,
                        response.body,
                        own[0].external_id,
                    ):
                        raise ValueError("pending_detail_identity_unconfirmed")
                    candidate = own[0]
                    qualify(candidate, self.config["scope"])
                    if candidate.raw.get("market_country") == self.country:
                        with self.db.transaction():
                            listing_id = self._observe(
                                run, candidate, response.timestamp, fetch_id
                            )
                            self._restore_pending_memberships(pending, candidate, listing_id)
                        report["resolved"] += 1
                        continue
                    pending["attempt_status"] = "country_unconfirmed"
                    report["unconfirmed"] += 1
                except Exception as error:
                    report["errors"] += 1
                    pending["attempt_status"] = "error"
                    pending["attempt_error"] = str(error)
                self.db.conn.execute(
                    "UPDATE monitor_state SET value=? WHERE key=?",
                    (json.dumps(pending), row["key"]),
                )
                self.db.conn.commit()
            report["deferred"] = self.db.conn.execute(
                "SELECT COUNT(*) FROM monitor_state WHERE key >= 'pending:' AND key < 'pending;'"
            ).fetchone()[0]
            self.db.finish_run(
                run,
                "partial" if report["errors"] or report["unconfirmed"] or report["deferred"] else "complete",
                report,
            )
            return report
        except BaseException as error:
            self.db.finish_run(run, "interrupted", {"error": str(error)})
            raise
