"""Coverage of configured searches: freshness, control misses and uncertainty."""

from datetime import UTC, datetime

from .db import now
from .provenance import timestamp


def query_share(expected, received, error):
    """Describe a source query's enumeration; this is never a whole-market share."""
    if (
        type(expected) is not int
        or expected <= 0
        or type(received) is not int
        or received < 0
        or received > expected
        or "counter_changed" in (error or "")
        or "search_filters_changed" in (error or "")
    ):
        return None
    return round(100 * received / expected, 2)


def report(conn, config):
    queries = []
    inventories = {}
    for query in config["queries"]:
        if not query.get("enabled", True):
            continue
        source, key = query["source"], query["key"]
        row = conn.execute(
            "SELECT * FROM source_health WHERE source=? AND query_key=?",
            (source, key),
        ).fetchone()
        fast_sources = config.get("fast_sources")
        fast = query.get("cadence") != "broad" and (
            not fast_sources or source in fast_sources
        )
        item = {
            "source": source,
            "query_key": key,
            "url": query["url"],
            "role": query.get("role", "primary"),
            "cadence": query.get("cadence", "fast" if fast else "broad"),
        }
        records = []
        if row:
            item.update(
                {
                    k: row[k]
                    for k in (
                        "run_id",
                        "status",
                        "started_at",
                        "finished_at",
                        "expected_count",
                        "received_count",
                        "pages",
                        "error",
                    )
                }
            )
            finished = timestamp(row["finished_at"])
            age = (datetime.now(UTC) - finished).total_seconds() if finished else None
            # Match actual source eligibility, with 30 minutes allowed for a pass.
            period = (
                config.get("fast_interval_minutes", 5) * 60
                if fast
                else config.get("interval_hours", 6) * 3600
            )
            age_limit = period + 1800
            item["freshness_limit_seconds"] = age_limit
            item["age_seconds"] = round(age) if age is not None else None
            item["stale"] = age is None or age < 0 or age > age_limit
            records = conn.execute(
                "SELECT l.id,l.vehicle_id,v.kenteken,l.scope,l.url FROM query_membership m "
                "JOIN listings l ON l.id=m.listing_id JOIN vehicles v ON v.id=l.vehicle_id "
                "WHERE m.source=? AND m.query_key=? AND m.last_run_id=? "
                "AND l.scope IN ('target','needs_confirmation')",
                (source, key, row["run_id"]),
            ).fetchall()
        else:
            item.update(status="never_checked", stale=True)
        item["query_enumeration_percent"] = query_share(
            item.get("expected_count"), item.get("received_count"), item.get("error")
        )
        item["coverage_basis"] = (
            "Unique parsed publication IDs divided by the source-reported result "
            "count for this exact query; excludes neither out-of-profile nor "
            "unconfirmed-country cards. Not unique vehicles or whole-market coverage."
        )
        item["empty_query_verified"] = (
            item["status"] == "zero_results"
            and item.get("expected_count") == 0
            and item.get("received_count") == 0
            and not item["stale"]
        )
        item["target_ads"] = sum(r["scope"] == "target" for r in records)
        item["unconfirmed_ads"] = sum(
            r["scope"] == "needs_confirmation" for r in records
        )
        inventories[(source, key)] = records
        queries.append(item)
    for item in queries:
        if item["role"] == "control":
            primary_queries = [
                q
                for q in queries
                if q["source"] == item["source"] and q["role"] == "primary"
            ]
            item["comparison_complete_and_fresh"] = (
                item["status"] in ("complete", "zero_results")
                and not item["stale"]
                and bool(primary_queries)
                and all(
                    q["status"] in ("complete", "zero_results") and not q["stale"]
                    for q in primary_queries
                )
            )
            primary_ids = {
                r["id"]
                for q in queries
                if q["source"] == item["source"] and q["role"] == "primary"
                for r in inventories[(q["source"], q["query_key"])]
            }
            item["missed_by_primary"] = [
                {
                    "listing_id": r["id"],
                    "kenteken": r["kenteken"],
                    "scope": r["scope"],
                    "url": r["url"],
                }
                for r in inventories[(item["source"], item["query_key"])]
                if r["id"] not in primary_ids
            ]
            primary_plates = {
                r["kenteken"]
                for q in primary_queries
                for r in inventories[(q["source"], q["query_key"])]
                if r["kenteken"]
            }
            item["additional_registrations_vs_primary"] = sorted(
                {
                    r["kenteken"]
                    for r in inventories[(item["source"], item["query_key"])]
                    if r["kenteken"] and r["kenteken"] not in primary_plates
                }
            )
            item["additional_ads_without_registration"] = sum(
                not r["kenteken"] for r in item["missed_by_primary"]
            )
    rows = {r["id"]: r for inventory in inventories.values() for r in inventory}
    plates = {r["kenteken"] for r in rows.values() if r["kenteken"]}
    channels = []
    for channel in config.get("channels", []):
        channel = dict(channel)
        if channel["source"] == "facebook":
            ads = conn.execute(
                "SELECT l.id,l.vehicle_id,l.scope,l.last_seen,v.kenteken,v.vin FROM listings l "
                "JOIN vehicles v ON v.id=l.vehicle_id WHERE l.source='facebook'"
            ).fetchall()
            linked = {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT vehicle_id FROM listings WHERE source<>'facebook'"
                )
            }
            channel.update(
                status="partial" if ads else "access_required",
                observed_ads=len(ads),
                target_ads=sum(r["scope"] == "target" for r in ads),
                unconfirmed_ads=sum(r["scope"] == "needs_confirmation" for r in ads),
                confirmed_vehicle_ids=len(
                    {r["vehicle_id"] for r in ads if r["kenteken"] or r["vin"]}
                ),
                matched_to_other_sources=len(
                    {r["vehicle_id"] for r in ads if r["vehicle_id"] in linked}
                ),
                ads_without_strong_identity=sum(
                    not (r["kenteken"] or r["vin"]) for r in ads
                ),
                last_observed_at=max((r["last_seen"] for r in ads), default=None),
                inventory_complete=False,
            )
        channels.append(channel)
    return {
        "generated_at": now(),
        "queries": queries,
        "channels": channels,
        "all_configured_queries_complete_and_fresh": bool(queries)
        and all(
            q["status"] in ("complete", "zero_results") and not q["stale"]
            for q in queries
        ),
        "market_100_percent_verified": False,
        "whole_market_coverage_percent": None,
        "limitation": "Unknown total market; private groups and unlisted dealer inventory are not enumerable. Counts are observed ads, not publication dates or confirmed unique cars.",
        "observed_target_and_unconfirmed_ads": len(rows),
        "confirmed_registrations": len(plates),
        "ads_without_registration": sum(not r["kenteken"] for r in rows.values()),
    }
