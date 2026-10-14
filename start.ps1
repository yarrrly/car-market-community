# Run one command in the installed image; keep the same project for later runs.
$ErrorActionPreference = 'Stop'
$releaseRoot = $PSScriptRoot
$releaseProject = if ($env:CAR_MARKET_PROJECT) { $env:CAR_MARKET_PROJECT } else { 'car-market' }
$releaseCompose = @('compose', '--project-name', $releaseProject, '--file', (Join-Path $releaseRoot 'compose.yaml'))

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error 'Install and start Docker Desktop with Linux containers and Compose v2.' -ErrorAction Continue
    exit 2
}
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Error 'Start Docker Desktop with Linux containers and Compose v2.' -ErrorAction Continue
    exit 2
}

if ($args.Count -gt 0 -and $args[0] -eq 'compare') {
    if ($args.Count -lt 2) {
        Write-Error 'Usage: ./start.ps1 compare OTHER_PROJECT [report options]' -ErrorAction Continue
        exit 2
    }
    # Compose lower-cases project names, and so its volume names.
    $releaseOther = ([string]$args[1]).ToLowerInvariant()
    if ($releaseOther -eq $releaseProject.ToLowerInvariant()) {
        Write-Error 'Choose the other search: OTHER_PROJECT must differ from CAR_MARKET_PROJECT.' -ErrorAction Continue
        exit 2
    }
    & docker volume inspect "${releaseOther}_market-data" *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "No saved search named $releaseOther. Create it first with `$env:CAR_MARKET_PROJECT = '$releaseOther' and .\start.ps1 research." -ErrorAction Continue
        exit 2
    }
    [string[]]$releaseExtra = if ($args.Count -gt 2) { $args[2..($args.Count - 1)] } else { @() }
    & docker @releaseCompose run --rm --no-deps --volume "${releaseOther}_market-data:/compare" market report --compare-workspace /compare @releaseExtra
    exit $LASTEXITCODE
}

if ($args.Count -eq 0 -or $args[0] -ne 'reports') {
    [string[]]$releaseArguments = if ($args.Count -eq 0) { @('--help') } else { $args }
    & docker @releaseCompose run --rm --no-deps market @releaseArguments
    exit $LASTEXITCODE
}

if ($args.Count -gt 2) {
    Write-Error 'Usage: ./start.ps1 reports [new output directory]' -ErrorAction Continue
    exit 2
}
$releaseDestination = if ($args.Count -eq 2) { $args[1] } else {
    Join-Path $releaseRoot ('reports/car-market-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + $PID)
}
if (Test-Path -LiteralPath $releaseDestination) {
    Write-Error 'Choose a new output directory; existing reports will not be overwritten.' -ErrorAction Continue
    exit 2
}
[void](New-Item -ItemType Directory -Path $releaseDestination)
$releaseContainer = 'car-market-report-copy-' + [Guid]::NewGuid().ToString('N')
$releaseCreated = $false
$releaseStatus = 1
try {
    & docker @releaseCompose run --name $releaseContainer --no-deps --entrypoint /bin/true market
    $releaseStatus = $LASTEXITCODE
    if ($releaseStatus -eq 0) {
        $releaseCreated = $true
        & docker cp "${releaseContainer}:/workspace/reports/." $releaseDestination
        $releaseStatus = $LASTEXITCODE
        if ($releaseStatus -eq 0) {
            Write-Output "Reports copied to $releaseDestination"
        } else {
            Write-Warning 'Report copy failed. Generate a report first and inspect the Docker error above.'
        }
    }
} finally {
    if ($releaseCreated) {
        & docker rm $releaseContainer *> $null
    }
}
exit $releaseStatus
