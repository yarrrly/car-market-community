# Native PowerShell argv/exit/cleanup controls. Docker is replaced at its command boundary.
$ErrorActionPreference = 'Stop'
$releaseFixture = Join-Path ([IO.Path]::GetTempPath()) ('car-market-launcher-' + [Guid]::NewGuid().ToString('N'))
$releaseDownload = Join-Path $releaseFixture 'download with spaces'
[void](New-Item -ItemType Directory -Path $releaseDownload)
Copy-Item (Join-Path $PSScriptRoot '../start.ps1') $releaseDownload
Copy-Item (Join-Path $PSScriptRoot '../compose.yaml') $releaseDownload
$global:releaseCalls = [Collections.Generic.List[object]]::new()
$global:releaseRunExit = 0
$global:releaseCopyExit = 0
$env:CAR_MARKET_PROJECT = 'corolla-nl'
function global:docker {
    $values = @($args | ForEach-Object { [string]$_ })
    $global:releaseCalls.Add($values)
    $global:LASTEXITCODE = 0
    if ($values[0] -eq 'cp') {
        $global:LASTEXITCODE = $global:releaseCopyExit
        if ($global:LASTEXITCODE -eq 0) {
            [IO.File]::WriteAllText((Join-Path $values[2] 'report.pdf'), 'copied report')
        }
    } elseif ($values -contains 'run' -and -not ($values -contains '--name')) {
        $global:LASTEXITCODE = $global:releaseRunExit
    }
}
function Assert-Release($condition, $message) {
    if (-not $condition) { throw $message }
}
try {
    & (Join-Path $releaseDownload 'start.ps1')
    Assert-Release ($LASTEXITCODE -eq 0) 'No-argument help failed'
    $command = $global:releaseCalls[$global:releaseCalls.Count - 1]
    Assert-Release ($command[-1] -eq '--help') 'No arguments must show help'
    Assert-Release (-not ($command -contains 'daemon')) 'Help must not start a monitor'
    Assert-Release ($command[4] -eq (Join-Path $releaseDownload 'compose.yaml')) 'Path with spaces was changed'

    $global:releaseCalls.Clear()
    & (Join-Path $releaseDownload 'start.ps1') coverage
    Assert-Release ($LASTEXITCODE -eq 0) 'Single command failed'
    $command = $global:releaseCalls[$global:releaseCalls.Count - 1]
    Assert-Release ($command[-1] -eq 'coverage' -and $command.Count -eq 10) 'A single command was split into characters'

    $global:releaseCalls.Clear()
    $global:releaseRunExit = 3
    $arguments = @('research', '--make', 'Land Rover', '--model', 'Range Rover', '--country', 'NL', '--url', 'https://example.invalid/search?q=a%20b&sort=age')
    & (Join-Path $releaseDownload 'start.ps1') @arguments
    Assert-Release ($LASTEXITCODE -eq 3) 'Partial exit code was hidden'
    $command = $global:releaseCalls[$global:releaseCalls.Count - 1]
    $forwarded = @($command[($command.Count - $arguments.Count)..($command.Count - 1)])
    Assert-Release (($forwarded | ConvertTo-Json -Compress) -eq ($arguments | ConvertTo-Json -Compress)) 'Search arguments were changed'

    $global:releaseCalls.Clear()
    $destination = Join-Path $releaseFixture 'my reports'
    & (Join-Path $releaseDownload 'start.ps1') reports $destination
    Assert-Release ($LASTEXITCODE -eq 0) 'Report copy failed'
    Assert-Release ((Get-Content (Join-Path $destination 'report.pdf') -Raw) -eq 'copied report') 'Report bytes were not copied'
    $copy = $global:releaseCalls[2]
    $remove = $global:releaseCalls[3]
    Assert-Release ($copy[0] -eq 'cp' -and $remove[0] -eq 'rm') 'Only own report container should be removed'
    Assert-Release ($copy[1] -eq ($remove[1] + ':/workspace/reports/.')) 'Cleanup selected a different container'
    Assert-Release ($remove[1].StartsWith('car-market-report-copy-')) 'Unowned container selected'

    $global:releaseCalls.Clear()
    & (Join-Path $releaseDownload 'start.ps1') reports $destination
    Assert-Release ($LASTEXITCODE -eq 2) 'Existing directory must refuse'
    Assert-Release ($global:releaseCalls.Count -eq 1) 'Existing output started a copy container'
    Assert-Release ((Get-Content (Join-Path $destination 'report.pdf') -Raw) -eq 'copied report') 'Existing output changed'

    $global:releaseCalls.Clear()
    $global:releaseCopyExit = 17
    & (Join-Path $releaseDownload 'start.ps1') reports (Join-Path $releaseFixture 'failed copy')
    Assert-Release ($LASTEXITCODE -eq 17) 'Copy failure became success'
    Assert-Release ($global:releaseCalls[$global:releaseCalls.Count - 1][0] -eq 'rm') 'Failed copy did not remove own container'
    $global:releaseCalls.Clear()
    $global:releaseRunExit = 0
    & (Join-Path $releaseDownload 'start.ps1') compare corolla-de --title 'Toyota Corolla'
    Assert-Release ($LASTEXITCODE -eq 0) 'Compare failed'
    Assert-Release (@($global:releaseCalls | Where-Object { ($_ -join ' ') -eq 'volume inspect corolla-de_market-data' }).Count -eq 1) 'Compare did not check the other search'
    $command = $global:releaseCalls[$global:releaseCalls.Count - 1]
    Assert-Release ($command[$command.IndexOf('--volume') + 1] -eq 'corolla-de_market-data:/compare') 'Compare mounted the wrong search'
    $tail = @($command[($command.Count - 5)..($command.Count - 1)])
    Assert-Release (($tail -join '|') -eq 'report|--compare-workspace|/compare|--title|Toyota Corolla') 'Compare changed report options'

    $global:releaseCalls.Clear()
    & (Join-Path $releaseDownload 'start.ps1') compare corolla-nl
    Assert-Release ($LASTEXITCODE -eq 2) 'Comparing a search with itself must refuse'
    Assert-Release (@($global:releaseCalls | Where-Object { $_ -contains 'run' }).Count -eq 0) 'Refused compare started a container'
    Write-Output 'PASS: eight native PowerShell launcher controls; mocked Docker only, no Docker Desktop certificate.'
} finally {
    Remove-Item Function:/docker
    # This script owns only this unique temporary fixture, never a real Docker volume.
    if ($releaseFixture -and (Split-Path $releaseFixture -Leaf).StartsWith('car-market-launcher-')) {
        Remove-Item -LiteralPath $releaseFixture -Recurse
    }
}
exit 0
