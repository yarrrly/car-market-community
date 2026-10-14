#!/usr/bin/env bash
# Run one command in the installed image; keep the same project for later runs.
set -uo pipefail

release_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
release_project="${CAR_MARKET_PROJECT:-car-market}"
release_compose=(docker compose --project-name "$release_project" --file "$release_root/compose.yaml")

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  printf '%s\n' 'Install and start Docker with Compose v2. Windows users must enable Linux containers.' >&2
  exit 2
fi

if [[ "${1:-}" == compare ]]; then
  if (( $# < 2 )); then
    printf '%s\n' 'Usage: ./start.sh compare OTHER_PROJECT [report options]' >&2
    printf '%s\n' 'Builds one report that compares this search with the search saved in OTHER_PROJECT.' >&2
    exit 2
  fi
  # Compose lower-cases project names, and so its volume names.
  release_other="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"
  shift 2
  if [[ "$release_other" == "$(printf '%s' "$release_project" | tr '[:upper:]' '[:lower:]')" ]]; then
    printf '%s\n' 'Choose the other search: OTHER_PROJECT must differ from CAR_MARKET_PROJECT.' >&2
    exit 2
  fi
  if ! docker volume inspect "${release_other}_market-data" >/dev/null 2>&1; then
    printf 'No saved search named %s. Create it first, for example:\n' "$release_other" >&2
    printf '  CAR_MARKET_PROJECT=%s ./start.sh research --make ... --model ... --country DE\n' "$release_other" >&2
    exit 2
  fi
  "${release_compose[@]}" run --rm --no-deps --volume "${release_other}_market-data:/compare" \
    market report --compare-workspace /compare "$@"
  exit "$?"
fi

if [[ "${1:-}" != reports ]]; then
  if (( $# == 0 )); then
    set -- --help
  fi
  "${release_compose[@]}" run --rm --no-deps market "$@"
  exit "$?"
fi

if (( $# > 2 )); then
  printf '%s\n' 'Usage: ./start.sh reports [new output directory]' >&2
  exit 2
fi
release_destination="${2:-$release_root/reports/car-market-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
if [[ -e "$release_destination" || -L "$release_destination" ]]; then
  printf '%s\n' 'Choose a new output directory; existing reports will not be overwritten.' >&2
  exit 2
fi
if ! mkdir -p -- "$release_destination"; then
  exit 2
fi

release_container="car-market-report-copy-$(date -u +%Y%m%d%H%M%S)-$$-$RANDOM"
release_created=0
release_cleanup() {
  if (( release_created )); then
    docker rm "$release_container" >/dev/null
  fi
}
trap release_cleanup EXIT
"${release_compose[@]}" run --name "$release_container" --no-deps --entrypoint /bin/true market
release_status=$?
if (( release_status != 0 )); then
  exit "$release_status"
fi
release_created=1
docker cp "$release_container:/workspace/reports/." "$release_destination"
release_status=$?
if (( release_status != 0 )); then
  printf '%s\n' 'Report copy failed. Generate a report first and inspect the Docker error above.' >&2
  exit "$release_status"
fi
printf 'Reports copied to %s\n' "$release_destination"
