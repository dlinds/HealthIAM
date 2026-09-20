<#
.SYNOPSIS
    Registers the nightly database backup as a Scheduled Task.

.EXAMPLE
    .\Register-BackupTask.ps1 -InstallRoot C:\HealthIAM -At 01:00 -KeepDays 30
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\HealthIAM',
    [string]$TaskName = 'HealthIAM database backup',
    [datetime]$At = '01:00',
    [int]$KeepDays = 30,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

$script = Join-Path $InstallRoot 'deploy\windows\Backup-Database.ps1'
if (-not (Test-Path $script)) { throw "No Backup-Database.ps1 at $script." }

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$script`" -InstallRoot `"$InstallRoot`" -KeepDays $KeepDays" `
    -WorkingDirectory $InstallRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $At
$principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Registered '$TaskName', daily at $($At.ToString('HH:mm')), keeping $KeepDays days." -ForegroundColor Green
Write-Host 'Copy the dumps off this server; a local-only backup is not a backup.' -ForegroundColor DarkGray
