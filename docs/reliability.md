# Collection reliability and limits

Car Market collects evidence from configured public searches. An arbitrary
make/model selector changes local classification; it does not certify every
site, model route, country or national inventory. The default 15-page pass is
a bounded sample. Even an expanded pass can remain partial.

## Read the three diagnostics together

`status` describes saved runs and discoveries. `coverage` reconciles each
enabled query with that source's counter. `quality` describes field presence,
country proof, original confirmation times and contradictions. A healthy
process or a successfully generated English report does not establish data
accuracy or market completeness.

`query_enumeration_percent` is `100 * received_count / expected_count`, where
`received_count` is unique parsed publication IDs from that pass and
`expected_count` is the source-reported count for the exact query. The numerator
includes out-of-profile and country-unconfirmed cards. Repeated placements of
one publication do not become extra IDs. `target_ads` is a separate measure;
unresolved discoveries need their own evidence before becoming catalog entries.

The percentage is descriptive even when it reaches 100. A BMW
`/lst/bmw/218` route is broader than 218i Active Tourer; matching its counter
does not enumerate only that variant. Local model filtering cannot retrieve
vehicles excluded by the search URL's filters. Unknown or zero denominators,
overcounts, counter drift and changed filters produce a `null` percentage.
`empty_query_verified` describes a fresh, reconciled zero-result query, not an
empty national market. Check `status`, `error` and `stale` alongside the fraction.

`all_configured_queries_complete_and_fresh` concerns enabled configured queries
only. It does not certify pending details, field accuracy or unique vehicles.
There is no known national inventory denominator:
`whole_market_coverage_percent` is `null`, and `market_100_percent_verified`
is `false`. Private groups, unlisted dealer stock, unconfigured sites and
overlapping publications remain outside any national percentage.

## Declared sources and native limits

| Source | Country | Supported evidence and limits |
|---|---|---|
| AutoScout24 | NL, DE | Own native search cards and accepted own detail pages. Country and page contracts must match the request; the current adapter accepts pages 1–200. |
| Marktplaats | NL | Native HTML search or `/lrp/api/search` JSON, plus accepted own details. API pagination uses `offset` and `limit`, with a source-reported page cap. |
| AutoTrack | NL | Legacy native search and the observed own Next search envelope. Next cards bind UUID/URL and `location.countryCode`; page/query/build filters bind enumeration. Legacy cards without country proof remain discoveries until an own VDP confirms it. Related inventory cannot supply that proof. |
| Gaspedaal | NL discovery | Own search cards and counters can be retained, but the observed search contract does not confirm physical country. Fragment URLs have no accepted own VDP contract. |

`sources` lists the generic registry, not live availability. Mobile.de is not
in that registry. Ordinary access can return HTTP 403; historical BMW/Mercedes
parsers and dealer subsets do not establish generic, national mobile.de support.
403/429, CAPTCHA and login requirements remain access limitations, with backoff.

AutoScout24's 200-page cap is not a promise of 4,000 distinct publications.
Promoted or repeated placements consume slots; a Golf query can remain
incomplete even when its counter is below the nominal 200 × 20 slots. Raising
the saved `max_pages` cannot remove this cap or recover IDs never returned.

For Marktplaats API responses reporting `maxAllowedPageNumber=167` with
`limit=30`, pagination exposes at most 5,010 slots before duplication, even if
`totalResultCount` is larger. The parser uses the smaller of the pages implied
by the counter and the native cap. HTML and API are alternative query
contracts; neither implies that all Marktplaats inventory is accessible.

Gaspedaal remains discovery only. The collector does not automatically fetch
its fragment/search URLs as details or follow its outbound vehicle redirects
to invent country or identity proof. Keep those discoveries and their original
evidence; an accepted own publication contract is needed to promote them.
The search's import filter, a `.nl` domain and a query country do not prove a
vehicle's physical country.

AutoTrack can serve two search layouts. A legacy search may redirect to the
observed `/aanbod?page=1&cat=...&cy=NL` Next layout. Its own `pageQuery`,
`numberOfResults`, `numberOfPages` and `listings` drive pagination through the
verified build's `/lst.json` route. The collector retains the resolved model
filters when following pages; it does not paginate the original legacy URL.
Only own bound cards supply facts and country proof. The collector bounds this
Next adapter to pages1–200; duplicate placements and changed counters can still make
the query partial. A `custtype=D` query is a dealer subset, not every seller.

