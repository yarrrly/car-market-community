# Research a car in one command

`research` creates a separate search, runs the existing collector, exports its
database and creates an English PDF/PNG report. It is a manual pass. It does not
start a background monitor.

In a Python installation with the reports extra:

```sh
car-market research --workspace ./corolla-nl --make Toyota --model Corolla --country NL --max-pages 15 --detail-limit 50
```

With Docker, the launchers in the [README](../README.md#try-a-first-search) are
the shortest form. The plain Compose equivalent uses a separate project name for
each search:

```sh
docker compose -p corolla-nl run --rm market research --make Toyota --model Corolla --country NL --max-pages 15 --detail-limit 50
```

The new workspace must be absent or empty. The command refuses to overwrite an
existing search, unrelated files or a linked workspace. The country is explicitly
`NL` or `DE`. Without `--url`, the search starts at AutoScout24's model page and
falls back to the make's page when AutoScout24 does not know the model name.
Each listing is checked against your selector either way. Add `--url` with a
supported public search URL to use a site's own filters; a URL filter alone does
not prove country or model.

The existing choices work here: `--make-alias`, `--model-alias`, `--require`,
`--exclude`, `--generation`, `--fuel`, `--transmission`, `--year-from`, `--year-to`
and repeated `--url`. Page and detail limits are saved in `search.json`. Defaults
remain 15 pages per query and 50 detail requests per pass. `--no-details` requests
search pages only. A zero detail limit can leave a partial detail backlog; the
command preserves that result rather than declaring it complete.

## Read the result

The terminal shows the collection status with plain-language notes (page limit
reached, detail pages left for the next pass, refused pages), listings that match
your search, listings that still need confirmation and the commands to try next.
Rejected cards may have no stored listing. These are publication counts, not
unique physical vehicles, available stock or a national market denominator.
Unknown country stays unknown. A refusal or empty result is not evidence that
there are no matching cars.

Every admitted attempt gets a new dated `data/research-receipts/.../receipt.json`.
It records the original collection result and exit code, export location, report
result and final exit code. A successful report has its own dated directory and
existing SHA manifest. The research receipt is an operator outcome, not a new
source capture or a certificate that every historical body can be replayed.

| Exit code | Meaning |
|---|---|
| 0 | The configured collection pass and report completed. National coverage is not certified. |
| 3 | Collection was partial. Saved evidence and any successful report remain available. Inspect the receipt for refusals, limits or pending details. |
| 2 | Setup, export, report or receipt failed. The original error is printed. A report failure after partial collection keeps code 3 and is separately marked failed in the receipt. |
| 4 | This workspace already has an active writer. No second collector is started. |
| 130 | The command was interrupted. Existing evidence is retained. |

Old capture and field clocks remain unchanged when reused. A report generated
today does not make an old price fresh. Asking prices are not completed-sale prices.

## Resume without changing the search

```sh
car-market research --workspace ./corolla-nl --resume
```

With Docker, retain the same project name:

```sh
docker compose -p corolla-nl run --rm market research --resume
```

`--resume` uses the saved selector, country, URLs and budgets. Do not repeat search
options with it; use a new workspace for another selector. Existing database
bindings and source cache/backoff rules are checked before collection. Resume is
an explicit manual pass, not an automatic retry loop.

If only reporting failed, fix the report dependency or filesystem error and use
the existing report command without collecting again:

```sh
car-market --workspace ./corolla-nl report
```

## Copy Docker results to your computer

The printed `/workspace/...` paths are inside the container's named volume.
`run --rm` removes its temporary container, while the volume retains the results.
Create the service container without starting collection, then copy the reports
and receipts to a new host directory:

```sh
docker compose -p corolla-nl create --no-recreate market
docker compose -p corolla-nl cp market:/workspace/reports ./corolla-nl-reports
docker compose -p corolla-nl cp market:/workspace/data/research-receipts ./corolla-nl-receipts
```

The receipt gives the exact report directory. Copy exports separately if needed.
Full exports remain the default. Explicit `--export-mode compact` uses
`exports/compact`, retains complete SQLite/history and leaves old full exports
with their old dates; see [compact exports](compact-export.md).

The `init`, `collect`, `report`, `export`, `status`, `coverage` and `quality`
commands remain available for scripts and advanced configuration. They print a
summary in a terminal and JSON with `--json` or when the output is redirected.
Automatic RDW lookup and personal notifications remain outside the generic flow.

The shorter host launchers forward the same command and can copy reports with
`./start.sh reports` or `.\start.ps1 reports`. For a development build, set
`CAR_MARKET_IMAGE=car-market:local` in the same terminal and use
`docker compose -f compose.yaml -f compose.build.yaml build`; the main Compose
file selects a ready-built image.
