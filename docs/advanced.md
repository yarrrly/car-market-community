# Advanced usage and source limits

This is the manual/developer guide. The Community Preview candidate uses an
image-only `compose.yaml`; building a checkout requires the explicit developer
override. Set the same local image override for the build and later commands:

```sh
export CAR_MARKET_IMAGE="car-market:local"
```

In PowerShell:

```powershell
$env:CAR_MARKET_IMAGE = "car-market:local"
```

Build with both Compose files, as shown below. The main README's launcher
workflow and [candidate setup](release-preview.md) explain offline image loading
and the proposed unpublished release image. Existing search names still select
their original volumes. Building or reporting alone does not refresh source data.

Search and monitor car listings in the Netherlands and Germany.
Configure the make, model, aliases, generation, fuel, transmission and years
without changing Python code. The selector works with the declared source
adapters; it does not guarantee support for every site, country or model route,
or complete national inventory.

The collector retains price history, raw responses with SHA-256 hashes, source
state and stable IDs. Several listings may describe the same vehicle.
Matching year, mileage and city alone cannot merge them.

## Storage and collection frequency

Raw responses are already compressed with gzip and deduplicated by their exact
SHA-256. Dynamic HTML can still produce a new file when the listing facts are
unchanged. The collector retains this evidence and does not delete it by age.

The daemon records each full collection attempt in SQLite before network work.
A failed or interrupted full pass keeps the configured interval (six hours by
default), including after a restart. Fast checks continue at their own interval;
changing the configured search queries allows one immediate full attempt.
An explicit `collect` command starts the requested pass regardless of this schedule.

Exports replace fixed filenames; they include another database copy and the full
history CSV. Reports create dated directories with database snapshots. Run reports
when needed and include `data/raw` when backing up response evidence. Do not delete
raw files merely because they are old: observations can still reference them.

## Start with Docker

