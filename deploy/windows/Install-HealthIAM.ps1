<#
.SYNOPSIS
    Installs HealthIAM as a Windows service on this server.

.DESCRIPTION
    Creates the virtual environment, installs the PostgreSQL role and database, writes
    .env, applies migrations, collects static files, locks down the install directory and
    registers the "HealthIAM" service listening on 127.0.0.1:8000.

    IIS is deliberately left to you: the certificate and the site binding are site
    policy. docs/deploy-windows.md has that half, and deploy/windows/web.config is the
    rewrite configuration to drop in.

    Re-runnable. Existing .env values are kept unless -Force is given, so running it
    again after a failure part-way through is safe.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Install-HealthIAM.ps1 `
        -InstallRoot C:\HealthIAM -Hostname iam.corp.example.org

.NOTES
    Run from an elevated PowerShell. Use -ExecutionPolicy Bypass on the command line
    rather than loosening the machine's execution policy for one install.
#>
[CmdletBinding()]
param(
    # Keep this short. A venv buries package files deeply, and Windows still refuses
    # paths over 260 characters unless long paths are enabled machine-wide.
    [string]$InstallRoot = 'C:\HealthIAM',

    # The source checkout to install from. Defaults to the repository this script is in,
    # so running it from a checkout already at $InstallRoot copies nothing.
    [string]$SourcePath,

    # The FQDN people will type. Goes into ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS.
    [Parameter(Mandatory = $true)]
    [string]$Hostname,

    [string]$PythonExe,

    [string]$DbName = 'healthiam',
    [string]$DbUser = 'healthiam',
    [securestring]$DbPassword,
    [string]$DbHost = '127.0.0.1',
    [int]$DbPort = 5432,

    # psql from the PostgreSQL install. Found on PATH if not given.
    [string]$PsqlExe,

    # Skip role/database creation and just point at an existing DATABASE_URL.
    [switch]$SkipDatabase,

    [string]$ServiceName = 'HealthIAM',

    # Overwrite an existing .env instead of keeping it.
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Step { param([string]$Message) Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Note { param([string]$Message) Write-Host "    $Message" -ForegroundColor DarkGray }

# --- Preflight ---------------------------------------------------------------------

Write-Step 'Checking prerequisites'

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this from an elevated PowerShell: it registers a service and sets ACLs.'
}

if (-not $SourcePath) { $SourcePath = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
if (-not (Test-Path (Join-Path $SourcePath 'manage.py'))) {
    throw "No manage.py under '$SourcePath'. Point -SourcePath at the HealthIAM checkout."
}

if ($InstallRoot.Length -gt 40) {
    Write-Warning "InstallRoot '$InstallRoot' is long. Package paths inside .venv can exceed the 260-character limit; C:\HealthIAM is the tested choice."
}

if (-not $PythonExe) {
    $PythonExe = (Get-Command python.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike '*WindowsApps*' } |
        Select-Object -First 1 -ExpandProperty Source)
}
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    throw 'No usable python.exe found. Install Python 3.12 (64-bit) from python.org with "Install for all users", then pass -PythonExe.'
}
if ($PythonExe -like '*WindowsApps*') {
    # The Store build runs in an app container: it cannot host a service and its paths
    # vanish for accounts other than the installing user.
    throw "The Microsoft Store build of Python ($PythonExe) cannot run as a service. Install Python 3.12 (64-bit) from python.org and pass -PythonExe."
}

$versionInfo = & $PythonExe -c "import sys,struct; print(f'{sys.version_info.major}.{sys.version_info.minor} {struct.calcsize(chr(80))*8}')"
$pyVersion, $pyBits = $versionInfo.Trim().Split(' ')
Write-Note "Python $pyVersion ($pyBits-bit) at $PythonExe"
if ([version]$pyVersion -lt [version]'3.11') { throw "Python 3.11 or newer is required; found $pyVersion." }
if ($pyBits -ne '64') { throw 'A 64-bit Python is required (psycopg and pywin32 wheels).' }

if (-not $SkipDatabase) {
    if (-not $PsqlExe) {
        $PsqlExe = (Get-Command psql.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
        if (-not $PsqlExe) {
            $PsqlExe = Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\psql.exe' -ErrorAction SilentlyContinue |
                Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
        }
    }
    if (-not $PsqlExe) {
        throw 'psql.exe not found. Install PostgreSQL 16, or pass -PsqlExe, or use -SkipDatabase and set DATABASE_URL yourself.'
    }
    Write-Note "psql at $PsqlExe"
    if (-not $DbPassword) { $DbPassword = Read-Host -AsSecureString "Password for the '$DbUser' database role" }
}

# --- Lay down the source ------------------------------------------------------------

Write-Step "Installing source into $InstallRoot"

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
if ((Resolve-Path $SourcePath).Path -ne (Resolve-Path $InstallRoot).Path) {
    # /XO would skip files the source has older copies of, which is wrong for a rollback
    # to an earlier tag. Mirror the tree but never touch runtime state.
    $excludeDirs = @('.git', '.venv', 'logs', 'media', 'staticfiles', '__pycache__', '.pytest_cache', '.ruff_cache')
    robocopy $SourcePath $InstallRoot /E /NFL /NDL /NJH /NJS /NP /XD @excludeDirs /XF '.env' | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE." }
    $global:LASTEXITCODE = 0
}

foreach ($dir in @('logs', 'media', 'certs', 'backups')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot $dir) | Out-Null
}

# --- Virtual environment -------------------------------------------------------------

Write-Step 'Creating the virtual environment'

$venv = Join-Path $InstallRoot '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPython)) { & $PythonExe -m venv $venv }
# uv alongside pip: pyproject.toml here is a dependency manifest, not a distributable
# package, so `pip install .` tries to build the project and setuptools refuses it --
# "Multiple top-level packages discovered in a flat-layout". `uv pip install -r
# pyproject.toml` reads it as the manifest it is, which is how the Makefile, the
# Dockerfile and CI all install this project. uv is an ordinary wheel, so this adds a
# PyPI package rather than a binary to vet.
& $venvPython -m pip install --upgrade pip uv --quiet
if ($LASTEXITCODE -ne 0) { throw "Could not install pip/uv into the virtual environment (exit $LASTEXITCODE)." }

