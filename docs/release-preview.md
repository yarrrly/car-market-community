# Install and run

Car Market runs in Docker. The launchers `start.sh` (macOS, Linux) and
`start.ps1` (Windows PowerShell) wrap Docker Compose so that you only type the
Car Market command.

## Use the published image

`compose.yaml` selects `ghcr.io/yarrrly/car-market:0.3.0rc1`. Docker downloads
it on the first command; it is about 1.7 GB unpacked and contains Python, the bundled
Chromium browser and the report fonts. Both `linux/amd64` and `linux/arm64`
(Apple Silicon, ARM servers) are published.

```sh
./start.sh research --make Toyota --model Corolla --country NL
./start.sh reports
```

Running a launcher without arguments shows the command list.

## Build the image yourself

From the folder that contains `compose.yaml`:

```sh
export CAR_MARKET_IMAGE="car-market:local"
docker compose -f compose.yaml -f compose.build.yaml build
./start.sh research --make Toyota --model Corolla --country NL
```

PowerShell:

```powershell
$env:CAR_MARKET_IMAGE = "car-market:local"
docker compose -f compose.yaml -f compose.build.yaml build
.\start.ps1 research --make Toyota --model Corolla --country NL
```

Keep `CAR_MARKET_IMAGE` set in the terminal you use for later commands, or the
launcher falls back to the published image.

## Keep searches separate

The launcher uses the Compose project `car-market` unless `CAR_MARKET_PROJECT`
says otherwise. The project name selects the Docker volume that holds the
search, so keep it the same for every command about one search:

```sh
export CAR_MARKET_PROJECT="corolla-nl"
./start.sh research --make Toyota --model Corolla --country NL
./start.sh research --resume
./start.sh reports ./corolla-nl-report
```

PowerShell:

```powershell
$env:CAR_MARKET_PROJECT = "corolla-nl"
.\start.ps1 research --make Toyota --model Corolla --country NL
.\start.ps1 research --resume
.\start.ps1 reports .\corolla-nl-report
```

`research --resume` keeps the saved make, model, country, URLs and limits, so
leave those options out. A different car or country needs a new project name.
`docker compose ls -a` lists the projects you have created.

## Copy reports

`reports` copies the reports saved in the search's volume to a new folder on
your computer. Without an argument it creates `reports/car-market-<time>-<pid>`
next to the launcher; with an argument it uses that folder, which must not
exist yet. Copying does not start a collection or change saved data.

Research receipts stay in the volume under `/workspace/data/research-receipts`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The pass and the report completed. |
| 3 | Partial collection: page or detail limits reached, or a site refused some pages. Data and report are saved. |
| 2 | Setup, export, report or receipt error. |
| 4 | Another collector is writing this search right now. |
| 130 | Interrupted. |

After a report-only failure, run `./start.sh report` to retry just the report.

## Tested platforms

| Platform | Status |
|---|---|
| Linux AMD64, Docker Engine | Full research, resume, compare and report runs. |
| Linux ARM64, Docker Engine | Image smoke test on a native ARM64 runner. |
| macOS and Linux shells | Launcher tests with a mocked Docker command. |
| Windows PowerShell | Launcher tests with a mocked Docker command. |
| Docker Desktop on Windows or macOS | Not tested yet. |

See the [CHANGELOG](../CHANGELOG.md) for what changed in each version.