Install Docker with Compose. On Windows, use Linux containers.
The examples use Docker Compose directly; the launchers in the
[README](../README.md#try-a-first-search) do the same with less typing.
Windows/macOS Docker Desktop is not tested yet; see
[tested platforms](release-preview.md#tested-platforms).

The main search in these examples is named `corolla-nl` (`-p`). Keep that name
in every command, including upgrades from another directory: it selects the
database volume. For an existing search, use its original name; check it with
`docker compose ls -a` before switching directories.

```sh
git clone https://github.com/yarrrly/car-market-community.git
cd car-market-community
docker compose -p corolla-nl -f compose.yaml -f compose.build.yaml build
docker compose -p corolla-nl create --no-recreate market
docker compose -p corolla-nl run --rm market init --make Toyota --model Corolla --country NL --max-pages 15 --url "https://www.autoscout24.nl/lst/toyota/corolla?cy=NL&atype=C&sort=age&desc=1"
docker compose -p corolla-nl run --rm market validate
docker compose -p corolla-nl run --rm market collect --browser --no-details
docker compose -p corolla-nl run --rm market list --max-price 20000
docker compose -p corolla-nl up -d
```

Without `--url`, the tool opens AutoScout24's model page, such as
`/lst/toyota/corolla`. When AutoScout24 answers that route with 404, the pass
records the 404, uses the make's page instead and notes
`model_route_not_found_used_make_route`. Either way, the model is checked
locally against each listing's own fields. To narrow a search, copy a specific
search URL from the site. Check the site's filters: if the results
include only automatics, the collector cannot find manuals outside that search.
For Germany, use `--country DE` with AutoScout24's URL parameter **`cy=D`**;
`cy=DE` is rejected before collection. The URL filter does not prove a vehicle's country.

For a copied AutoScout24 search URL, `init` adds the missing country parameter
and removes `page` so enumeration starts at the first page. It rejects an
explicit incorrect country filter. Copied Marktplaats `/lrp/api/search` URLs
use API pagination and start at `offset=0`; other filters stay in the URL.

The first manual pass above requests search pages only. `init --max-pages 15`
saves its page budget; `collect` uses that configuration and has no
`--max-pages` option. The daemon started by `up -d` uses the saved configuration
and enables bounded detail collection. See [docs/reliability.md](reliability.md)
for expanded manual passes, copied URL initialization and source limits.

Exit code **3** after `collect` means a partial pass: data was saved, but a
source was blocked, its count did not match, or the page limit was reached.
Unconfirmed discoveries or incomplete details can also make a pass partial.
This is an expected result; `status`, `coverage` and `quality` explain the reasons.

## Search for another car

For a complete NL/DE research workflow ending in an English PDF, see
[docs/research.md](research.md).

```sh
docker compose -p mini-search run --rm market init --make MINI --model Cooper --require S --generation R53 --country NL
docker compose -p mini-search up -d
docker compose -p mini-search run --rm market list
```

A different `-p` creates a separate volume, configuration and history. Each
search has one daemon. Repeating `init` refuses to overwrite an existing search.
Applying a new profile to an existing database is prohibited.

Options:

| `init` option | Meaning |
|---|---|
| `--model-alias "218 i"` | Alternative spelling of the main model |
| `--make-alias "Mercedes Benz"` | Alternative spelling of the make |
| `--require "Active Tourer"` | Additional required part of the model name |
| `--exclude "Gran Tourer"` | Explicitly excluded variant |
| `--generation C205` | Generation from the listing's own model or structured fields |
| `--fuel petrol` | Allowed fuel type; repeatable |
| `--transmission automatic` | Allowed transmission; omit to allow any |
| `--year-from 2016 --year-to 2026` | Bounds on the published year |
| `--url "https://…"` | Search results on a supported site; repeatable |
| `--max-pages 2 --detail-limit 8` | Bounded first pass without editing JSON |

If a required field is unknown, the listing gets `needs_confirmation`.
Descriptions, year and search URL do not confirm generation or location.
For short variants such as `S` or `GT`, use exact `model_terms` groups in JSON,
for example `[ ["Cooper S"] ]` rather than separate words.

## Everyday commands

```sh
docker compose -p corolla-nl run --rm market status
docker compose -p corolla-nl run --rm market doctor --require-daemon
docker compose -p corolla-nl run --rm market coverage
docker compose -p corolla-nl run --rm market quality
docker compose -p corolla-nl run --rm market list --include-pending
docker compose -p corolla-nl run --rm market discoveries
docker compose -p corolla-nl run --rm market vehicle UUID
docker compose -p corolla-nl run --rm market export
docker compose -p corolla-nl create --no-recreate market
docker compose -p corolla-nl cp market:/workspace/exports ./exports
docker compose -p corolla-nl logs --tail 100 market
docker compose -p corolla-nl stop
docker compose -p corolla-nl create --no-recreate market
```

`list` shows publications with links; `vehicle UUID` shows merged facts, source
listings and field provenance for one vehicle. By default, `list` hides stale
and withdrawn publications; `--include-stale` includes history.
A listed price is an asking price, not a verified transaction price.

Data remains in the named Docker volume after `stop`, `down` and image rebuilds.
**`docker compose -p corolla-nl down -v` deletes the database and its volume.**
To edit the configuration, stop the daemon, copy the file out, edit it,
copy it back and validate:

```sh
docker compose -p corolla-nl stop
docker compose -p corolla-nl cp market:/workspace/search.json ./search.json
docker compose -p corolla-nl cp ./search.json market:/workspace/search.json
docker compose -p corolla-nl run --rm market validate
docker compose -p corolla-nl up -d
```

You can change search URLs, intervals and budgets. Changing `scope.selector`
or country requires a new search with a different `-p`.

`run --rm` removes only its temporary container. `cp` requires a service
container; `create --no-recreate market` creates it without starting collection
and preserves an existing container. After `down`, repeat `create` before `cp`.

## Sources and coverage limits

The generic registry declares AutoScout24 NL/DE, Marktplaats NL (HTML or the
observed search API), AutoTrack NL and Gaspedaal NL. Gaspedaal is discovery
only: its search cards do not confirm country, and its fragment links have no
accepted own vehicle detail page contract. AutoTrack needs the publication's
own detail page to resolve missing country proof. `sources` lists the registry;
it does not certify live access or inventory completeness. Other historical
BMW/Mercedes adapters remain for regression tests; they are not declared
general-purpose adapters. Mobile.de is outside this registry; ordinary requests
can return HTTP 403, and retained specialized parsers do not establish generic
support.

The tool does not bypass login or CAPTCHA. HTTP 403/429 responses are retained
as evidence, with backoff before later attempts. A failure at one source should
not stop collection from the others. Listings with an unknown country stay in
the discovery queue and receive no confirmed physical vehicle identity.

Defaults are up to 15 search pages per query and 50 detail requests per broad
pass, with a broad pass every 6 hours and a quick pass every 30 minutes. A large
market may need a higher `max_pages` and narrower searches, but source page caps
and promoted/repeated placements can still prevent enumeration. A BMW `/lst/bmw/218` search is broader
than 218i Active Tourer; its counter also covers cars outside that selector.

In `coverage`, `query_enumeration_percent` describes unique parsed publication
IDs divided by the source-reported count for that exact query. It includes
out-of-profile and unconfirmed-country cards. It is not a fraction of unique
vehicles, target vehicles or a country's market. No whole-country coverage
percentage can currently be calculated: `whole_market_coverage_percent` remains
`null`, and `market_100_percent_verified` is `false`.

`quality` separates filled fields from fresh confirmations. A recently seen
listing does not refresh a retained price, mileage or other field. A newer direct
confirmation must match the current value and its own capture evidence; original
lineage and historical rows stay unchanged. Reports use the same distinction.

Automatic RDW lookup and personal notifications are not included in the
generic profile. Keep your own workspaces, sessions and databases out of Git;
see the [fixture policy](fixture-policy.md) for what tests may contain.

## Python installation and development

```sh
uv sync --frozen --group dev --extra browser --extra reports
uv run playwright install chromium
uv run car-market --workspace ./workspace init --make Toyota --model Corolla --max-pages 15
uv run car-market --workspace ./workspace collect --browser --no-details
uv run pytest tests -q
```

Requires Python 3.12; dependencies are pinned in `uv.lock`. On Linux, the browser
may need system libraries: `uv run playwright install --with-deps chromium`.
Docker installs them during the build, as described in the
[Playwright documentation](https://playwright.dev/python/docs/docker).
Container startup and shutdown settings follow the
[Compose documentation](https://docs.docker.com/reference/compose-file/services/).

English PDF/PNG reports: [docs/reports.md](reports.md).

Operations and recovery: [docs/operations.md](operations.md).
Architecture and adding adapters: [docs/architecture.md](architecture.md).

Project code and documentation use the [MIT license](../LICENSE). Bundled Lato
fonts retain their separate [SIL Open Font License](../car_market/assets/fonts/OFL.txt).

## Explicit compact export

Full twelve-file exports remain the default. `export --export-mode compact`
keeps complete SQLite and nine CSVs under `exports/compact`, omits only history
CSV and rich vehicle JSON, and records format/path/as-of in an explicit manifest.
Old full files are retained with their original dates. See
[compact export](compact-export.md) for CLI, publication, fast/manual and
storage semantics.