## Initialize copied search URLs

Build the image as in the README and use a new project name for each new
workspace. For a copied canonical AutoScout24 search URL:

```sh
docker compose -p copied-corolla-de create --no-recreate market
docker compose -p copied-corolla-de run --rm market init --make Toyota --model Corolla --country DE --max-pages 15 --url "https://www.autoscout24.de/lst/toyota/corolla?sort=age&desc=1&page=4"
docker compose -p copied-corolla-de run --rm market validate
```

`init` adds a missing `cy=D` for DE or `cy=NL` for NL and removes all `page`
parameters to start enumeration at page one. It preserves other filters and
prints the normalized saved `search_urls`. An explicitly incorrect, empty or
duplicate `cy` is rejected, not silently corrected. Manually edited searches
must contain exactly the matching country filter; DE uses `D`, not `DE`.
The request's country filter still does not prove a publication's location.

For a copied Marktplaats search API URL:

```sh
docker compose -p copied-corolla-mp create --no-recreate market
docker compose -p copied-corolla-mp run --rm market init --make Toyota --model Corolla --country NL --max-pages 15 --url "https://www.marktplaats.nl/lrp/api/search?l1CategoryId=91&query=Toyota+Corolla&limit=30&offset=60"
docker compose -p copied-corolla-mp run --rm market validate
```

`init` selects `pagination=marktplaats_api` for `/lrp/api/search` and resets the
copied offset to zero, preserving other filters. The API contract accepts
`limit` values 1–100 and uses 30 when omitted. HTML search URLs use
`pagination=marktplaats`. A manually edited API query must start at `offset=0`
and use the API pagination mode. A valid URL does not certify the intended
model, physical country or completeness of the returned cards.

For HTML searches, copy a URL whose filters are in the path, such as
`https://www.marktplaats.nl/l/auto-s/toyota/q/corolla/`. `init` removes a copied
terminal `/p/N/` to start at page one; manually edited HTML queries must also
start there. HTML URLs containing query parameters or a fragment are rejected:
native captures show that `?query=Corolla` and `#q:Corolla` can return the broad
Toyota inventory without a native Corolla search filter. Use the API URL above
for those filters. Changing URL components alone does not verify a filtered census.

## Bounded and expanded manual collection

Run from the directory containing `compose.yaml`, with the image already built.
The following examples use two new project names and separate volumes.
`init --max-pages` saves the global budget used by each query; `collect` has no
`--max-pages` option. For a small search-only check:

```sh
docker compose -p corolla-bounded-nl create --no-recreate market
docker compose -p corolla-bounded-nl run --rm market init --make Toyota --model Corolla --country NL --max-pages 2 --url "https://www.autoscout24.nl/lst/toyota/corolla?cy=NL&atype=C&sort=age&desc=1"
docker compose -p corolla-bounded-nl run --rm market validate
docker compose -p corolla-bounded-nl run --rm market collect --browser --no-details
docker compose -p corolla-bounded-nl run --rm market status
docker compose -p corolla-bounded-nl run --rm market coverage
docker compose -p corolla-bounded-nl run --rm market quality
docker compose -p corolla-bounded-nl run --rm market discoveries
```

For a larger search-only pass, choose an explicit page budget up to 200:

```sh
docker compose -p corolla-expanded-nl create --no-recreate market
docker compose -p corolla-expanded-nl run --rm market init --make Toyota --model Corolla --country NL --max-pages 200 --url "https://www.autoscout24.nl/lst/toyota/corolla?cy=NL&atype=C&sort=age&desc=1"
docker compose -p corolla-expanded-nl run --rm market validate
docker compose -p corolla-expanded-nl run --rm market collect --browser --no-details
docker compose -p corolla-expanded-nl run --rm market coverage
docker compose -p corolla-expanded-nl run --rm market quality
```

These are per-query upper bounds, not a completeness switch. `--no-details`
skips detail collection and pending-country enrichment, so unresolved cards
can remain discoveries. Source caps, repeated pages, changed filters/counters,
missing routes and blocked responses can still stop enumeration. Exit code 3
means partial results were saved; investigate the reasons before describing
the sample. It is not evidence that missing publications were sold.

