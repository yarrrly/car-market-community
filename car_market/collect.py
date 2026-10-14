import hashlib
import json
import pathlib
import re
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from threading import Lock
from urllib.parse import parse_qs, urljoin, urlsplit

from .db import now
from .fetch import Fetcher
from .parse import Page, parse_page, with_query
from .text_encoding import json_text


@contextmanager
def bounded_responses(get, urls, workers):
    """Keep at most one request per worker in flight, including during shutdown."""
    pool = ThreadPoolExecutor(max_workers=workers)
    pending = deque()
    urls = iter(urls)

    def submit_next():
        try:
            url = next(urls)
        except StopIteration:
            return
        pending.append(pool.submit(get, url))

    def responses():
        for _ in range(workers):
            submit_next()
        while pending:
            yield pending.popleft().result()
            submit_next()

    try:
        yield responses()
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)


def parse_market_page(source, body, url):
    if source.startswith("dealer:") and urlsplit(url).hostname in {
        "flevomobiel.nl",
        "www.flevomobiel.nl",
        "www.hedinautomotive.nl",
        "linkerbaan.com",
        "www.linkerbaan.com",
        "www.schalkautomotive.nl",
        "www.drive-works.nl",
    }:
        from .quality_specific_dealers import parse_specific_dealer_detail

        page = parse_specific_dealer_detail(body, url) or Page([])
        for item in page.listings:
            item.source = source
        return page
    if source in {"autofirst", "vakgarage", "carteam", "autokopen", "autouncle"}:
        from .quality_dealer_adapters import parse_quality_page

        return parse_quality_page(source, body, url)
    return parse_page(source, body, url)


def load_config(path):
    config = json.loads(pathlib.Path(path).read_text())
    if config.get("scope", {}).get("country", "NL") not in {"NL", "DE"}:
        raise ValueError("Only verified NL and DE market configurations are supported")
    from .model_profiles import config_profile

    config_profile(config)
    return config


def page_url(query, page, first):
    url = query["url"]
    if page == 1:
        return url
    if query["pagination"] == "mercedes_autoscout_next":
        from .mercedes_autoscout import mercedes_autoscout_page_url

        return mercedes_autoscout_page_url(query, page, first)
    if query["pagination"] == "autoscout_de_next":
        from .germany_enumeration import autoscout_next_url

        return autoscout_next_url(query["url"], page, first)
    if query["pagination"] == "mobile_de":
        from .germany_mobile import mobile_page_url

        return mobile_page_url(query["url"], page)
    if query["pagination"] == "secondary_de":
        from .germany_secondary import secondary_page_request

        request = secondary_page_request(query["source"], query["url"], page)
        if query["source"] == "autohero_de":
            return with_query(request["url"], offset=(page - 1) * 24)
        return request["url"]
    if query["pagination"] == "marktplaats":
        parsed = urlsplit(url)
        path = re.sub(r"/p/\d+/?$", "", parsed.path).rstrip("/")
        return parsed._replace(path=path + "/p/" + str(page) + "/").geturl()
    if query["pagination"] == "marktplaats_api":
        size = int(parse_qs(urlsplit(url).query).get("limit", [30])[0])
        return with_query(url, offset=(page - 1) * size)
    if query["pagination"] == "autotrack":
        if first.search_filters.get("native_adapter") == "autotrack_own_next_search_v1":
            from .autotrack_search import native_page_url

            return native_page_url(page, first)
        links = [x for x in first.links if "pageNumber=" in x]
        if not links:
            return None
        return with_query(links[0], pageNumber=page)
    if query["pagination"] in ("anwb", "autohero"):
        return with_query(url, pageNumber=page)
    if query["pagination"] == "bmw":
        import base64

        value = parse_qs(urlsplit(url).query).get("filters", [""])[0]
        decoded = base64.b64decode(value).decode()
        decoded = re.sub(
            r'(s:4:"page";i:)\d+', lambda m: m.group(1) + str(page), decoded
        )
        return with_query(url, filters=base64.b64encode(decoded.encode()).decode())
    if query["pagination"] == "page":
        return with_query(url, page=page)
    if query["pagination"] == "viabovag":
        return url.rstrip("/") + "/pagina-" + str(page)
    if query["pagination"] == "p":
        return with_query(url, p=page)
    if query["pagination"] == "mobiel":
        links = (
            [x for x in first.links if "index.php" in x and "p-page=" in x]
            if first
            else []
        )
        return with_query(links[0] if links else url, **{"p-page": page})
    if query["pagination"] == "autoweek":
        return with_query(url, pagina=page)
    if query["pagination"] == "trovit":
        return url.rstrip("/") + "/" + str(page)
    next_links = [
        x
        for x in first.links
        if re.search(r"(?:page=|pageNumber=|p-page=)" + str(page) + r"(?:&|$)", x)
    ]
    return next_links[0] if next_links else None


def identify_source(url):
    host = urlsplit(url).netloc.lower()
    if urlsplit(url).hostname == "autos.trovit.nl":
        return "trovit"
    for source in [
        "autoscout24",
        "marktplaats",
        "autotrack",
        "autotrader",
        "autouncle",
        "viabovag",
        "autowereld",
        "autoweek",
        "autokopen",
        "nederlandmobiel",
        "marktnet",
        "autohero",
        "anwb",
        "vakgarage",
        "autofirst",
        "carteam",
    ]:
        if source in host:
            return source
    if host.endswith("bmw.nl"):
        return "bmw"
    return "dealer:" + host


def canonical_id(source, url):
    if source.endswith("_de"):
        from .germany import canonical_de_id

        return canonical_de_id(source, url)
    path = urlsplit(url).path
    if source == "nederlandmobiel":
        match = re.search(r"/tweedehands-auto/.+/(\d+)/?$", path)
        return match.group(1) if match else url.split("#")[0]
    if source == "trovit":
        match = re.search(r"/listing/[^/]+\.([A-Za-z0-9_]+)/?$", path)
        if not match:
            match = re.search(r"(?:^|/)id\.([A-Za-z0-9_]+)(?:/|$)", path)
        return match.group(1) if match else url.split("#")[0]
    if source == "autofirst":
        match = re.search(r"/occasions/(\d+)-", path)
        return match.group(1) if match else url.split("#")[0]
    if source == "vakgarage":
        match = re.search(r"/p/(\d+)/", path)
        return match.group(1) if match else url.split("#")[0]
    if source == "marktnet":
        identity = parse_qs(urlsplit(url).query).get("id", [""])[0]
        if identity.isdigit():
            return identity
        match = re.search(r"(?:/|-)(\d+)(?:\.htm)?$", path)
        return match.group(1) if match else url.split("#")[0]
    if source in ["autoscout24", "autotrader"]:
        m = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27,})$", path)
    elif source == "marktplaats":
        m = re.search(r"/(m\d+)", path)
    elif source in ("autotrack", "autokopen"):
        m = re.search(r"-(\d+)$", path)
    else:
        return url.split("#")[0]
    return m.group(1) if m else url.split("#")[0]


def fair_detail_jobs(jobs, last_source=None):
    groups = defaultdict(deque)
    for job in jobs:
        groups[job[1][2].source].append(job)
    result = []
    sources = sorted(groups)
    if last_source:
        after = [source for source in sources if source > last_source]
        sources = after + [source for source in sources if source <= last_source]
    while groups:
        for source in list(sources):
            result.append(groups[source].popleft())
            if not groups[source]:
                del groups[source]
                sources.remove(source)
    return result


