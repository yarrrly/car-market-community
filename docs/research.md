# First research run: from ZIP to an English report

Download the project (`git clone` or GitHub's **Code → Download ZIP**), extract
it and open the directory containing `compose.yaml`. Docker Compose is
required; on Windows, select Linux containers. The main Compose file uses the
published image; to build it yourself, see [Install](release-preview.md#build-the-image-yourself).
This page uses plain Docker Compose commands. The
[README](../README.md#compare-the-netherlands-with-germany) shows the shorter
launcher form of the same workflow.

This example searches for Toyota Corolla hybrid automatics from 2019 onward in
NL and DE. For a different vehicle, change the same selector parameters for
both countries and use real search-result URLs from the source. The names
`corolla-hybrid-nl`/`corolla-hybrid-de` must be new; an upgrade does not need
another `init`. Arbitrary selectors do not establish support for every site or
complete national coverage. The two searches below produce bounded samples.

```sh
docker compose -p corolla-hybrid-nl create --no-recreate market
docker compose -p corolla-hybrid-nl run --rm market init --make Toyota --model Corolla --fuel hybrid --transmission automatic --year-from 2019 --max-pages 2 --detail-limit 8 --country NL --url "https://www.autoscout24.nl/lst/toyota/corolla?cy=NL&atype=C&sort=age&desc=1"
docker compose -p corolla-hybrid-nl run --rm market validate
docker compose -p corolla-hybrid-nl run --rm market collect --browser
docker compose -p corolla-hybrid-nl run --rm market list
docker compose -p corolla-hybrid-nl run --rm market coverage
```

`collect` may exit with code 3: results are saved, but collection is partial.
The example is limited to two pages and eight detail requests per country.
Check received/counter, `page_limit_exceeded`, detail errors and field
timestamps. Code 2 indicates an error that needs fixing. A one-off collection
already creates exports in the volume.

The second country uses a separate volume and the same image:

```sh
docker compose -p corolla-hybrid-de create --no-recreate market
docker compose -p corolla-hybrid-de run --rm market init --make Toyota --model Corolla --fuel hybrid --transmission automatic --year-from 2019 --max-pages 2 --detail-limit 8 --country DE --url "https://www.autoscout24.de/lst/toyota/corolla?cy=D&atype=C&sort=age&desc=1"
docker compose -p corolla-hybrid-de run --rm market validate
docker compose -p corolla-hybrid-de run --rm market collect --browser
docker compose -p corolla-hybrid-de run --rm market coverage
docker compose -p corolla-hybrid-de run --rm market export
docker compose -p corolla-hybrid-de cp market:/workspace/exports/market.sqlite3 ./de-snapshot.sqlite3
```

The tool's country code is `DE`; the German URL parameter is **`cy=D`**. An
incorrect URL may return a genuinely empty result set. `validate` checks known
configuration errors but does not prove that the source's filters express your
intended search. Check the URL in a browser; a successful run with no results
does not prove the market is empty.

For search-only checks before fetching details, use
`collect --browser --no-details` with the saved two-page budget. The
[reliability guide](reliability.md) also shows expanded manual passes and
initialization from copied AutoScout24 and Marktplaats API URLs. Query
percentages describe source publication IDs, including non-target or
unconfirmed cards; they do not measure the share of Corollas or vehicles in
either country.

Comparison is an option of `report`; there is no separate `compare` command:

```sh
docker compose -p corolla-hybrid-nl cp ./de-snapshot.sqlite3 market:/workspace/de-snapshot.sqlite3
docker compose -p corolla-hybrid-nl run --rm market report --compare-db /workspace/de-snapshot.sqlite3 --title "Toyota Corolla hybrid automatic 2019+"
docker compose -p corolla-hybrid-nl cp market:/workspace/reports ./reports
```

Open `comparison-editorial.pdf` and `comparison-editorial-details.pdf` in the
new UTC directory. It also contains PNG, CSV, `analysis.json`, source snapshots
and a manifest. If a group has fewer than five prices in either country, the
report explains this. The overall median difference does not imply import
savings.

The DE snapshot is frozen. For the next fresh comparison, stop both daemons
before manual collection, collect both searches again, then repeat the DE
`export`, `cp` and NL `report` steps. Exporting or reporting alone does not
refresh observations. Previous reports are not overwritten. See
[reports.md](reports.md) for details. Page/detail budgets and completeness
conditions are described in [reliability.md](reliability.md). The report retains
original field observation times; recent listing presence alone cannot renew
a price confirmation.

To monitor after the first research run, execute
`docker compose -p corolla-hybrid-nl up -d` and the equivalent for DE. To stop,
use `stop` with the same project name; data remains intact. Check
`doctor --require-daemon`. See [operations.md](operations.md) for backup,
restore and ZIP upgrades. Avoid `down -v` to keep the database.