Write-Step 'Installing dependencies (base + windows extra)'
# The windows extra carries waitress (gunicorn imports fcntl and cannot run here) and
# pywin32 (the service host); tzdata comes from the base list behind a win32 marker.
Push-Location $InstallRoot
try {
    & $venvPython -m uv pip install --python $venvPython -r pyproject.toml --extra windows
    if ($LASTEXITCODE -ne 0) { throw "Installing dependencies failed with exit code $LASTEXITCODE." }
} finally { Pop-Location }

# pywin32 drops pythonservice.exe and its DLLs where the service manager can find them.
# Skipping this is the usual cause of a service that installs and then will not start.
$postInstall = Join-Path $venv 'Scripts\pywin32_postinstall.py'
if (Test-Path $postInstall) { & $venvPython $postInstall -install -silent | Out-Null }

# --- Database --------------------------------------------------------------------------

$plainDbPassword = $null
if (-not $SkipDatabase) {
    Write-Step "Creating the '$DbName' database"
    $plainDbPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($DbPassword))

    # Doubled for the SQL string literal below. A password containing an apostrophe
    # would otherwise close the literal early and the statement fails to parse -- a
    # perfectly ordinary password, and a confusing failure to debug.
    $sqlDbPassword = $plainDbPassword.Replace("'", "''")

    $roleSql = @"
DO `$`$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$DbUser') THEN
    CREATE ROLE $DbUser LOGIN PASSWORD '$sqlDbPassword';
  ELSE
    ALTER ROLE $DbUser LOGIN PASSWORD '$sqlDbPassword';
  END IF;
