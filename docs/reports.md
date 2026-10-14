# English reports

Reports use an editorial layout: a light background, terracotta for NL, green
for DE, prominent prices, two columns, charts, comparable groups and sample
sizes. All labels, explanations and summaries are in English. Lato is bundled,
so reports do not depend on operating-system fonts. Font source and licenses:
[Google Fonts](https://github.com/google/fonts/tree/main/ofl/lato).

## One market

```sh
docker compose -p corolla-nl run --rm market report
docker compose -p corolla-nl create --no-recreate market
docker compose -p corolla-nl cp market:/workspace/reports ./reports
```

The generator reads the search's current database. You can also specify a
source SQLite file explicitly:
`report --db /workspace/another-snapshot.sqlite3 --country NL`.
The collector does not need to run to generate a report.
`create --no-recreate` ensures a service container exists for `cp`, without
starting a stopped container or replacing an existing one. Data and one-off
command results are already saved in the volume. The `-p corolla-nl` value must
match your search's project name. Run Compose commands from the directory
containing `compose.yaml`.

## Compare NL and DE

Both countries must search for the same model with the same selector. With the
launchers, one command does it: `./start.sh compare corolla-de` (or
`.\start.ps1 compare corolla-de`) mounts the other project's data volume in a
temporary container and runs `report --compare-workspace` against it. See the
[README](../README.md#compare-the-netherlands-with-germany).

Without the launchers, pass a second accessible workspace through
`--compare-workspace`, or its SQLite snapshot through `--compare-db`. In plain
Docker Compose, copying a snapshot looks like this:

```sh
docker compose -p de-search run --rm market export
docker compose -p de-search create --no-recreate market
docker compose -p corolla-nl create --no-recreate market
docker compose -p de-search cp market:/workspace/exports/market.sqlite3 ./de-snapshot.sqlite3
docker compose -p corolla-nl cp ./de-snapshot.sqlite3 market:/workspace/de-snapshot.sqlite3
docker compose -p corolla-nl run --rm market report --compare-db /workspace/de-snapshot.sqlite3
docker compose -p corolla-nl cp market:/workspace/reports ./reports
```

`create --no-recreate` does not start the daemon; existing containers from
`up -d` or `stop` also work. The report runs in a temporary container and does
not stop the daemon. `report --title "Toyota Corolla"` sets the display title
without changing the compatibility checks for the two databases.

Existing BMW/Mercedes research databases can be read through ordinary read-only
paths. Replace these example paths with your database locations:

```sh
uv run car-market report --db /path/nl.sqlite3 --compare-db /path/de.sqlite3 --output ./reports
```

## Output files

Each run creates a new directory named with a UTC timestamp. Previous reports
are preserved.

- `comparison-editorial.png` — a 2800×4680 overview poster.
- `comparison-editorial.pdf` — the same overview with vector text.
- `comparison-editorial-details.pdf` — larger panels: five charts, comparable
  groups and source pages; usually eight pages for NL/DE.
- `detail-*.png` — individual panels for sharing or publishing.
- `overview.txt` — a short English summary with limitations.
- `analysis.json`, `*-cars.csv`, `*-exclusions.csv`, `comparable-groups.csv`
  — exact figures, selected publications, links and exclusion reasons.
- `*-snapshot.sqlite3`, `manifest.json` — consistent source snapshots and a
  SHA-256 hash for each output file.

Snapshots include history and can be large. Visual analysis reads current
observations; original field-verification timestamps stay unchanged. The
report does not modify the working database, refresh old prices or send files.
Seeing a listing again does not make an inherited price or other field fresh;
analysis uses the original field observation evidence. Missing, invalid or
future confirmation times cannot establish freshness.
Keep the entire directory for an archive. PDF/PNG files are sufficient for
sharing the illustrations.

## Calculation rules

Each existing resolved ID contributes one price: its lowest eligible full
purchase price. Bids, deposits, monthly payments, conflicts, price warnings,
listings from another or unverified country, and withdrawn publications do not
qualify. An explicit `sold`/`reserved` status on one peer publication puts the
others on hold until their own later confirmation of `available`. An
incompatible RDW brand/model hold does not become strong identity evidence.

A catalog ID without a plate or VIN remains provisional; it is not guaranteed
to represent a unique physical vehicle. Countries are not merged by UUID.
Matching full VINs are counted separately in `vin_overlap_count`.

Comparable groups require the same published year, transmission and mileage
band, with no declared damage or defects. By default, each country needs at
least five prices. Groups are ranked by the smaller of their two country sample
sizes, highest first; the headline group is the best supported, rather than the
one with the largest apparent discount. The complete group list is saved in
CSV. Generation, trim, seller and actual condition are
not matched. German asking prices exclude import costs. They do not establish
promised savings, profit or a future resale price.

Unknown year, mileage and transmission stay unknown. Year shares use all IDs
with eligible prices as their denominator; unknown years are shown separately.
Transmission shares use all current IDs, including those without an eligible
price. Source volumes count saved publications. Neither these volumes nor
`query_enumeration_percent` in `coverage` measure unique physical vehicles or a
national market; the latter includes non-target and unconfirmed-country cards
against the exact source query's counter. A report from a 15-page default pass
is a bounded sample. An expanded pass can still be constrained by source caps
and repeated promoted placements. See [reliability.md](reliability.md) for the
collection method and limits.

## Settings

```sh
docker compose -p corolla-nl run --rm market report --freshness-hours 48 --min-cohort-size 10 --mileage-bin-km 25000
```

Defaults are a 13-hour freshness window, 50,000 km mileage bands and prices
strictly above €1 with no upper limit. `--price-floor` and `--price-ceiling`
set explicit limits for analysis, not collection. Settings are saved in
`analysis.json`. The BMW upper limit from an earlier one-off analysis does not
apply to other vehicles.

`--as-of` analyzes an already frozen snapshot at a specified time, including
its timezone. It does not reconstruct a past market from a later database:
current rows after that time are not replaced with historical rows.

An empty database or lack of comparable groups produces a valid report with
explanations, without invented medians. A preparation failure leaves the
previous published generation intact. A local installation needs
`uv sync --extra reports`.

For a downloaded wheel without a source checkout, install the extra from the
same file in the environment where `car-market` is installed; no public PyPI
package is required:

```sh
python -m pip install "/path/to/car_market-0.3.0rc1-py3-none-any.whl[reports]"
```

Replace the path with your wheel's location. For browser support, use the
`[browser]` extra, then run `python -m playwright install chromium`. Docker
already includes both extras and the browser.
