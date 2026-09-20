<#
.SYNOPSIS
    Runs `manage.py sync_ad` for the Scheduled Task, preserving its exit code.

.DESCRIPTION
    A thin wrapper so Task Scheduler's Last Run Result means what it should. It tees the
    run's output to logs\sync_ad.log -- stdout is the summary, stderr the per-row errors
    -- and exits with the command's own code, which is non-zero when the run failed or
    any row had an error.

    LOG_FILE is overridden so the sync does not share the service's rotating log: two
    processes rolling the same file over on Windows fight over the rename.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\HealthIAM',
    # Passed straight through, e.g. --dry-run, --users-only, --groups-only.
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SyncArgs = @()
)

$ErrorActionPreference = 'Continue'

$venvPython = Join-Path $InstallRoot '.venv\Scripts\python.exe'
$log = Join-Path $InstallRoot 'logs\sync_ad.log'

$env:DJANGO_SETTINGS_MODULE = 'config.settings.prod'
$env:LOG_FILE = Join-Path $InstallRoot 'logs\sync_ad.django.log'

Set-Location $InstallRoot
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
"[$stamp] sync_ad $($SyncArgs -join ' ')" | Add-Content -Path $log -Encoding UTF8

& $venvPython manage.py sync_ad @SyncArgs 2>&1 | Tee-Object -FilePath $log -Append
$code = $LASTEXITCODE

"[$stamp] exit $code" | Add-Content -Path $log -Encoding UTF8
exit $code
