<#
.SYNOPSIS
    Runs a directory sync for the Scheduled Task, preserving its exit code.

.DESCRIPTION
    A thin wrapper so Task Scheduler's Last Run Result means what it should. It runs
    `manage.py sync_ad`, or `manage.py sync_entra` with -Command sync_entra, tees the
    run's output to logs\<command>.log -- stdout is the summary, stderr the per-row
    errors -- and exits with the command's own code, which is non-zero when the run
    failed or any row had an error.

    LOG_FILE is overridden so the sync does not share the service's rotating log: two
    processes rolling the same file over on Windows fight over the rename. Each command
    gets its own, so the two syncs never share one either.

.EXAMPLE
    .\Invoke-Sync.ps1 -InstallRoot C:\HealthIAM --dry-run

.EXAMPLE
    .\Invoke-Sync.ps1 -InstallRoot C:\HealthIAM -Command sync_entra --groups-only
#>
# Not positional: an unnamed argument such as --dry-run must land in $SyncArgs, never
# in -Command.
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$InstallRoot = 'C:\HealthIAM',
    [ValidateSet('sync_ad', 'sync_entra')]
    [string]$Command = 'sync_ad',
    # Passed straight through, e.g. --dry-run, --users-only, --groups-only.
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SyncArgs = @()
)

$ErrorActionPreference = 'Continue'

$venvPython = Join-Path $InstallRoot '.venv\Scripts\python.exe'
$log = Join-Path $InstallRoot "logs\$Command.log"

$env:DJANGO_SETTINGS_MODULE = 'config.settings.prod'
$env:LOG_FILE = Join-Path $InstallRoot "logs\$Command.django.log"

Set-Location $InstallRoot
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
"[$stamp] $Command $($SyncArgs -join ' ')" | Add-Content -Path $log -Encoding UTF8

& $venvPython manage.py $Command @SyncArgs 2>&1 | Tee-Object -FilePath $log -Append
$code = $LASTEXITCODE

"[$stamp] exit $code" | Add-Content -Path $log -Encoding UTF8
exit $code