def detail_identity_matches(source, item_url, final_url, body, advertisement_id=None):
    expected = canonical_id(source, final_url)
    if source == "nederlandmobiel":
        from .dom import mobiel_detail_identity

        if (
            mobiel_detail_identity(body, final_url) != expected
            or str(advertisement_id) != expected
        ):
            return False
    if (
        source in {"autoscout24", "autotrader"}
        and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", expected)
        and (
            not advertisement_id
            or canonical_id(source, str(advertisement_id)) != expected
        )
    ):
        return False
    if canonical_id(source, item_url) == expected:
        return True
    routes = {
        "dealer:www.van-poelgeest.nl": r"^/voorraad/.+-(\d{6,})/?$",
        "dealer:www.wijnne.nl": r"^/occasions/.+/(\d{6,})/?$",
    }
    if source not in routes:
        return False
    from bs4 import BeautifulSoup

    links = BeautifulSoup(body, "html.parser").select('link[rel~="canonical"][href]')
    if len(links) != 1:
        return False
    declared = urljoin(final_url, links[0]["href"])
    urls = [urlsplit(url) for url in (item_url, final_url, declared)]
    if len({url.hostname for url in urls}) != 1 or any(url.query for url in urls):
        return False
    matches = [re.fullmatch(routes[source], url.path) for url in urls]
    return bool(
        all(matches)
        and len({match.group(1) for match in matches}) == 1
        and item_url.rstrip("/") == declared.rstrip("/")
    )