For an existing search, keep its original `-p` and stop its daemon before a
manual write pass. Do not repeat `init`: edit the global `max_pages` in
`search.json` to 2 or the chosen expanded budget (at most 200), update any
query-specific `max_pages` overrides, and run `validate` before `collect`.
Query overrides take precedence over the global limit. One workspace permits
one writer. The same saved budgets apply to daemon passes; see
[operations.md](operations.md) for copying and validating configuration. Omitting
`--no-details` enables the saved detail budget, normally 50 requests for a broad
pass shared with pending discovery enrichment. A larger search-only pass does
not refresh missing detail fields. Resume scheduled collection with `up -d`
using that search's project name when ready.

## Field freshness and acceptance evidence

In `quality`, each field's `present_percent` measures filled values in saved
publications, not accuracy. `fresh_confirmations` uses the field's original
evidence clock or a validated newer direct confirmation of the same current value.
These bounded confirmations refresh unchanged facts without adding duplicate
market observations. A newer listing `last_seen`, database restore or report
timestamp does not renew an inherited price, mileage or other fact. Invalid,
future, conflicting and unverified legacy confirmations cannot establish freshness.
Inspect missing country proof and field disagreements as well as presence percentages.

`ads_with_retained_evidence` checks stored raw-path/hash metadata. Acceptance
must separately read the archived bytes and verify SHA-256. Retained fixture
projections prove only their stated native fields and parser contracts;
synthetic faults are regression cases, and omitted details remain unproven.

Release notes are in [CHANGELOG.md](../CHANGELOG.md). An acceptance report must
identify the exact commit/image, UTC time,
host/container platform, anonymous or authenticated access state, selector
and source URLs, page/detail budgets,
response statuses/hashes and the reconciled query/discovery/field results.
Live counts belong to that dated evidence, not to an undated claim about the
current market. English report creation preserves source observation times;
see [reports.md](reports.md) for analysis rules.

For independent fresh Docker measurements, use the opt-in
[acceptance script](../tools/live_acceptance.py) from the checkout. Build the
image from the intended revision, choose a new output directory and record the
commit and Docker host platform alongside the generated summary:

```sh
docker build -t car-market:acceptance .
python3 tools/live_acceptance.py --image car-market:acceptance --output ./acceptance-20261011 --sources autoscout24 autoscout24_de --models corolla golf --max-pages 3 --detail-limit 8 --browser
```

The script defaults to three search pages and eight detail requests per case,
all declared sources and four benchmark selectors. `--sources` and `--models`
limit cases; `--no-details` makes them search-only. Every case gets a new
volume, and an existing volume with the same run ID is refused. It uses no
accounts or imported databases, runs sequentially, and skips later cases for
a source after a detected access denial. Volumes remain for evidence inspection;
the final cleanup command names only those created by the run. Local logs and
raw data remain local.

The [Live Docker acceptance workflow](../.github/workflows/live-acceptance.yml)
is manual (`workflow_dispatch`), with a smaller one-page/two-detail budget per
case. It uploads only aggregate `summary.json` as `live-acceptance-metrics`,
not raw responses, databases or full command logs. A green job or
`acceptance_commands_passed=true` means the commands met their expected exit
codes; partial collection (exit 3) is allowed. Inspect query errors, country
proof and field freshness before making an acceptance claim. The measurements
remain exact-query publication evidence, not national market coverage.

New retained gzip captures are completed and flushed in a temporary file before
publication under their SHA-256 name. A write failure can be retried without a
partial final capture blocking the retry. Existing captures are checked and never
replaced; their original decoded bytes and history remain intact.

Query staleness follows the source's effective configured polling period plus a
30-minute allowance for completing a pass. An explicit broad query, or a source
outside a nonempty fast-source filter, uses the full interval. This query health
clock is separate from field freshness; it does not renew prices or other facts.
Native discovery stores each page and filter context separately. Older unresolved
IDs keep their original capture references after partial pagination and are not
credited to the current query counter.



Legacy BMW NL AutoWereld search queries pause after a persisted403/429 for the
same requested or final query URI. The expiry uses the original refusal capture
clock; deferred passes record partial status and zero new received/target IDs
without renewing captures, rewriting observations or inferring missing listings.
The pause survives collector/database restart. `query_error_backoff_hours`
overrides `detail_error_backoff_hours` (default6); zero explicitly disables this
query pause. Mercedes, configured searches, Germany and unrelated provider/URI
refusals keep their existing behavior.
