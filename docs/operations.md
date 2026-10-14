# Operations

Each search has its own workspace and SQLite database. Place `--workspace` and
`--config` before the command. Docker sets `/workspace` automatically.
The examples use the project name `corolla-nl`. For an existing installation,
use its original name in every `-p` argument to select the correct volume.
Run Compose commands from the directory containing `compose.yaml`.

## Workspace files

| Path relative to the workspace | Contents |
|---|---|
| `search.json` | Validated configuration |
| `data/market.sqlite3` | Publications, history, IDs, aliases, checks |
| `data/raw/` | Saved responses, gzip-compressed with verifiable SHA-256 hashes |
| `data/last-run.json` | Last saved run summary |
| `data/heartbeat.json` | Timestamped process state |
| `data/daemon.lock` | Lock preventing a second daemon |
| `data/market.sqlite3.lock` | Lock for all commands that write data |
| `exports/` | CSV, JSON and a consistent SQLite snapshot |

## Diagnostics

`status` reads the database without creating one. `doctor --require-daemon`
checks SQLite integrity, foreign keys, the heartbeat and write permissions on
workspace files. After a failed run, the heartbeat shows `error` until the next
attempt or successful run. Passing diagnostics does not prove that market data
is complete or fresh. Check `coverage`, `quality`, source timestamps and the
discovery queue in `status`/`discoveries`. Query enumeration includes non-target
and country-unconfirmed publication IDs; it cannot measure national inventory.
Field freshness uses the field's original confirmation time, even if the
listing was seen again recently. Invalid or future timestamps stay unverified.
`quality` checks stored raw-path/hash metadata, not the actual archived bytes.
It reports invalid calendar years separately from field presence; a filled year
can still be a parsing error. Historical values are not rewritten by this check.
Schema version 6 indexes source/query health by the original actual timestamp.
Schema version 7 adds a time-leading partial index for recent HTTP 403/429
across sources, so detail backoff does not scan expired refusals. Writable
commands apply migrations under the workspace lock; read-only diagnostics leave
older databases unchanged. Neither migration rewrites historical observations.

Eligible unverified listing details are fetched in bounded batches. Gaspedaal
discoveries remain queued without automatic fragment or outbound-redirect
requests; there is no accepted own detail contract. AutoTrack cards with
unconfirmed country need the publication's own detail evidence. An invalid or
unrelated listing does not create a vehicle ID; the attempt timestamp persists
across restarts.

Exit codes: 0 — success; 2 — invalid configuration or command error;
3 — partial collection; 4 — writer or daemon busy; 5 — unhealthy diagnostics;
130 — interrupted one-off run.

After a crash, the next run marks the previous unfinished run as `interrupted`;
observations already committed remain intact. The daemon catches ordinary run
errors and waits for the next scheduled slot. SIGTERM stops the loop and
prevents further requests; the current request is bounded by its timeout.
Compose restarts an exited container, and the healthcheck reports a stale
heartbeat. An unhealthy status alone does not trigger a Docker restart.

For HTTP 403/429, do not increase request frequency or try to solve CAPTCHAs in
code. The scheduled interval and backoff apply after a refusal; collection
continues for sources that still respond. Partial results do not mean that
older listings have sold.

For a bounded or expanded search-only manual pass, stop this search's daemon
and use `collect --browser --no-details`. Set the explicit page budget with
`init --max-pages` for a new search, or edit `max_pages` in `search.json` for
an existing one, then `validate`. A query's own `max_pages` takes precedence
over the global value; update any overrides when changing the budget.
The range is 1–200; `collect` itself has no page-budget option. These saved
settings also apply to the daemon. Increasing the budget cannot bypass a source's page cap.
See [reliability.md](reliability.md) for complete commands and how to inspect
the resulting query, country and detail limitations.

## Backup

```sh
docker compose -p corolla-nl run --rm market backup --output /workspace/backups/market-20261010.sqlite3
docker compose -p corolla-nl create --no-recreate market
docker compose -p corolla-nl cp market:/workspace/backups ./backups
docker compose -p corolla-nl stop
docker compose -p corolla-nl cp market:/workspace/data/raw ./raw-backup
docker compose -p corolla-nl cp market:/workspace/search.json ./search-backup.json
docker compose -p corolla-nl up -d
```

The SQLite backup API creates a consistent snapshot, including committed WAL
entries. The command runs `integrity_check` and refuses to overwrite an existing
file. Use a new backup filename for each run. For a complete archive of the
source evidence, keep the database, `raw/` and configuration together. Copying
only an open `market.sqlite3` with an ordinary file copy can lose WAL entries.

## Restore

Stop only this search's container. Save the entire existing `data/` directory.
Restore the selected snapshot as `data/market.sqlite3`, together with its
matching `raw/` and `search.json`. Do not carry old `-wal`/`-shm` files over to a
different database. Run `validate` and `doctor`, then `up -d`. Never restore one
model over another model's database. An older restored snapshot remains old:
its timestamps are not replaced with the current time.

Copying files from the host into the container may leave them owned by root;
the collector runs as UID 10001. After restoring, while the daemon is stopped,
fix ownership and permissions inside this search's volume, then check:

```sh
docker compose -p corolla-nl create --no-recreate market
docker compose -p corolla-nl run --rm --user 0 --entrypoint chown market -R 10001:10001 /workspace/data /workspace/search.json
docker compose -p corolla-nl run --rm --user 0 --entrypoint chmod market -R u+rwX /workspace/data
docker compose -p corolla-nl run --rm market validate
docker compose -p corolla-nl run --rm market doctor
docker compose -p corolla-nl up -d
```

Do not run `up -d` while `doctor` returns a nonzero exit code or any
`write_permissions` value is false. Other containers and volumes are unaffected
by these permission commands.

## Upgrade

First back up the database, raw responses and configuration as described above.
Record the original project name with `docker compose ls -a` and use the same
`-p` value in every subsequent command.

For a Git installation, run `git pull --ff-only`, then update the image and start as
shown below. For a ZIP installation, download the new release, extract it into a new directory and open that directory. Git is
not required for ZIP installations. A different directory name is safe when you explicitly
use the original `-p` value. Copy any custom `.env` files or Compose overrides
separately.

The `pull` step below applies to a published registry image. For an offline
candidate, load the new supplied archive with `docker load --input ARCHIVE` and
set `CAR_MARKET_IMAGE` to its loaded reference instead of pulling. For a source
development build, use `compose.yaml` plus `compose.build.yaml` and the same local
image override shown in [Advanced usage](advanced.md). Keep the original project
name in every case.

```sh
docker compose -p corolla-nl stop
docker compose -p corolla-nl pull
docker compose -p corolla-nl up -d
docker compose -p corolla-nl run --rm market doctor --require-daemon
docker compose -p corolla-nl run --rm market coverage
```

Do not run `init` during an upgrade or use `down -v`. Check the export after the
first run. Schema migrations happen on writes; reading status does not trigger
a migration.

Independent installations can keep their configuration outside Git. The
repository ignores workspaces, SQLite files, `.env` and logs. Container logs
are limited to three files of 10 MB each; raw responses and the database need
separate disk-space monitoring. Archive them according to your retention
policy. Do not delete raw responses referenced by history without preserving
them in a matching archive.