class Collector:
    def __init__(self, db, config, browser=False):
        self.db, self.config = db, config
        self.country = config.get("scope", {}).get("country", "NL")
        self.browser_enabled = browser
        self.fetcher = (
            Fetcher(
                browser=browser, locale="de-DE", delay=config.get("request_delay", 0.7)
            )
            if self.country == "DE"
            else Fetcher(browser=browser)
        )
        self.workers = config.get("workers", 4)
        self.detail_jobs = {}
        self.failures = []
        self.detail_stats = {}
        self.fast = False
        self.specialized_details_enabled = True
        self.db.conn.execute(
            "CREATE TABLE IF NOT EXISTS monitor_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        self.db.conn.commit()
        if self.country == "DE":
            from .germany import bind_market

            bind_market(self.db, config)
        self.parser_refresh_since = None
        revision = config.get("detail_parser_revision")
        if revision:
            row = self.db.conn.execute(
                "SELECT value FROM monitor_state WHERE key='detail_parser_revision'"
            ).fetchone()
            state = json.loads(row[0]) if row else {}
            if state.get("revision") != revision:
                state = {"revision": revision, "since": now()}
                self.db.conn.execute(
                    "INSERT OR REPLACE INTO monitor_state VALUES ('detail_parser_revision',?)",
                    (json.dumps(state),),
                )
                self.db.conn.commit()
            self.parser_refresh_since = state["since"]
        self.source_parser_refresh_since = {}
        for source, revision in config.get("detail_parser_revisions", {}).items():
            key = "detail_parser_revision:" + source
            row = self.db.conn.execute(
                "SELECT value FROM monitor_state WHERE key=?", (key,)
            ).fetchone()
            state = json.loads(row[0]) if row else {}
            if state.get("revision") != revision:
                state = {"revision": revision, "since": now()}
                self.db.conn.execute(
                    "INSERT OR REPLACE INTO monitor_state VALUES (?,?)",
                    (key, json.dumps(state)),
                )
                self.db.conn.commit()
            self.source_parser_refresh_since[source] = state["since"]

    def _parse(self, source, body, url):
        if self.country == "DE":
            from .germany import parse_germany_page

            return parse_germany_page(source, body, url)
        page = parse_market_page(source, body, url)
        if (
            self.config.get("scope", {}).get("model_profile", "bmw_218i_active_tourer")
            == "bmw_218i_active_tourer"
        ):
            from .nl_publication import qualify_publication

            for item in page.listings:
                qualify_publication(item)
        return page

    def _source(self, url):
        if self.country == "DE":
            from .germany import source_for_url

            return source_for_url(url)
        return identify_source(url)

    def _observe(self, run, item, stamp, fetch_id=None, key=None):
        if self.country == "DE":
            from .germany import observe_german_item

            return observe_german_item(self.db, run, item, stamp, fetch_id, key)
        return self.db.observe(run, item, stamp, fetch_id, key)

    def _detail_matches(self, source, item_url, final_url, body, advertisement_id=None):
        if self.country == "DE":
            from .germany import german_detail_identity_matches

            return german_detail_identity_matches(
                source, item_url, final_url, body, advertisement_id
            )
        return detail_identity_matches(
            source, item_url, final_url, body, advertisement_id
        )

    def _source_backoff_until(self, source):
        return None

    def specialized_detail_policy(self, run):
        from .bmw_detail_policy import BMWDetailPolicy

        policy = getattr(self, "_bmw_detail_policy", None)
        if policy is None or policy.run != run:
            policy = self._bmw_detail_policy = BMWDetailPolicy(self, run)
        return policy

    def fetch_record(self, run, source, response, retain_raw=True):
        return self.db.save_fetch(
            run,
            source,
            response.url,
            response.final_url,
            response.status,
            response.body,
            response.timestamp,
            response.transport,
            response.error,
            retain_raw=retain_raw,
        )

    def _de_removal_allowed(self, listing_id, original, requested_url, response):
        """Removal requires the publication URI and a response no older than its last presence."""
        from .provenance import timestamp

        listing = self.db.conn.execute(
            "SELECT source,external_id,url,last_seen FROM listings WHERE id=?",
            (listing_id,),
        ).fetchone()
        if not listing or (listing["source"], listing["external_id"]) != (
            original.source,
            original.external_id,
        ):
            return False
        received, seen = timestamp(response.timestamp), timestamp(listing["last_seen"])
        if received is None or seen is None or received < seen:
            return False
        try:
            return all(
                self._source(uri) == original.source
                and canonical_id(original.source, uri) == original.external_id
                for uri in (
                    original.url,
                    listing["url"],
                    requested_url,
                    response.url,
                    response.final_url,
                )
            )
        except (ValueError, TypeError):
            return False

    def queue_details(self, item, listing_id):
        if self.country == "DE" and item.source == "bmw_de":
            # Native inventory already contains VIN/specification; the detail UI has not yet been accepted.
            return
        if self.country == "DE" and item.source == "pkw_de":
            from .germany import canonical_de_id

            identity = canonical_de_id(item.source, item.url)
            if identity != item.external_id or not identity.isdigit():
                raise ValueError("PKW detail publication identity mismatch")
            self.detail_jobs["https://www.pkw.de/api/v1/cars/" + identity] = (
                item.source,
                listing_id,
                item,
            )
            return
        if item.source == "facebook":
            # Supplied observations are updated through normal account access, not the daemon.
            return
        marktnet_id = (
            canonical_id("marktnet", item.url)
            if item.source == "marktnet"
            and urlsplit(item.url).hostname in {"marktnet.nl", "www.marktnet.nl"}
            else None
        )
        if marktnet_id and marktnet_id.isdigit():
            url = "https://www.marktnet.nl/advertentie.xml?id=" + marktnet_id
            self.detail_jobs[url] = ("marktnet", listing_id, item)
        elif item.source != "gaspedaal" and "#" not in item.url:
            self.detail_jobs[item.url] = (item.source, listing_id, item)
        for link in item.links:
            # Retained Quoka/12Gebrauchtwagen listings carry URL strings; other adapters use mappings.
            if isinstance(link, str):
                url, label = link, None
            elif isinstance(link, dict):
                url, label = link.get("url"), link.get("label")
            else:
                continue
            if not isinstance(url, str) or not url:
                continue
            if (
                marktnet_id
                and identify_source(url) == "marktnet"
                and canonical_id("marktnet", url) == marktnet_id
            ):
                continue
            # A dealer homepage is not an advertisement; don't attach its arbitrary inventory.
            if label == "dealer" and urlsplit(url).path in ["", "/"]:
                continue
            if "api.gaspedaal.nl/redirect/vehicle/" in url or urlsplit(
                url
            ).path not in ["", "/"]:
                self.detail_jobs.setdefault(url, ("portal", listing_id, item))

    def ingest_response(self, run, source, key, response, queue=True):
        if response.error or not response.body:
            self.fetch_record(run, source, response)
            raise ValueError(response.error or "empty_response")
        try:
            page = self._parse(source, response.body, response.final_url)
            if self.country == "DE" and page.search_filters.get("country") != "DE":
                raise ValueError("unconfirmed_search_country")
        except Exception:
            self.fetch_record(run, source, response)
            raise
        fields = [
            "external_id",
            "title",
            "price",
            "price_type",
            "mileage",
            "year",
            "transmission",
            "fuel",
            "kenteken",
            "description",
            "images",
        ]
        legacy_nl = (
            self.country == "NL"
            and self.config.get("scope", {}).get(
                "model_profile", "bmw_218i_active_tourer"
            )
            == "bmw_218i_active_tourer"
        )
        if legacy_nl:
            from .nl_publication import publication_facts

        signature = hashlib.sha256(
            json_text(
                {
                    "total": page.total,
                    "page": page.page,
                    "ads": sorted(
                        [
                            {
                                **{k: getattr(x, k) for k in fields},
                                **(
                                    {"publication": publication_facts(vars(x))}
                                    if legacy_nl
                                    else {}
                                ),
                            }
                            for x in page.listings
                        ],
                        key=lambda x: x["external_id"],
                    ),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        state_key = (
            "search_signature:"
            + source
            + ":"
            + hashlib.sha256(response.url.encode()).hexdigest()
        )
        record = self.db.conn.execute(
            "SELECT value FROM monitor_state WHERE key=?", (state_key,)
        ).fetchone()
        previous = json.loads(record[0]) if record else {}
        retain = (
            self.country == "DE"
            or not page.listings
            or signature != previous.get("signature")
            or previous.get("day") != response.timestamp[:10]
        )
        fetch_id = self.fetch_record(run, source, response, retain_raw=retain)
        self.db.conn.execute(
            "INSERT OR REPLACE INTO monitor_state VALUES (?,?)",
            (
                state_key,
                json.dumps({"signature": signature, "day": response.timestamp[:10]}),
            ),
        )
        self.db.conn.commit()
        targets = 0
        for item in page.listings:
            if (
                self.config.get("scope", {}).get("model_profile")
                == "configured_vehicle"
            ):
                listing_id = self._observe(run, item, response.timestamp, fetch_id, key)
                targets += int(item.scope == "target" and listing_id is not None)
                if queue and listing_id and item.scope != "excluded":
                    self.queue_details(item, listing_id)
                continue
            if item.scope == "excluded":
                if (
                    self.country == "DE" and item.raw.get("market_country") == "DE"
                ) or legacy_nl:
                    listing_id = self._observe(
                        run, item, response.timestamp, fetch_id, key
                    )
                    if legacy_nl and (
                        item.source == "autokopen"
                        or (
                            item.source == "autoscout24"
                            and listing_id is not None
                            and self.db.conn.execute(
                                "SELECT 1 FROM listings WHERE id=? AND source=? "
                                "AND external_id=? AND scope='excluded'",
                                (listing_id, item.source, item.external_id),
                            ).fetchone()
                        )
                    ):
                        for url, (_, queued_id, _) in list(self.detail_jobs.items()):
                            if listing_id is not None and queued_id == listing_id:
                                self.detail_jobs.pop(url)
                continue
            if self.country == "DE" and item.raw.get("market_country") != "DE":
                self.failures.append(
                    "country_unconfirmed:" + source + ":" + item.external_id
                )
                continue
            targets += 1
            listing_id = self._observe(run, item, response.timestamp, fetch_id, key)
            if (
                legacy_nl
                and item.source == "autokopen"
                and (
                    item.scope not in {"target", "needs_confirmation"}
                    or item.raw.get("field_conflicts")
                )
            ):
                # Final bound registry/field conflicts cannot become own-detail work.
                for url, (_, queued_id, _) in list(self.detail_jobs.items()):
                    if listing_id is not None and queued_id == listing_id:
                        self.detail_jobs.pop(url)
                targets -= 1
                continue
            if queue and listing_id:
                self.queue_details(item, listing_id)
        return page, targets

    def collect_query(self, run, query):
        if (
            self.country == "NL"
            and self.config.get("scope", {}).get(
                "model_profile", "bmw_218i_active_tourer"
            )
            == "bmw_218i_active_tourer"
            and query.get("source") == "autowereld"
        ):
            from .autowereld_query_backoff import query_backoff_until

            started = now()
            until = query_backoff_until(self.db.conn, query, self.config, as_of=started)
            if until:
                self.db.finish_source(
                    run,
                    query["source"],
                    query["key"],
                    started,
                    "partial",
                    None,
                    0,
                    0,
                    0,
                    "access_refusal_backoff_until:" + until,
                    infer_missing=False,
                )
                return
        if (
            self.country == "DE"
            and query.get("inventory_contract") == "wiveda_used_218_active"
        ):
            from .germany_dealer_inventory import collect_wiveda_inventory

            collect_wiveda_inventory(self, run, query)
            return
        if (
            self.country == "DE"
            and query.get("inventory_contract") == "autoland_bmw_2er"
        ):
            from .germany_dealer_inventory import collect_dealer_inventory

            collect_dealer_inventory(self, run, query)
            return
        if self.country == "DE" and query["source"] in {
            "instamotion_de",
            "carwow_de",
            "markt_de",
            "facebook_de",
        }:
            from .germany_additional import collect_additional
            from .germany_additional_public import AdditionalPublicGetter

            previous = self.fetcher
            mapped_countries = {}
            if query["source"] == "carwow_de":
                row = self.db.conn.execute(
                    "SELECT value FROM monitor_state WHERE key='germany_country_publications:carwow_de'"
                ).fetchone()
                mapped_countries = {
                    identity: proof["claim_url"]
                    for identity, proof in (json.loads(row[0]) if row else {}).items()
                    if isinstance(proof, dict)
                    and isinstance(proof.get("claim_url"), str)
                }
            self.fetcher = AdditionalPublicGetter(
                max_requests=query.get("request_limit", 20),
                country_publications={
                    **mapped_countries,
                    **query.get("country_publications", {}),
                },
            )
            try:
                collect_additional(self, run, query)
            finally:
                self.fetcher = previous
            return
        if (
            self.country == "DE"
            and query["source"] == "dealer:fahrzeugmarkt.auto-straussberger.de"
        ):
            from .bmw_dedicated import collect_bmw_mobile_dealer

            started = now()
            result = collect_bmw_mobile_dealer(self, run, query)
            status = (
                "complete"
                if result["status"] == "complete_dealer_subset"
                else "partial"
            )
            self.db.finish_source(
                run,
                query["source"],
                query["key"],
                started,
                status,
                result["expected_count"],
                result["received_count"],
                result["target_count"],
                1,
                "; ".join(result["errors"]) or None,
                infer_missing=False,
            )
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return
        if self.country == "DE" and query["source"] in {
            "gebrauchtwagen_de",
            "quoka_de",
        }:
            from .germany_discovery import collect_discovery

            try:
                ledger = collect_discovery(self, run, query)
            except Exception as error:  # noqa: BLE001 — discovery failure does not stop other sources
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
                return
            directory = self.db.path.parent / "enumeration"
            directory.mkdir(exist_ok=True)
            (
                directory
                / (str(run) + "-" + query["source"] + "-" + query["key"] + ".json")
            ).write_text(json.dumps(ledger, ensure_ascii=False, indent=2))
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in ledger.items()
                        if k not in {"cards", "pages", "detail_results"}
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return
        if self.country == "DE" and query["source"] == "bmw_de":
            from .germany_bmw_collect import collect_bmw

            try:
                collect_bmw(self, run, query)
            except Exception as error:  # noqa: BLE001 — record BMW UI failure separately from other platforms
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
            return
        if self.country == "DE" and query["source"] == "kleinanzeigen_de":
            from .germany_kleinanzeigen import collect_kleinanzeigen

            ledger = collect_kleinanzeigen(self, run, query)
            directory = self.db.path.parent / "enumeration"
            directory.mkdir(exist_ok=True)
            (
                directory / (str(run) + "-kleinanzeigen-" + query["key"] + ".json")
            ).write_text(json.dumps(ledger, ensure_ascii=False, indent=2))
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in ledger.items()
                        if k not in {"cards", "pages", "detail_results"}
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return
        if query.get("partition") == "capacity":
            from .germany_enumeration import collect_capacity_partitions

            result = collect_capacity_partitions(self, run, query)
            directory = self.db.path.parent / "enumeration"
            directory.mkdir(exist_ok=True)
            (
                directory
                / (str(run) + "-" + query["source"] + "-" + query["key"] + ".json")
            ).write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in result.items()
                        if k not in {"ids", "root_ids", "root_after_ids"}
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return
        if query.get("partition") == "price":
            from .partitions import collect_price_partitions

            print(
                json.dumps(
                    collect_price_partitions(self, run, query), ensure_ascii=False
                ),
                flush=True,
            )
            return
        source, key, started = query["source"], query["key"], now()
        get = (
            partial(self.fetcher.get, request_body=query["request_body"])
            if "request_body" in query
            else self.fetcher.get
        )
        if query.get("transport") == "browser_public":
            if (
                self.country != "DE"
                or source != "mobile_de"
                or query.get("role") != "dealer_subset"
            ):
                raise ValueError("Unverified public browser query")

            def get(url, refresh=False):
                return self.fetcher.browser_get(url)

        if self.country == "DE" and source == "autohero_de":
            from .germany_secondary import secondary_page_request

            def get(url, refresh=False):
                offset = int(parse_qs(urlsplit(url).query).get("offset", [0])[0])
                request = secondary_page_request(source, query["url"], offset // 24 + 1)
                return self.fetcher.get(
                    with_query(request["url"], offset=offset),
                    refresh=refresh,
                    request_body=request["body"],
                )

        autoscout = source in {"autoscout24", "autoscout24_de"}
        if autoscout:
            from .germany_enumeration import _blocked_response
            from .mercedes_autoscout import _host

        def access_denied(response):
            return _blocked_response(response) or bool(
                re.search(
                    r"(?is)<title[^>]*>\s*(?:login|log in|sign in|anmelden|inloggen)\b",
                    (response.body or "")[:4096],
                )
            )

        expected, received, targets, pages, ids = None, 0, 0, 0, set()
        errors, status = [], "partial"
        try:
            response = get(query["url"])
            if (
                autoscout
                and response.status == 404
                and query.get("fallback_url")
                and not access_denied(response)
            ):
                # A generated model route that the site does not know.
                self.fetch_record(run, source, response)
                errors.append("model_route_not_found_used_make_route")
                query = {**query, "url": query["fallback_url"]}
                response = get(query["url"])
                if response.status == 404:
                    self.fetch_record(run, source, response)
                    raise ValueError("make_route_not_found")
            if autoscout and (
                access_denied(response)
                or response.error
                or response.status != 200
                or not response.body
            ):
                self.fetch_record(run, source, response)
                raise ValueError(
                    response.error
                    or ("access_response_held" if access_denied(response) else None)
                    or (
                        "http_" + str(response.status)
                        if response.status != 200
                        else "empty_response"
                    )
                )
            if source == "autotrack" and "other.toast" in parse_qs(
                urlsplit(response.final_url).query
            ):
                self.fetch_record(run, source, response)
                raise ValueError("filter_validation_redirect")
            first, count = self.ingest_response(run, source, key, response)
            if (
                self.country == "DE"
                and source == "mobile_de"
                and query.get("role") != "dealer_subset"
                and first.search_filters.get("coverage_status") != "verified"
            ):
                errors.append("national_mobile_scope_unverified")
            pages, targets = 1, count
            expected = first.total
            if first.page is not None and first.page != 1:
                errors.append("page_number_mismatch:1")
            ids.update(x.external_id for x in first.listings)
            total_pages = first.pages or 1
            if query.get("page_limit") and total_pages > query["page_limit"]:
                total_pages = query["page_limit"]
                errors.append("intentional_recent_only")
            max_pages = query.get("max_pages", self.config.get("max_pages", 100))
            if total_pages > max_pages:
                errors.append("page_limit_exceeded")
                total_pages = max_pages
            if not first.listings:
                status = (
                    "partial"
                    if errors
                    else "zero_results"
                    if expected == 0
                    else "unparsed"
                )
                errors.append(
                    "No structured listing data; inventory is not confirmed empty"
                    if expected != 0
                    else ""
                )
            else:
                routes = [
                    (number, page_url(query, number, first))
                    for number in range(2, total_pages + 1)
                ]
                if any(url is None for _, url in routes):
                    errors.append("pagination_route_missing")
                routes = [(number, url) for number, url in routes if url]
                with bounded_responses(
                    get, (url for _, url in routes), query.get("workers", self.workers)
                ) as responses:
                    for (number, requested_route), response in zip(routes, responses):
                        try:
                            requested_url = requested_route
                            if autoscout and access_denied(response):
                                self.fetch_record(run, source, response)
                                raise ValueError("access_response_held")
                            refreshable = True
                            transient = False
                            if autoscout:
                                final_path = urlsplit(response.final_url).path
                                refreshable = (
                                    "request_body" not in query
                                    and str(query.get("method", "GET")).upper() == "GET"
                                    and response.url == requested_url
                                    and not access_denied(response)
                                )
                                transient = (
                                    refreshable
                                    and _host(requested_url, self.country)
                                    and _host(response.final_url, self.country)
                                    and final_path.startswith(("/lst/", "/_next/data/"))
                                    and response.status in {500, 502, 503, 504}
                                    and response.error
                                    in {None, "http_" + str(response.status)}
                                    and response.final_url == requested_url
                                )
                            failed = (
                                response.error
                                or not response.body
                                or (autoscout and response.status != 200)
                            )
                            if failed:
                                if transient:
                                    self.fetch_record(run, source, response)
                                    preview = Page([])
                                elif autoscout:
                                    self.fetch_record(run, source, response)
                                    raise ValueError(
                                        response.error
                                        or (
                                            "http_" + str(response.status)
                                            if response.status != 200
                                            else "empty_response"
                                        )
                                    )
                                else:
                                    self.ingest_response(run, source, key, response)
                            else:
                                try:
                                    preview = self._parse(
                                        source, response.body, response.final_url
                                    )
                                except Exception:
                                    if autoscout:
                                        self.fetch_record(run, source, response)
                                    raise
                            preview_ids = {x.external_id for x in preview.listings}
                            repeated = bool(
                                preview_ids
                                and (
                                    preview_ids.issubset(ids)
                                    if autoscout
                                    else len(preview_ids & ids)
                                    >= max(1, len(preview_ids) // 2)
                                )
                            )
                            if (
                                transient
                                or (preview.page is not None and preview.page != number)
                                or not preview_ids
                                or repeated
                                or preview.search_filters != first.search_filters
                                or (
                                    source == "autotrack"
                                    and "other.toast"
                                    in parse_qs(urlsplit(response.final_url).query)
                                )
                            ) and (not autoscout or refreshable):
                                if autoscout and not transient:
                                    self.fetch_record(run, source, response)
                                response = get(
                                    requested_url if autoscout else response.url,
                                    refresh=True,
                                )
                                if autoscout and (
                                    access_denied(response)
                                    or response.error
                                    or response.status != 200
                                    or not response.body
                                ):
                                    self.fetch_record(run, source, response)
                                    raise ValueError(
                                        response.error
                                        or (
                                            "access_response_held"
                                            if access_denied(response)
                                            else None
                                        )
                                        or (
                                            "http_" + str(response.status)
                                            if response.status != 200
                                            else "empty_response"
                                        )
                                    )
                                try:
                                    preview = self._parse(
                                        source, response.body or "", response.final_url
                                    )
                                except Exception:
                                    if autoscout:
                                        self.fetch_record(run, source, response)
                                    raise
                            if autoscout:
                                preview_ids = {x.external_id for x in preview.listings}
                                repeated = bool(
                                    preview_ids and preview_ids.issubset(ids)
                                )
                                if not preview_ids or repeated:
                                    self.fetch_record(run, source, response)
                                    errors.append(
                                        "empty_or_repeated_page:" + str(number)
                                    )
                                    continue
                            if autoscout and (
                                preview.page is None
                                or preview.total is None
                                or preview.pages is None
                            ):
                                self.fetch_record(run, source, response)
                                errors.append(
                                    "pagination_counter_unverified:" + str(number)
                                )
                                continue
                            if autoscout and preview.pages != first.pages:
                                note = (
                                    f"page_count_changed:{first.pages}->{preview.pages}"
                                )
                                if note not in errors:
                                    errors.append(note)
                            if preview.page is not None and preview.page != number:
                                self.fetch_record(run, source, response)
                                errors.append("page_number_mismatch:" + str(number))
                                break
                            if preview.search_filters != first.search_filters:
                                response.error = "search_filters_changed"
                                self.fetch_record(run, source, response)
                                errors.append("search_filters_changed:" + str(number))
                                continue
                            if source == "autotrack" and "other.toast" in parse_qs(
                                urlsplit(response.final_url).query
                            ):
                                self.fetch_record(run, source, response)
                                errors.append(
                                    "filter_validation_redirect:" + str(number)
                                )
                                continue
                            page, count = self.ingest_response(
                                run, source, key, response
                            )
                            if (
                                page.total is not None
                                and expected is not None
                                and page.total != expected
                            ):
                                note = f"counter_changed:{expected}->{page.total}"
                                if note not in errors:
                                    errors.append(note)
                            targets += count
                            pages += 1
                            current_ids = {x.external_id for x in page.listings}
                            if page.page is not None and page.page != number:
                                errors.append("page_number_mismatch:" + str(number))
                            if not current_ids or current_ids.issubset(ids):
                                errors.append("empty_or_repeated_page:" + str(number))
                                break
                            ids.update(current_ids)
                        except Exception as error:  # noqa: BLE001 — log the source error and continue with the next source
                            errors.append("page_" + str(number) + ":" + str(error))
                            if response.status in (403, 429) or (
                                autoscout and access_denied(response)
                            ):
                                break
                received = len(ids)
                if expected is not None and received != expected:
                    errors.append(f"count_mismatch:{received}/{expected}")
                if query.get("coverage") != "enumerate":
                    errors.append("pagination_not_certified")
                if expected is None:
                    errors.append("counter_unavailable")
                status = "complete" if not errors else "partial"
        except Exception as error:  # noqa: BLE001 — log the source error and continue with the next source
            status = "error"
            errors.append(str(error))
        received = len(ids)
        self.db.finish_source(
            run,
            source,
            key,
            started,
            status,
            expected,
            received,
            targets,
            pages,
            "; ".join(x for x in errors if x) or None,
            infer_missing=query.get("infer_missing", True),
        )
        print(
            json.dumps(
                {
                    "source": source,
                    "query": key,
                    "status": status,
                    "received": received,
                    "expected": expected,
                    "targets": targets,
                    "pages": pages,
                    "errors": errors,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    def details(self, run):
        jobs = list(self.detail_jobs.items())
        deferred = 0

        def native_wiveda(job):
            from .bmw_dedicated import bmw_wiveda_context

            url, (source, _, original) = job
            return (
                self.country == "DE"
                and bmw_wiveda_context(self)
                and (
                    source == "dealer:wiveda.de"
                    or original.source == "dealer:wiveda.de"
                    or self._source(url) == "dealer:wiveda.de"
                )
            )

        def record_backoff(original, excluded=True):
            nonlocal deferred
            deferred += int(excluded)
            stats = self.detail_stats.setdefault(
                original.source,
                {"eligible": 0, "attempted": 0, "parsed": 0, "errors": 0},
            )
            stats["backoff"] = stats.get("backoff", 0) + 1

        source_backoffs = {
            original.source: self._source_backoff_until(original.source)
            for original in {job[1][2].source: job[1][2] for job in jobs}.values()
        }
        ready = []
        for job in jobs:
            if source_backoffs[job[1][2].source]:
                record_backoff(job[1][2])
            else:
                ready.append(job)
        jobs = ready
        from .gaspedaal_detail_policy import prepare
        from .trovit_detail_policy import in_error_backoff, publication_key

        alias_policy = prepare(self, jobs)
        eligible_native = []
        for job in jobs:
            url, (_, _, original) = job
            if (
                in_error_backoff(self, url, original)
                or alias_policy.get(url, (None, False))[1]
            ):
                record_backoff(original)
            else:
                eligible_native.append(job)
        jobs = eligible_native
        if jobs and self.config.get("detail_cache_hours"):
            cutoff = self.config["detail_cache_hours"]
            eligible = []
            from .opportunities import evaluate

            favorite_vehicles = {
                x["vehicle_id"]
                for x in evaluate(self.db.conn, self.config)["opportunities"]
            }
            favorite_listings = {
                r["id"]
                for r in self.db.conn.execute("SELECT id,vehicle_id FROM listings")
                if r["vehicle_id"] in favorite_vehicles
            }
            # Scan the event history once; per-listing MAX queries have no supporting
            # event index and make a large detail queue quadratic in history size.
            changed_at = dict(
                self.db.conn.execute(
                    "SELECT listing_id,MAX(observed_at) FROM events "
                    "WHERE kind IN ('first_observed','price_eur_changed') GROUP BY listing_id"
                )
            )
            for job in jobs:
                url, (_, listing_id, original) = job
                if native_wiveda(job):
                    eligible.append((True, True, True, "", job))
                    continue
                previous = self.db.conn.execute(
                    "SELECT fetched_at,error,status FROM fetches WHERE url=? OR final_url=? ORDER BY fetched_at DESC,id DESC LIMIT 1",
                    (url, url),
                ).fetchone()
                if (
                    original.source == "marktnet"
                    and urlsplit(url).path == "/advertentie.xml"
                ):
                    blocked = self.db.conn.execute(
                        "SELECT fetched_at,error,status FROM fetches WHERE (url=? OR final_url=?) AND status IN (403,429) ORDER BY fetched_at DESC,id DESC LIMIT 1",
                        (original.url, original.url),
                    ).fetchone()
                    if (
                        blocked
                        and (
                            datetime.now(UTC)
                            - datetime.fromisoformat(blocked["fetched_at"])
                        ).total_seconds()
                        < self.config.get("detail_error_backoff_hours", 6) * 3600
                    ):
                        record_backoff(original)
                        continue
                event = changed_at.get(listing_id)
                stale = (
                    not previous
                    or (
                        datetime.now(UTC)
                        - datetime.fromisoformat(previous["fetched_at"])
                    ).total_seconds()
                    > (
                        self.config.get("detail_error_backoff_hours", 6)
                        if previous["error"]
                        else cutoff
                    )
                    * 3600
                )
                changed = bool(
                    previous
                    and not previous["error"]
                    and event
                    and event > previous["fetched_at"]
                )
                parser_upgrade = (
                    original.source == "marktnet"
                    and not original.raw.get("detail_complete")
                    and not original.raw.get("public_xml")
                    and (not previous or not previous["error"])
                )
                parser_upgrade = parser_upgrade or bool(
                    self.parser_refresh_since
                    and previous
                    and not previous["error"]
                    and previous["fetched_at"] < self.parser_refresh_since
                )
                source_cutoff = self.source_parser_refresh_since.get(original.source)
                parser_upgrade = parser_upgrade or bool(
                    source_cutoff
                    and previous
                    and previous["status"] == 200
                    and previous["error"]
                    in (None, "detail_unparsed", "detail_parse_error")
                    and previous["fetched_at"] < source_cutoff
                )
                if stale or changed or parser_upgrade:
                    # New and changed listings before stale background work; budget-friendly manual first.
                    eligible.append(
                        (
                            listing_id not in favorite_listings,
                            not changed,
                            not (
                                original.transmission == "manual"
                                and original.price
                                and original.price <= 10000
                            ),
                            previous["fetched_at"] if previous else "",
                            job,
                        )
                    )
                elif previous and (
                    previous["error"] or previous["status"] in (403, 429)
                ):
                    record_backoff(original)
            jobs = [x[-1] for x in sorted(eligible, key=lambda x: x[:-1])]
        cursor = self.db.conn.execute(
            "SELECT value FROM monitor_state WHERE key='detail_queue_cursor'"
        ).fetchone()
        cursor_state = json.loads(cursor[0]) if cursor else {}
        last_source = cursor_state.get("source")
        generic_priority = (
            self.config.get("scope", {}).get("model_profile") == "configured_vehicle"
        )
        verification_phase = cursor_state.get("verification_phase", 0)
        if type(verification_phase) is not int or not 0 <= verification_phase < 5:
            verification_phase = 0
        target_source = cursor_state.get("target_source", last_source)
        verification_source = cursor_state.get("verification_source", last_source)
        if generic_priority:
            targets = deque(
                fair_detail_jobs(
                    [job for job in jobs if job[1][2].scope == "target"], target_source
                )
            )
            pending = deque(
                fair_detail_jobs(
                    [job for job in jobs if job[1][2].scope != "target"],
                    verification_source,
                )
            )
        else:
            jobs = fair_detail_jobs(jobs, last_source)
        limit = (
            self.config.get("fast_detail_limit", 25)
            if self.fast
            else self.config.get("detail_batch_limit", 75)
        )
        limit = min(limit, self.config.get("detail_limit", limit))
        policy = self.specialized_detail_policy(run) if self.country == "DE" else None
        if policy:
            limit = min(limit, policy.remaining())
        if len(jobs) > limit:
            self.failures.append("detail_backlog:" + str(len(jobs) - limit))
        completed, enriched, errors = 0, 0, 0
        native_operations = native_satisfied = 0
        filtered_deferred = deferred
        served_source = None
        for _, (_, _, original) in jobs:
            stats = self.detail_stats.setdefault(
                original.source,
                {"eligible": 0, "attempted": 0, "parsed": 0, "errors": 0},
            )
            stats["eligible"] += 1
        blocked_sources = set()
        dispatched_jobs = deque()
        dispatched, skipped = 0, 0

        def requested_urls():
            nonlocal dispatched
            phase = verification_phase
            while dispatched - skipped < limit:
                for group in (targets, pending):
                    while group and group[0][1][2].source in blocked_sources:
                        record_backoff(group.popleft()[1][2], excluded=False)
                if not targets and not pending:
                    return
                # Settled responses replace the reservation phase. A skipped
                # response leaves the owed slot and HTTP allowance available.
                if completed + skipped == dispatched:
                    phase = verification_phase
                group = pending if pending and (phase == 4 or not targets) else targets
                job = group.popleft()
                phase = min(4, phase + 1) if job[1][2].scope == "target" else 0
                dispatched_jobs.append(job)
                dispatched += 1
                yield job[0]

        # Skipped portable slots can refill from the entire eligible frontier.
        publication_keys = {
            url: publication_key(self, url, original)
            or alias_policy.get(url, (None, False))[0]
            for url, (_, _, original) in jobs
        }
        # Legacy aliases share a reservation, while the original frontier and
        # census remain intact. Generic GC02 keeps its actual-response planner.
        dispatch_jobs = []
        publication_aliases = defaultdict(list)
        if not generic_priority:
            reserved_publications = set()
            for job in jobs:
                key = publication_keys[job[0]]
                if key is not None and key in reserved_publications:
                    publication_aliases[key].append(job)
                else:
                    dispatch_jobs.append(job)
                    if key is not None:
                        reserved_publications.add(key)

        def record_aliases(planned_url, actual_url, *, denied):
            family = [(planned_url, self.detail_jobs[planned_url])]
            family.extend(publication_aliases.get(publication_keys[planned_url], ()))
            for alias_url, (_, _, alias) in family:
                if alias_url == actual_url:
                    continue
                if denied or alias.source in blocked_sources:
                    record_backoff(alias, excluded=False)
                else:
                    stats = self.detail_stats[alias.source]
                    stats["duplicate_publication_deferred"] = (
                        stats.get("duplicate_publication_deferred", 0) + 1
                    )

        attempted_publications = set()
        duplicate_publications = set()
        publication_lock = Lock()
        actual_jobs = {}

        def trovit_family_held(key):
            # Keys were verified on the writer thread by Trovit's own-ID policy.
            return (
                "trovit" in blocked_sources
                and isinstance(key, str)
                and key.startswith("trovit_detail_attempt:")
            )

        def fetch_detail_response(url):
            from .trovit_details import CAPTCHA_REFUSAL, trovit_captcha_refusal

            response = self.fetcher.get(url)
            if (
                response is not None
                and response.status == 200
                and (
                    response.error == CAPTCHA_REFUSAL
                    or trovit_captcha_refusal(response.body, response.final_url)
                )
            ):
                response.error = CAPTCHA_REFUSAL
                # Ordered futures may wait behind an unrelated slow request.
                # Stop new own Trovit admission when a getter observes refusal;
                # SQL, raw and attempt receipts stay on the writer thread.
                with publication_lock:
                    blocked_sources.add("trovit")
            return response

        def get_detail(url):
            if not generic_priority:
                key = publication_keys[url]
                family = [(url, self.detail_jobs[url])]
                family.extend(publication_aliases.get(key, ()))
                with publication_lock:
                    if trovit_family_held(key):
                        return None
                    if key is not None and key in attempted_publications:
                        duplicate_publications.add(url)
                        return None
                    # A logical-source skip has made no native HTTP request.
                    selected = next(
                        (
                            job
                            for job in family
                            if job[1][2].source not in blocked_sources
                        ),
                        None,
                    )
                    if selected is None:
                        return None
                    if key is not None:
                        # A started getter claims the family even on native
                        # refusal or an uncertain None; siblings cannot retry.
                        attempted_publications.add(key)
                    actual_jobs[url] = selected
                return fetch_detail_response(selected[0])
            if self.detail_jobs[url][2].source in blocked_sources:
                return None
            key = publication_keys[url]
            if trovit_family_held(key):
                return None
            if key is not None:
                with publication_lock:
                    if key in attempted_publications:
                        duplicate_publications.add(url)
                        return None
                    attempted_publications.add(key)
            return fetch_detail_response(url)

        @contextmanager
        def detail_responses():
            if generic_priority:
                # Generic slots depend on the preceding actual response; avoid
                # speculative requests across a denial or an owed verification.
                with bounded_responses(get_detail, requested_urls(), 1) as responses:
                    yield responses
                return

            def legacy_batches():
                nonlocal \
                    completed, \
                    enriched, \
                    errors, \
                    native_operations, \
                    native_satisfied, \
                    served_source
                position = 0
                while position < len(dispatch_jobs):
                    job = dispatch_jobs[position]
                    if native_wiveda(job):
                        # Complete the current generic batch before a two-request native job;
                        # keep walking after paid quota for a free accepted cache in the tail.
                        from .bmw_dedicated import fetch_wiveda_detail
                        from .bmw_detail_policy import DetailDeferred

                        url, (source, listing_id, original) = job
                        position += 1
                        stats = self.detail_stats[original.source]
                        before = policy.used
                        try:
                            _, proof = fetch_wiveda_detail(
                                self,
                                run,
                                original,
                                requested_url=url,
                                queued_source=source,
                                listing_id=listing_id,
                            )
                            native_satisfied += 1
                            if proof.cached:
                                stats["cached"] = stats.get("cached", 0) + 1
                            else:
                                stats["parsed"] += 1
                                enriched += 1
                        except DetailDeferred as error:
                            if "backoff" in error.reason:
                                record_backoff(original)
                            else:
                                field = (
                                    "deferred"
                                    if error.reason == "detail_budget_deferred"
                                    else "held"
                                )
                                stats[field] = stats.get(field, 0) + 1
                            self.failures.append(error.reason + ":" + url)
                        except (
                            ValueError,
                            TypeError,
                            KeyError,
                            AttributeError,
                        ) as error:
                            errors += 1
                            stats["errors"] += 1
                            self.failures.append("detail:" + source + ":" + str(error))
                        finally:
                            spent = policy.used - before
                            completed += spent
                            native_operations += spent
                            stats["attempted"] += spent
                            if spent:
                                served_source = original.source
                                cursor_state["source"] = served_source
                                self.db.conn.execute(
                                    "INSERT OR REPLACE INTO monitor_state VALUES ('detail_queue_cursor',?)",
                                    (json.dumps(cursor_state),),
                                )
                                self.db.conn.commit()
                        continue

                    if completed >= limit:
                        position += 1
                        continue
                    batch = []
                    while (
                        position < len(dispatch_jobs)
                        and len(batch) < limit - completed
                        and not native_wiveda(dispatch_jobs[position])
                    ):
                        batch.append(dispatch_jobs[position])
                        position += 1
                    with bounded_responses(
                        get_detail, (job[0] for job in batch), self.workers
                    ) as responses:
                        for job, response in zip(batch, responses):
                            dispatched_jobs.append(job)
                            yield response

            responses = legacy_batches()
            try:
                yield responses
            finally:
                responses.close()

        with detail_responses() as responses:
            for response in responses:
                url, (source, listing_id, original) = dispatched_jobs.popleft()
                planned_url = url
                if not generic_priority:
                    url, (source, listing_id, original) = actual_jobs.get(
                        planned_url, (url, (source, listing_id, original))
                    )
                if response is None:
                    skipped += 1
                    if planned_url in duplicate_publications:
                        stats = self.detail_stats[original.source]
                        stats["duplicate_publication_deferred"] = (
                            stats.get("duplicate_publication_deferred", 0) + 1
                        )
                    else:
                        record_backoff(original, excluded=False)
                    if not generic_priority:
                        record_aliases(planned_url, url, denied=False)
                    continue
                if policy:
                    policy.used += 1
                from .trovit_details import CAPTCHA_REFUSAL, trovit_captcha_refusal

                if (
                    response.error != CAPTCHA_REFUSAL
                    and response.status == 200
                    and trovit_captcha_refusal(response.body, response.final_url)
                ):
                    response.error = CAPTCHA_REFUSAL
                actual_source = self._source(response.final_url)
                fetch_id = self.fetch_record(run, actual_source, response)
                from .trovit_detail_policy import remember_attempt

                remember_attempt(self, url, original, fetch_id)
                completed += 1
                stats = self.detail_stats[original.source]
                stats["attempted"] += 1
                served_source = original.source
                if generic_priority:
                    if original.scope == "target":
                        target_source = original.source
                        verification_phase = min(4, verification_phase + 1)
                    else:
                        verification_source = original.source
                        verification_phase = 0
                    # Checkpoint every recorded attempt before parsing or
                    # requesting another URL, including interruption paths.
                    self.db.conn.execute(
                        "INSERT OR REPLACE INTO monitor_state VALUES ('detail_queue_cursor',?)",
                        (
                            json.dumps(
                                {
                                    "source": served_source,
                                    "verification_phase": verification_phase,
                                    "target_source": target_source,
                                    "verification_source": verification_source,
                                }
                            ),
                        ),
                    )
                    self.db.conn.commit()
                else:
                    record_aliases(
                        planned_url,
                        url,
                        denied=response.status in (403, 429)
                        or response.error == CAPTCHA_REFUSAL,
                    )
                    # Retain completed fairness across interruption without
                    # changing any existing phase/class cursor metadata.
                    cursor_state["source"] = served_source
                    self.db.conn.execute(
                        "INSERT OR REPLACE INTO monitor_state VALUES ('detail_queue_cursor',?)",
                        (json.dumps(cursor_state),),
                    )
                    self.db.conn.commit()
                if response.status in (403, 429):
                    blocked_sources.add(original.source)
                if response.error == CAPTCHA_REFUSAL:
                    blocked_sources.add("trovit")
                if response.error or not response.body:
                    errors += 1
                    stats["errors"] += 1
                    may_remove = (
                        response.status in [404, 410] and source == original.source
                    )
                    if may_remove and self.country == "DE":
                        may_remove = self._de_removal_allowed(
                            listing_id, original, url, response
                        )
                    if may_remove:
                        self.db.conn.execute(
                            "UPDATE listings SET availability=? WHERE id=?",
                            ("removed", listing_id),
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
                        self.db.conn.commit()
                    continue
                destination = urlsplit(response.final_url)
                if destination.path in {"", "/"} and not destination.query:
                    self.db.conn.execute(
                        "UPDATE fetches SET error='detail_not_advertisement' WHERE id=?",
                        (fetch_id,),
                    )
                    self.db.conn.commit()
                    errors += 1
                    stats["errors"] += 1
                    self.failures.append("detail_not_advertisement:" + url)
                    continue
                try:
                    page = self._parse(actual_source, response.body, response.final_url)
                    valid = [
                        x
                        for x in page.listings
                        if x.scope in ["target", "needs_confirmation"]
                        or (
                            self.config.get("scope", {}).get("model_profile")
                            == "configured_vehicle"
                            and x.scope == "excluded"
                            and actual_source == original.source
                            and x.external_id == original.external_id
                        )
                    ]
                    # A detail URL must describe one vehicle, not an unrelated catalogue.
                    if len(page.listings) == len(valid) == 1 and page.total in (
                        None,
                        1,
                    ):
                        item = valid[0]
                        detail_id = canonical_id(actual_source, response.final_url)
                        if not self._detail_matches(
                            actual_source,
                            url if self.country == "DE" else item.url,
                            response.final_url,
                            response.body,
                            advertisement_id=item.external_id,
                        ):
                            self.db.conn.execute(
                                "UPDATE fetches SET error='detail_identity_mismatch' WHERE id=?",
                                (fetch_id,),
                            )
                            self.db.conn.commit()
                            errors += 1
                            stats["errors"] += 1
                            self.failures.append("detail_identity_mismatch:" + url)
                            continue
                        same_listing = (
                            actual_source == original.source
                            and detail_id
                            in {
                                original.external_id,
                                canonical_id(original.source, original.url),
                            }
                        )
                        item.external_id = (
                            original.external_id if same_listing else detail_id
                        )
                        item.url = response.final_url.split("#")[0]
                        own_registration, own_vin = item.kenteken, item.vin
                        found = self._observe(run, item, response.timestamp, fetch_id)
                        if (
                            self.country != "DE"
                            and found
                            and item.raw.get("sold") is True
                        ):
                            self.db.conn.execute(
                                "UPDATE listings SET availability='seller_marked_sold' WHERE id=?",
                                (found,),
                            )
                        elif (
                            self.country != "DE"
                            and found
                            and item.raw.get("reserved") is True
                        ):
                            self.db.conn.execute(
                                "UPDATE listings SET availability='seller_reserved' WHERE id=?",
                                (found,),
                            )
                        # Only an explicit portal publication reference can transfer its own identity.
                        portal_reference = original.source == "gaspedaal" and any(
                            isinstance(portal, dict) and portal.get("klikUrl") == url
                            for portal in original.raw.get("portalen", [])
                        )
                        if (
                            found
                            and (own_registration or own_vin)
                            and (same_listing or portal_reference)
                            and (
                                self.config.get("scope", {}).get("model_profile")
                                != "configured_vehicle"
                                or (
                                    item.scope == "target"
                                    and item.raw.get("market_country") == self.country
                                )
                            )
                        ):
                            self.db.attach_identity(
                                listing_id,
                                own_registration,
                                own_vin,
                                evidence={
                                    "kind": "same_listing"
                                    if same_listing
                                    else "gaspedaal_portal_reference",
                                    "detail_listing_id": found,
                                    "fetch_id": fetch_id,
                                    "requested_url": url,
                                    "final_url": response.final_url,
                                },
                            )
                        enriched += 1
                        stats["parsed"] += 1
                    else:
                        # A plate somewhere in the HTML can belong to a recommendation.
                        self.db.conn.execute(
                            "UPDATE fetches SET error='detail_unparsed' WHERE id=?",
                            (fetch_id,),
                        )
                        errors += 1
                        stats["errors"] += 1
                        self.failures.append("detail_unparsed:" + url)
                    self.db.conn.commit()
                except Exception as error:  # noqa: BLE001 — log the source error and continue with the next source
                    self.db.conn.execute(
                        "UPDATE fetches SET error='detail_parse_error' WHERE id=?",
                        (fetch_id,),
                    )
                    self.db.conn.commit()
                    errors += 1
                    stats["errors"] += 1
                    self.failures.append("detail_parse:" + str(error))
                if completed % 50 == 0:
                    print(
                        json.dumps(
                            {
                                "details_completed": completed,
                                "details_total": min(limit, len(jobs)),
                                "enriched": enriched,
                                "errors": errors,
                            }
                        ),
                        flush=True,
                    )
        if served_source is not None and not generic_priority:
            cursor_state["source"] = served_source
            self.db.conn.execute(
                "INSERT OR REPLACE INTO monitor_state VALUES ('detail_queue_cursor',?)",
                (json.dumps(cursor_state),),
            )
            self.db.conn.commit()
        satisfied = completed - native_operations + native_satisfied
        unresolved = max(0, len(jobs) - satisfied) + filtered_deferred
        self.db.finish_source(
            run,
            "portal_details",
            "discovered_links",
            now(),
            "partial" if errors or deferred or unresolved else "complete",
            len(jobs) + filtered_deferred,
            satisfied,
            enriched,
            completed,
            f"{errors} errors; {unresolved} deferred; {deferred} in backoff"
            if errors or deferred or unresolved
            else None,
        )

    def rdw(self, run):
        if self.country == "DE":
            return
        records = self.db.conn.execute("""SELECT v.* FROM vehicles v WHERE v.kenteken IS NOT NULL AND NOT EXISTS
         (SELECT 1 FROM vehicle_checks c WHERE c.vehicle_id=v.id AND c.provider='rdw' AND julianday(c.checked_at)>julianday('now','-1 day'))""").fetchall()
        records = records[: 50 if self.fast else self.config.get("rdw_limit", 200)]
        urls = [
            "https://opendata.rdw.nl/resource/m9d7-ebf2.json?kenteken=" + r["kenteken"]
            for r in records
        ]
        successful = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for vehicle, response in zip(records, pool.map(self.fetcher.get, urls)):
                fetch_id = self.fetch_record(run, "rdw", response)
                if response.error or not response.body:
                    continue
                try:
                    data = json.loads(response.body)
                    if data:
                        self.db.conn.execute(
                            "INSERT INTO vehicle_checks(vehicle_id,checked_at,provider,data_json,fetch_id) VALUES (?,?,?,?,?)",
                            (
                                vehicle["id"],
                                response.timestamp,
                                "rdw",
                                json.dumps(data[0], ensure_ascii=False),
                                fetch_id,
                            ),
                        )
                        name = data[0].get("handelsbenaming", "").upper()
                        if "218I" in name and "ACTIVE" in name:
                            self.db.conn.execute(
                                "UPDATE listings SET scope='target' WHERE vehicle_id=? AND scope='needs_confirmation'",
                                (vehicle["id"],),
                            )
                        elif (
                            "GRAN TOURER" in name
                            or "GRAN COUPE" in name
                            or "218D" in name
                        ):
                            self.db.conn.execute(
                                "UPDATE listings SET scope='model_conflict' WHERE vehicle_id=?",
                                (vehicle["id"],),
                            )
                        successful += 1
                        self.db.conn.commit()
                except (ValueError, TypeError):
                    pass
        self.db.conn.commit()
        print(
            json.dumps({"rdw_checked": successful, "rdw_requested": len(records)}),
            flush=True,
        )

    def run(self, source_filter=None, details=True, checks=True, fast=False):
        previous_details_enabled = self.specialized_details_enabled
        self.fast = fast
        self.failures.clear()
        self.detail_stats.clear()
        self.detail_jobs.clear()
        run = self.db.start_run("fast" if fast else "live")
        queries = [
            q
            for q in self.config["queries"]
            if (not source_filter or q["source"] in source_filter)
            and q.get("enabled", True)
            and (not fast or q.get("cadence") != "broad")
        ]
        # Rotate whole BMW source groups so synchronous collectors cannot always
        # exhaust the shared allowance before a later source gets a turn.
        rotate_bmw = (
            self.country == "DE"
            and self.config.get("scope", {}).get(
                "model_profile", "bmw_218i_active_tourer"
            )
            == "bmw_218i_active_tourer"
        )
        cursor_key = "bmw_specialized_query_cursor"
        if rotate_bmw:
            row = self.db.conn.execute(
                "SELECT value FROM monitor_state WHERE key=?", (cursor_key,)
            ).fetchone()
            try:
                state = json.loads(row[0]) if row else {}
                last_source = state.get("source") if isinstance(state, dict) else None
            except (ValueError, TypeError):
                last_source = None
            groups = defaultdict(list)
            for query in queries:
                groups[query["source"]].append(query)
            order = list(groups)
            if last_source in order:
                position = order.index(last_source) + 1
                order = order[position:] + order[:position]
            queries = [query for source in order for query in groups[source]]
        try:
            self.specialized_details_enabled = details
            if (
                not fast
                and self.browser_enabled
                and self.config.get("facebook_refresh", {}).get("enabled")
                and (not source_filter or "facebook" in source_filter)
            ):
                from .facebook_browser import refresh_known

                print(
                    json.dumps(
                        refresh_known(self.db, run, self.config["facebook_refresh"])
                    ),
                    flush=True,
                )
            for query in queries:
                used_before = (
                    self.specialized_detail_policy(run).used if rotate_bmw else 0
                )
                try:
                    self.collect_query(run, query)
                except Exception as error:
                    if (
                        self.country != "DE"
                        and self.config.get("scope", {}).get("model_profile")
                        != "configured_vehicle"
                    ):
                        raise
                    self.failures.append(
                        f"query_failed:{query['source']}:{query['key']}"
                    )
                    self.db.finish_source(
                        run,
                        query["source"],
                        query["key"],
                        now(),
                        "error",
                        None,
                        None,
                        None,
                        None,
                        str(error),
                        infer_missing=False,
                    )
                    print(
                        json.dumps(
                            {
                                "source": query["source"],
                                "query": query["key"],
                                "status": "error",
                                "error": str(error),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                finally:
                    if (
                        rotate_bmw
                        and self.specialized_detail_policy(run).used > used_before
                    ):
                        self.db.conn.execute(
                            "INSERT OR REPLACE INTO monitor_state VALUES (?,?)",
                            (cursor_key, json.dumps({"source": query["source"]})),
                        )
                        self.db.conn.commit()
                if fast:
                    from .opportunities import publish

                    publish(
                        self.db, self.db.path.parent / "signals", config=self.config
                    )
            if details:
                # Drain existing stale detail work in bounded batches between fresh searches.
                from .parse import Listing

                for record in self.db.conn.execute(
                    "SELECT l.id,o.details_json FROM listings l JOIN latest_observations o ON o.listing_id=l.id WHERE l.scope IN ('target','needs_confirmation') AND l.availability='observed'"
                ):
                    self.queue_details(
                        Listing(**json.loads(record["details_json"])), record["id"]
                    )
                self.details(run)
            if checks:
                self.rdw(run)
            from .identity_merge import bridge_shared_publications

            if self.country == "DE":
                from .germany_identity import bridge_de_publications

                publisher_bridges = bridge_de_publications(self.db)
            else:
                publisher_bridges = bridge_shared_publications(self.db)
            self.db.candidates()
            for channel in self.config.get("channels", []):
                self.db.finish_source(
                    run,
                    channel["source"],
                    "channel_status",
                    now(),
                    channel["status"],
                    None,
                    0,
                    0,
                    0,
                    channel["note"],
                )
            summary = self.db.summary()
            complete = all(
                x["status"] in ["complete", "zero_results"]
                for x in summary["latest_sources"]
                if x["query_key"] != "channel_status" and x["run_id"] == run
            )
            status = "complete" if complete else "partial"
            summary["collector_errors"] = self.failures
            summary["detail_sources"] = self.detail_stats
            summary["publisher_bridges"] = publisher_bridges
            if publisher_bridges["conflicts"] or self.failures:
                status = "partial"
            self.db.finish_run(run, status, summary)
            return {"run_id": run, "status": status, **summary}
        except BaseException as error:
            self.db.finish_run(run, "interrupted", {"error": str(error)})
            raise
        finally:
            self.specialized_details_enabled = previous_details_enabled
