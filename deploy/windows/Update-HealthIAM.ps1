<#
.SYNOPSIS
    Upgrades an installed HealthIAM to a newer checkout.

.DESCRIPTION
    Stops the service, backs up the database, replaces the source, reinstalls
    dependencies, migrates, collects static files and starts the service again.

    On TrueNAS an upgrade is an image tag and a rollback is the previous tag. There is
    no image here, so the two halves of a rollback are the previous source tree and the
    dump this script takes before touching anything. It prints where that dump is.

    .env, media, logs and backups are never touched.

.EXAMPLE
    .\Update-HealthIAM.ps1 -InstallRoot C:\HealthIAM -SourcePath C:\src\HealthIAM
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\HealthIAM',

    # The new checkout. Must not be $InstallRoot itself.
    [Parameter(Mandatory = $true)]
    [string]$SourcePath,

    [string]$ServiceName = 'HealthIAM',

    # Upgrade without a dump first. Only when you have a fresh one from elsewhere.
    [switch]$SkipBackup
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Step { param([string]$Message) Write-Host "`n==> $Message" -ForegroundColor Cyan }

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this from an elevated PowerShell.'
}

if (-not (Test-Path (Join-Path $SourcePath 'manage.py'))) {
    throw "No manage.py under '$SourcePath'."
}
if ((Resolve-Path $SourcePath).Path -eq (Resolve-Path $InstallRoot).Path) {
    throw 'SourcePath and InstallRoot are the same directory; there is nothing to upgrade from.'
}

$venvPython = Join-Path $InstallRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) { throw "No virtual environment at $venvPython." }

if (-not $SkipBackup) {
    Write-Step 'Backing up the database first'
    & (Join-Path $InstallRoot 'deploy\windows\Backup-Database.ps1') -InstallRoot $InstallRoot
}

Write-Step "Stopping $ServiceName"
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service -and $service.Status -ne 'Stopped') {
    Stop-Service -Name $ServiceName -Force
    (Get-Service -Name $ServiceName).WaitForStatus('Stopped', (New-TimeSpan -Seconds 60))
}

Write-Step 'Replacing the source'
# /PURGE so a file deleted upstream goes away here too; without it a stale module left
# behind can keep importing and shadow the new one. Runtime state is excluded, and
# .env is excluded by name -- losing it would lose SECRET_KEY and sign everybody out.
$excludeDirs = @('.git', '.venv', 'logs', 'media', 'staticfiles', 'backups', 'certs', '__pycache__', '.pytest_cache', '.ruff_cache')
robocopy $SourcePath $InstallRoot /E /PURGE /NFL /NDL /NJH /NJS /NP /XD @excludeDirs /XF '.env' | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE." }
$global:LASTEXITCODE = 0

Write-Step 'Reinstalling dependencies'
Push-Location $InstallRoot
try {
    # As in Install-HealthIAM.ps1: pyproject.toml is a dependency manifest, not a
    # buildable package, so it is read with uv rather than `pip install .`.
    & $venvPython -m pip install --upgrade uv --quiet
    if ($LASTEXITCODE -ne 0) { throw "Could not install uv (exit $LASTEXITCODE)." }
    & $venvPython -m uv pip install --python $venvPython -r pyproject.toml --extra windows
    if ($LASTEXITCODE -ne 0) { throw "Installing dependencies failed with exit code $LASTEXITCODE." }

    $env:DJANGO_SETTINGS_MODULE = 'config.settings.prod'

    Write-Step 'Applying migrations'
    & $venvPython manage.py migrate --noinput
    if ($LASTEXITCODE -ne 0) { throw 'migrate failed. The service is still stopped; fix the cause and re-run.' }

    & $venvPython manage.py bootstrap_roles
    if ($LASTEXITCODE -ne 0) { throw 'bootstrap_roles failed.' }

    Write-Step 'Collecting static files'
    # Skipping this is the classic broken upgrade: production hashes static file names
    # into a manifest, and last release's manifest names files this release does not
    # have. Every page then raises "Missing staticfiles manifest entry".
    & $venvPython manage.py collectstatic --noinput --clear | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'collectstatic failed.' }
} finally {
    Pop-Location
    Remove-Item Env:\DJANGO_SETTINGS_MODULE -ErrorAction SilentlyContinue
}

Write-Step "Starting $ServiceName"
Start-Service -Name $ServiceName

$deadline = (Get-Date).AddSeconds(30)
$ok = $false
while ((Get-Date) -lt $deadline) {
    try {
        if ((Invoke-WebRequest -Uri 'http://127.0.0.1:8000/login/' -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200) {
            $ok = $true; break
        }
    } catch { Start-Sleep -Seconds 2 }
}

if ($ok) {
    Write-Host "`nUpgrade complete. HealthIAM is answering on http://127.0.0.1:8000/" -ForegroundColor Green
} else {
    Write-Warning @"
The service started but http://127.0.0.1:8000/login/ did not answer. Run the server in
the foreground to see the traceback:

    $venvPython $InstallRoot\deploy\windows\serve.py

To roll back: re-run this script with -SourcePath pointing at the previous checkout,
then restore the dump it took above with pg_restore if a migration has to come out too.
"@
}