END
`$`$;
"@
    $roleSql | & $PsqlExe -h $DbHost -p $DbPort -U postgres -v ON_ERROR_STOP=1 -q
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the database role. Check that PostgreSQL is running and that you can authenticate as postgres.' }

    $exists = (& $PsqlExe -h $DbHost -p $DbPort -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DbName'").Trim()
    if ($exists -ne '1') {
        # TEMPLATE template0 is not optional. A default Windows cluster is initialised
        # with a locale like English_United States.1252, and copying template1 into a
        # UTF8 database fails with "new encoding (UTF8) is incompatible with the
        # encoding of the template database".
        & $PsqlExe -h $DbHost -p $DbPort -U postgres -v ON_ERROR_STOP=1 -q `
            -c "CREATE DATABASE $DbName OWNER $DbUser ENCODING 'UTF8' TEMPLATE template0;"
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the database.' }
        Write-Note "Created database '$DbName' (UTF8, from template0)."
    } else {
        Write-Note "Database '$DbName' already exists; left alone."
    }
}

# --- .env ---------------------------------------------------------------------------

Write-Step 'Writing .env'

$envPath = Join-Path $InstallRoot '.env'
if ((Test-Path $envPath) -and -not $Force) {
    Write-Note '.env already exists; keeping it. Re-run with -Force to regenerate.'
} else {
    $secretKey = (& $venvPython -c "import secrets; print(secrets.token_urlsafe(64))").Trim()
    $logFile = Join-Path $InstallRoot 'logs\healthiam.log'
    $databaseUrl = if ($SkipDatabase) {
        "postgres://$DbUser`:CHANGE_ME@$DbHost`:$DbPort/$DbName"
    } else {
        # The password is URL-encoded: a '@' or '/' in it would otherwise split the URL.
        $encoded = [uri]::EscapeDataString($plainDbPassword)
        "postgres://$DbUser`:$encoded@$DbHost`:$DbPort/$DbName"
    }

    $content = @"
# Written by deploy/windows/Install-HealthIAM.ps1 on $(Get-Date -Format 'yyyy-MM-dd HH:mm').
# Reference and the full key list: deploy/windows/env.windows.example, .env.example.
# Restart the $ServiceName service after any change; settings are read once, at start.
SECRET_KEY=$secretKey
DEBUG=false
ALLOWED_HOSTS=$Hostname
CSRF_TRUSTED_ORIGINS=https://$Hostname
TIME_ZONE=America/Chicago

# IIS terminates TLS and its rewrite rule sets X-Forwarded-Proto, which prod settings
# trust. Redirecting here as well would loop: waitress only ever sees plain HTTP.
SECURE_SSL_REDIRECT=false

# A Windows service has no console, so without this the log goes nowhere.
LOG_FILE=$logFile

DATABASE_URL=$databaseUrl

# Keep one local account working, so a domain controller outage cannot lock the
# administrators out of their own IAM system.
AUTH_LOCAL_LOGIN=true

# Shown on Admin > Active Directory. There is no container here to docker exec into.
SYNC_SCHEDULE_COMMAND=schtasks /Run /TN "$ServiceName AD sync"

SUPPORT_CONTACT=the Information Security team

# --- Active Directory -------------------------------------------------------
# Off until both AD_SERVER_URIS and AD_BASE_DN are set. See docs/ad-setup.md, and
# deploy/windows/env.windows.example for what differs on Windows -- in particular,
# leave AD_CA_BUNDLE empty first: on a domain-joined server Python reads the machine's
# own certificate stores, which already trust the enterprise CA.
#AD_SERVER_URIS=
#AD_BASE_DN=
#AD_BIND_DN=
#AD_BIND_PASSWORD=
"@
    # UTF-8 with NO byte order mark, and not Set-Content -Encoding UTF8, which writes one
    # in Windows PowerShell 5.1. django-environ opens .env as plain utf8 and does not
    # strip a BOM, so the first line would arrive as "\ufeffSECRET_KEY=..." -- it fails
    # the key pattern, is skipped as an invalid line, and the service dies with
    # "ImproperlyConfigured: Set the SECRET_KEY environment variable" pointing at a file
    # that plainly contains one.
    [System.IO.File]::WriteAllText($envPath, $content, (New-Object System.Text.UTF8Encoding($false)))
    Write-Note "Wrote $envPath with a freshly generated SECRET_KEY."
}

# --- Django setup ---------------------------------------------------------------------

Write-Step 'Applying migrations and collecting static files'

Push-Location $InstallRoot
try {
    $env:DJANGO_SETTINGS_MODULE = 'config.settings.prod'
    # The same two commands docker/entrypoint.sh runs on every container start.
    & $venvPython manage.py migrate --noinput
    if ($LASTEXITCODE -ne 0) { throw 'migrate failed. Check DATABASE_URL in .env and that PostgreSQL is reachable.' }
    & $venvPython manage.py bootstrap_roles
    if ($LASTEXITCODE -ne 0) { throw 'bootstrap_roles failed.' }
    & $venvPython manage.py bootstrap_person_types
    if ($LASTEXITCODE -ne 0) { throw 'bootstrap_person_types failed.' }
    # The container does this at image build time. Not optional: production uses
    # CompressedManifestStaticFilesStorage, and without the manifest every page raises
    # "Missing staticfiles manifest entry".
    & $venvPython manage.py collectstatic --noinput | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'collectstatic failed.' }
} finally {
    Pop-Location
    Remove-Item Env:\DJANGO_SETTINGS_MODULE -ErrorAction SilentlyContinue
}

# --- Service ----------------------------------------------------------------------------

Write-Step "Registering the $ServiceName service"

$serviceScript = Join-Path $InstallRoot 'deploy\windows\healthiam_service.py'
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Status -ne 'Stopped') { Stop-Service -Name $ServiceName -Force }
    & $venvPython $serviceScript remove | Out-Null
    Start-Sleep -Seconds 2
}
& $venvPython $serviceScript --startup auto install
if ($LASTEXITCODE -ne 0) { throw 'Registering the service failed.' }

# A virtual account: per-service, no password to store or rotate, and it already holds
# the "Log on as a service" right that a normal local account would need granting
# through secedit. It works here because nothing authenticates to the network as the
# service identity -- the LDAPS bind uses AD_BIND_DN/AD_BIND_PASSWORD from .env and
# PostgreSQL uses the password in DATABASE_URL.
$serviceAccount = "NT SERVICE\$ServiceName"
& sc.exe config $ServiceName obj= $serviceAccount password= "" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Could not set the service to run as $serviceAccount; it will run as LocalSystem. Change it under services.msc if that matters to you."
    $serviceAccount = 'NT AUTHORITY\SYSTEM'
}

# Restart on failure: after 60s, again after 60s, then every 5 minutes. The equivalent
# of `restart: unless-stopped` in the compose file.
& sc.exe failure $ServiceName reset= 86400 actions= restart/60000/restart/60000/restart/300000 | Out-Null
& sc.exe description $ServiceName "HealthIAM application catalog and position-based access defaults." | Out-Null

# --- Permissions -------------------------------------------------------------------------

Write-Step 'Setting permissions'

# Read and execute over the tree, write only where the app actually writes.
& icacls $InstallRoot /inheritance:r /grant:r `
    '*S-1-5-32-544:(OI)(CI)F' `
    '*S-1-5-18:(OI)(CI)F' `
    "$serviceAccount`:(OI)(CI)RX" /T /Q | Out-Null

foreach ($dir in @('logs', 'media', 'staticfiles', 'backups')) {
    $path = Join-Path $InstallRoot $dir
    if (Test-Path $path) { & icacls $path /grant "$serviceAccount`:(OI)(CI)M" /T /Q | Out-Null }
}

# .env holds SECRET_KEY and, once AD is configured, the directory bind password.
# Nobody but the administrators, the system and the service needs to read it.
& icacls $envPath /inheritance:r /grant:r `
    '*S-1-5-32-544:F' '*S-1-5-18:F' "$serviceAccount`:R" /Q | Out-Null
Write-Note "Restricted $envPath to Administrators, SYSTEM and $serviceAccount."

# --- Start -----------------------------------------------------------------------------

Write-Step 'Starting the service'

Start-Service -Name $ServiceName
$deadline = (Get-Date).AddSeconds(30)
$ok = $false
while ((Get-Date) -lt $deadline) {
    try {
        $probe = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/login/' -UseBasicParsing -TimeoutSec 5
        if ($probe.StatusCode -eq 200) { $ok = $true; break }
    } catch { Start-Sleep -Seconds 2 }
}

if (-not $ok) {
    Write-Warning @"
The service started but http://127.0.0.1:8000/login/ did not answer.
Run the server in the foreground to see the traceback the service swallows:

    $venvPython $InstallRoot\deploy\windows\serve.py

Also check Event Viewer > Windows Logs > Application, source "$ServiceName", and
$InstallRoot\logs\healthiam.log.
"@
} else {
    Write-Host "`nHealthIAM is answering on http://127.0.0.1:8000/" -ForegroundColor Green
}

# --- What is left to do -------------------------------------------------------------------

Write-Host @"

Still to do, by hand (docs/deploy-windows.md):

  1. IIS. Install URL Rewrite 2.1 and then ARR 3.0 (in that order), then:
       .\deploy\windows\Setup-IIS.ps1 -InstallRoot $InstallRoot -Hostname $Hostname
     Run it once without -CertificateThumbprint to list your certificates, then
     again with one. It sets the three machine-scope options that cannot live in
     web.config and that all fail silently: preserveHostHeader, the allowed
     X-Forwarded-Proto server variable, and the 30-second ARR proxy time-out.

  2. Create the first administrator:
       cd $InstallRoot
       .venv\Scripts\python.exe manage.py createsuperuser

  3. Active Directory, if you want it: fill in the AD_ block in .env
     (docs/ad-setup.md), restart the service, then register the nightly sync:
       .\deploy\windows\Register-SyncTask.ps1 -InstallRoot $InstallRoot

  4. Backups. The database holds the audit trail as well as the catalog:
       .\deploy\windows\Register-BackupTask.ps1 -InstallRoot $InstallRoot

"@ -ForegroundColor Yellow
