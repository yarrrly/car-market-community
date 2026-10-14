"""Durable evidence reuse and shared HTTP allowance for specialized BMW details."""

import json
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from .db import now
from .fetch import Fetcher, Response
from .germany_retained import _raw_body
from .provenance import timestamp


def _origin(url):
    try:
        parsed = urlsplit(url)
        default = {"https": 443, "http": 80}.get(parsed.scheme)
        if not parsed.hostname or default is None:
            return None
        port = parsed.port
        return parsed.scheme, parsed.hostname, default if port is None else port
    except (TypeError, ValueError):
        return None


class DetailDeferred(ValueError):
    def __init__(self, reason, capture=None):
        super().__init__(reason)
        self.reason = reason
        self.capture = (
            {
                "fetch_id": capture["id"],
                **{key: capture[key] for key in ("fetched_at", "content_hash")},
            }
            if capture
            else None
        )
        self.detail_policy_requests = 0


@dataclass
class DetailFetch:
    response: Response
    fetch_id: int
    requests: int = 0
    cached: bool = False


def detail_fact_conflicts(facts, item, *, require_both=False):
    """Compare bound parser fields, including explicit native raw fields."""
    conflicts = []
    for field, value in (facts or {}).items():
        previous = item
        for part in field.split("."):
            previous = previous.get(part) if isinstance(previous, dict) else None
        if (
            value is not None
            and value != previous
            and (previous is not None or not require_both)
        ):
            conflicts.append(field)
    return conflicts


