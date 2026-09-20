<#
.SYNOPSIS
    Registers the nightly Active Directory sync as a Scheduled Task.

.DESCRIPTION
    The Windows counterpart of the TrueNAS cron job in docs/deploy-truenas.md. Runs
    `manage.py sync_ad`, which exits non-zero when the run failed or any row had an
    error -- so the task's Last Run Result is the signal that something needs attention.
    There is no cron mail here, so the run's own output goes to its own log file and
    every run is also listed under Admin > Active Directory.

    Runs as SYSTEM: a Scheduled Task cannot use the service's virtual account, and
    SYSTEM can read the .env the installer locked down. Nothing authenticates to the
    network as this identity -- the LDAPS bind uses AD_BIND_DN from .env.

.EXAMPLE
    .\Register-SyncTask.ps1 -InstallRoot C:\HealthIAM -At 02:00
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\HealthIAM',
    [string]$TaskName = 'HealthIAM AD sync',
    [datetime]$At = '02:00',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

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
    -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$wrapper`" -InstallRoot `"$InstallRoot`"" `
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

Do the first sync from Admin > Active Directory (Preview, then Apply) before trusting
the schedule. To run the task by hand:

    schtasks /Run /TN "$TaskName"

Last Run Result 0 is a clean run. Anything else means the run failed or a row had an
error; the detail is in $logDir\sync_ad.log and under Admin > Active Directory > Sync runs.
"@ -ForegroundColor DarkGray
