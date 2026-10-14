# Explicit compact exports

Full export remains the default: the same twelve CSV/JSON/SQLite files and
existing positional `Database.export(destination)` result. No configuration,
collection cadence or selected source/model changes automatically.

Compact export is an explicit operator choice. It writes
`exports/compact/` with the complete SQLite snapshot and nine CSVs, omitting
only `price-history.csv` and `vehicles.json`. It adds `export-manifest.json`:
ten data files plus one manifest. Every observation, event, identity, alias,
check and field verification remains in SQLite. Working SQLite and raw response
archives stay in their original locations. Raw files are not bundled into either
export format; raw references in a snapshot still need their original archives.

The portable entry point accepts the option after the command:

```sh
car-market --workspace ./workspace export --export-mode compact
car-market --workspace ./workspace collect --export-mode compact
car-market --workspace ./workspace daemon --export-mode compact
```

The legacy BMW/Mercedes entry point accepts it before the command:

```sh
python -m car_market.cli --db /path/data/market.sqlite3 \
  --config /path/config.json --export-dir /path/exports \
  --export-mode compact export
```

The library call is `db.export(destination, mode="compact")`. The destination
is the existing export root; compact files go into its `compact` child. Country,
profile, working-database alias and committed-transaction protections apply to
both the root and compact snapshot. The compact directory must resolve to the
root's separate compact child. Directory aliases and selected files that alias
any retained full output by resolved path or inode are refused before staging
and again before publication. Existing writer locks remain required; these
checks do not promise protection against an external concurrent namespace swap.

A compact result includes `export_artifacts`: actual format, resolved directory,
database binding, latest snapshot run, capture/as-of clock, deliberately omitted
names, and exact payload names/formats/byte sizes. The same receipt is saved in
the compact manifest. This inventory is not a payload checksum or an atomic
whole-directory generation guarantee. Files publish through the existing
per-file replacement/rollback helper, with the manifest last. A caught error
restores the previous selected generation; failed rollback retains recovery
links and the original errors. Process termination is not crash recovery.

Publication diagnostics separate collection mode (`full` or `skipped_fast`)
from export format (`full` or `compact`). `last_successful_export` identifies
the most recent successful publication in either format.
`last_successful_full_export` refers only to the full file format; a compact
success cannot describe retained flat full files as updated. `export_format`,
`export_directory` and `export_snapshot_at` expose the actual receipt. Older
valid metadata remains readable; missing legacy path/snapshot fields stay
unknown. Invalid, foreign or future compact receipts are unresolved.

Fast passes intentionally skip exporting. They keep the last successful
receipt and its original clock. Manual export returns its own artifact receipt;
it does not create a collection/publication attempt or rewrite a previously
failed attempt as successful. Collection status/counts/finished_at and original
fetch/observation/field-verification clocks never become export clocks.

Portable reports read SQLite, not either omitted file. For example:

```sh
car-market report --db /path/exports/compact/market.sqlite3 --country NL
```

The legacy `import-db` command also consumes the compact SQLite snapshot with
its existing country/profile/raw-path checks. It does not add a configurable
profile importer. Reports create their own complete snapshots and can consume
additional space. Use full export for external consumers of history CSV or rich
vehicle JSON; those remain supported full-format interfaces.

First adoption keeps the old full files and adds a compact generation, so it
can increase retained disk usage. There is no automatic deletion, compression
or migration. Subsequent compact preparation omits duplicated history CSV and
rich JSON bytes, but still creates a whole SQLite snapshot and materializes the
existing vehicle catalog for `vehicles.csv`. SQLite sorting, concurrent raw/DB/
WAL growth and other writers still need space. Successful export establishes
publication of stored data, not source freshness or national market coverage.
