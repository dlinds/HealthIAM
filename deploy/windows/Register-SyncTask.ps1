<#
.SYNOPSIS
    Registers a nightly directory sync as a Scheduled Task.

.DESCRIPTION
    The Windows counterpart of the TrueNAS cron jobs in docs/deploy-truenas.md. Runs
    `manage.py sync_ad`, or `manage.py sync_entra` with -Command sync_entra; either
    exits non-zero when the run failed or any row had an error -- so the task's Last
    Run Result is the signal that something needs attention. There is no cron mail
    here, so the run's own output goes to its own log file and every run is also
    listed under Admin > Active Directory or Admin > Entra ID.

    Runs as SYSTEM: a Scheduled Task cannot use the service's virtual account, and
    SYSTEM can read the .env the installer locked down. Nothing authenticates to the
    network as this identity -- the LDAPS bind uses AD_BIND_DN from .env, and the
    Graph sync its own application credential.

.EXAMPLE
    .\Register-SyncTask.ps1 -InstallRoot C:\HealthIAM -At 02:00

.EXAMPLE
    .\Register-SyncTask.ps1 -InstallRoot C:\HealthIAM -Command sync_entra -At 02:30
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\HealthIAM',
    [ValidateSet('sync_ad', 'sync_entra')]
    [string]$Command = 'sync_ad',
    # Defaults to "HealthIAM AD sync" or "HealthIAM Entra sync", the names the installer
    # writes into SYNC_SCHEDULE_COMMAND and ENTRA_SYNC_SCHEDULE_COMMAND.
    [string]$TaskName = '',
    # Defaults to 02:00 for sync_ad and 02:30 for sync_entra, so the two do not start together.
    [datetime]$At,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$isEntra = $Command -eq 'sync_entra'
if (-not $PSBoundParameters.ContainsKey('At')) {
    $At = if ($isEntra) { '02:30' } else { '02:00' }
}
if (-not $TaskName) {
    $TaskName = if ($isEntra) { 'HealthIAM Entra sync' } else { 'HealthIAM AD sync' }
}
$adminPage = if ($isEntra) { 'Admin > Entra ID' } else { 'Admin > Active Directory' }

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

$venvPython = Join-Path $InstallRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) { throw "No virtual environment at $venvPython. Run Install-HealthIAM.ps1 first." }

$logDir = Join-Path $InstallRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# The sync gets its own log file, separate from the service's. Two processes rotating
# one RotatingFileHandler on Windows collide over the rename and the loser raises.
$wrapper = Join-Path $InstallRoot 'deploy\windows\Invoke-Sync.ps1'
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$wrapper`" -InstallRoot `"$InstallRoot`" -Command $Command" `
    -WorkingDirectory $InstallRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $At
$principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Registered '$TaskName', daily at $($At.ToString('HH:mm'))." -ForegroundColor Green
Write-Host @"

Do the first sync from $adminPage (Preview, then Apply) before trusting
the schedule. To run the task by hand:

    schtasks /Run /TN "$TaskName"

Last Run Result 0 is a clean run. Anything else means the run failed or a row had an
error; the detail is in $logDir\$Command.log and under $adminPage > Sync runs.
"@ -ForegroundColor DarkGray