class BMWDetailPolicy:
    def __init__(self, collector, run, *, limit=None):
        self.collector, self.run, self.used = collector, run, 0
        self.limit = limit

    def remaining(self):
        if self.limit is not None:
            return max(0, self.limit - self.used)
        config = self.collector.config
        limit = (
            config.get("fast_detail_limit", 25)
            if self.collector.fast
            else config.get("detail_batch_limit", 75)
        )
        return max(0, min(limit, config.get("detail_limit", limit)) - self.used)

    def accept_observation(self, item, stamp, fetch_id, listing_id, key):
        """Keep accepted proof for an actual parsed VDP even when market facts deduplicate."""
        if listing_id is None:
            return None
        conn = self.collector.db.conn
        row = conn.execute("SELECT * FROM fetches WHERE id=?", (fetch_id,)).fetchone()
        if (
            not row
            or row["source"] != item.source
            or row["status"] != 200
            or row["error"]
            or timestamp(stamp) is None
            or timestamp(stamp) != timestamp(row["fetched_at"])
            or item.raw.get("market_country") != "DE"
        ):
            raise DetailDeferred("accepted_detail_capture_mismatch")
        if not self._accepted(item.source, item.external_id, row):
            # A real fetch/parse has occurred. Preserve existing field provenance and
            # old rows while recording its exact accepted capture, not a new cache clock.
            return self.collector.db.observe(
                self.run, item, stamp, fetch_id, key, preserve_observation=True
            )
        return listing_id

    def _revision_refresh(self, source, row, clock):
        observed = timestamp(row["fetched_at"])
        cutoffs = (
            self.collector.parser_refresh_since,
            self.collector.source_parser_refresh_since.get(source),
        )
        return any(
            value is not None
            and (
                timestamp(value) is None
                or observed is None
                or observed < timestamp(value) <= clock
            )
            for value in cutoffs
        )

    def _accepted(self, source, identity, row):
        clock = timestamp(now())
        cutoffs = [
            timestamp(value)
            for value in (
                self.collector.parser_refresh_since,
                self.collector.source_parser_refresh_since.get(source),
            )
        ]
        applicable = [value for value in cutoffs if value and clock and value <= clock]
        since = (
            max(applicable).isoformat() if applicable else "0001-01-01T00:00:00+00:00"
        )
        return self.collector.db.conn.execute(
            "SELECT o.details_json,o.observed_at,l.id AS listing_id,f.id AS fetch_id FROM observations o "
            "JOIN listings l ON l.id=o.listing_id JOIN fetches f ON f.id=o.fetch_id "
            "WHERE l.source=? AND l.external_id=? AND f.source=? AND f.id=? AND f.status=200 "
            "AND f.error IS NULL AND f.content_hash=? AND (f.url=? OR f.final_url=?) "
            "AND julianday(o.observed_at)=julianday(f.fetched_at) "
            "AND julianday(o.observed_at)>=julianday(?) "
            "AND json_extract(o.details_json,'$.raw.market_country')='DE' "
            "ORDER BY julianday(o.observed_at) DESC,o.id DESC LIMIT 1",
            (
                source,
                identity,
                source,
                row["id"],
                row["content_hash"],
                row["url"],
                row["final_url"],
                since,
            ),
        ).fetchone()

    def _host_backoff(self, source, url, clock):
        collector = self.collector
        hours = collector.config.get("detail_error_backoff_hours", 6)
        origin = _origin(url)
        if origin is None:
            return
        if clock and hours > 0:
            for blocked in collector.db.conn.execute(
                # Logical source retains request provenance; a refusal belongs to
                # the actual final host even when another source followed a redirect.
                "SELECT * FROM fetches WHERE status IN (403,429) "
                "AND julianday(fetched_at)>julianday(?) AND julianday(fetched_at)<=julianday(?) "
                "ORDER BY julianday(fetched_at) DESC,id DESC",
                (
                    (clock - timedelta(hours=hours)).isoformat(),
                    clock.isoformat(),
                ),
            ):
                if _origin(blocked["final_url"] or blocked["url"]) == origin:
                    raise DetailDeferred(
                        "detail_host_backoff:" + str(blocked["status"]), blocked
                    )

    def fetch(
        self,
        source,
        url,
        *,
        identity=None,
        request=None,
        capture=None,
        query_remaining=None,
        cache=True,
        facts=None,
        replay=False,
    ):
        collector = self.collector
        if not collector.specialized_details_enabled:
            raise DetailDeferred("details_disabled")
        clock = timestamp(now())
        hours = collector.config.get("detail_error_backoff_hours", 6)
        self._host_backoff(source, url, clock)
        row = (
            collector.db.conn.execute(
                "SELECT * FROM fetches WHERE source=? AND (url=? OR final_url=?) "
                "ORDER BY julianday(fetched_at) DESC,id DESC LIMIT 1",
                (source, url, url),
            ).fetchone()
            if cache
            else None
        )
        observed = timestamp(row["fetched_at"]) if row else None
        if clock and observed and observed <= clock:
            age = (clock - observed).total_seconds()
            failed = row["status"] != 200 or bool(row["error"])
            refresh = row["status"] == 200 and self._revision_refresh(
                source, row, clock
            )
            accepted = (
                self._accepted(source, identity, row)
                if identity and not failed
                else None
            )
            if accepted and facts:
                previous = json.loads(accepted["details_json"])
                refresh = refresh or bool(detail_fact_conflicts(facts, previous))
            evidence_time = timestamp(accepted["observed_at"]) if accepted else observed
            evidence_age = (
                (clock - evidence_time).total_seconds() if evidence_time else None
            )
            if (
                failed
                and (row["error"] or row["status"] is None or row["status"] >= 400)
                and age < hours * 3600
                and not refresh
            ):
                raise DetailDeferred("detail_error_backoff", row)
            if (
                (accepted or replay)
                and not failed
                and not refresh
                and evidence_age is not None
                and 0
                <= evidence_age
                < collector.config.get("detail_cache_hours", 6) * 3600
            ):
                try:
                    body = _raw_body(
                        collector.db.path.parent, row["raw_path"], row["content_hash"]
                    )
                except (ValueError, OSError):
                    pass  # Missing/corrupt retained bytes require a bounded fresh request.
                else:
                    from .germany_additional_public import PublicResponse

                    item = json.loads(accepted["details_json"]) if accepted else None
                    publication = (
                        item.get("raw", {}).get("country_evidence") if item else None
                    )
                    if (
                        not isinstance(publication, dict)
                        or publication.get("kind") != "explicit_own_publication_country"
                    ):
                        publication = None
                    response = PublicResponse(
                        row["url"],
                        row["final_url"],
                        200,
                        body,
                        row["fetched_at"],
                        transport=row["transport"],
                        country_publication=publication,
                    )
                    response.detail_policy_cached = True
                    response.detail_policy_accepted = bool(accepted)
                    response.detail_policy_item = item
                    response.detail_policy_listing_id = (
                        accepted["listing_id"] if accepted else None
                    )
                    return DetailFetch(response, row["id"], cached=True)
        remaining = self.remaining()
        if query_remaining is not None:
            remaining = min(remaining, query_remaining)
        if remaining <= 0:
            raise DetailDeferred("detail_budget_deferred", row)
        getter = collector.fetcher
        before = getattr(getter, "http_requests", None)
        maximum = getattr(getter, "max_requests", None)
        if before is not None and maximum is not None:
            getter.max_requests = min(maximum, before + remaining)
        captures_before = len(getattr(getter, "captures", []))
        previous_guard = getattr(getter, "before_request", None)
        if before is not None:
            getter.before_request = lambda target: self._host_backoff(
                collector._source(target), target, timestamp(now())
            )
        attempts = 1
        try:
            if request:
                response = request()
            elif isinstance(getter, Fetcher):
                response = getter.get(url, refresh=True)
            else:
                response = getter.get(url)
        except BaseException as error:
            attempts = (
                max(0, getter.http_requests - before) if before is not None else 1
            )
            for response in getattr(getter, "captures", [])[captures_before:]:
                collector.fetch_record(
                    self.run, collector._source(response.final_url), response
                )
            if (
                isinstance(error, ValueError)
                and str(error) == "public_http_request_budget"
            ):
                deferred = DetailDeferred("detail_budget_deferred", row)
                deferred.detail_policy_requests = attempts
                raise deferred from error
            error.detail_policy_requests = attempts
            raise
        finally:
            if before is not None:
                attempts = max(0, getter.http_requests - before)
                getter.before_request = previous_guard
                if maximum is not None:
                    getter.max_requests = maximum
            self.used += attempts
        try:
            processed = {
                id(response),
                *(id(extra) for extra in getattr(response, "extra_responses", [])),
            }
            captures = getattr(getter, "captures", [])[captures_before:]
            main_index = next(
                (i for i, value in enumerate(captures) if id(value) == id(response)), 0
            )
            for extra in captures[:main_index]:
                if id(extra) not in processed:
                    collector.fetch_record(
                        self.run, collector._source(extra.final_url), extra
                    )
            response, fetch_id = (
                capture(response)
                if capture
                else (response, collector.fetch_record(self.run, source, response))
            )
            for extra in captures[main_index:]:
                if id(extra) not in processed:
                    collector.fetch_record(
                        self.run, collector._source(extra.final_url), extra
                    )
        except BaseException as error:
            error.detail_policy_requests = attempts
            raise
        return DetailFetch(response, fetch_id, requests=attempts)


def deferred_record(error, url):
    return {
        "status": "deferred",
        "url": url,
        "reason": error.reason,
        **(error.capture or {}),
    }
