<#
.SYNOPSIS
    Dumps the HealthIAM database to a dated file and prunes old dumps.

.DESCRIPTION
    The TrueNAS deployment backs up by snapshotting the ZFS dataset under the database
    container. A Windows host has no equivalent, so this is the backup -- and it is not
    optional: the database holds the audit trail of who changed what, not just the
    catalog.

    Uses pg_dump's custom format (-Fc), which is compressed and restores selectively
    with pg_restore. Credentials come from DATABASE_URL in .env, so there is no second
    copy of the password to keep in step.

.EXAMPLE
    .\Backup-Database.ps1 -InstallRoot C:\HealthIAM -KeepDays 30
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\HealthIAM',
    [string]$BackupDir,
    [int]$KeepDays = 30,
    [string]$PgDumpExe
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $BackupDir) { $BackupDir = Join-Path $InstallRoot 'backups' }
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

if (-not $PgDumpExe) {
    $PgDumpExe = (Get-Command pg_dump.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    if (-not $PgDumpExe) {
        $PgDumpExe = Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\pg_dump.exe' -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
}
if (-not $PgDumpExe) { throw 'pg_dump.exe not found. Pass -PgDumpExe.' }

# Read DATABASE_URL out of .env rather than taking it as a parameter: one source of
# truth, and no password on a command line where it would land in the event log.
$envPath = Join-Path $InstallRoot '.env'
if (-not (Test-Path $envPath)) { throw "No .env at $envPath." }
$line = Select-String -Path $envPath -Pattern '^\s*DATABASE_URL\s*=\s*(.+)$' | Select-Object -First 1
if (-not $line) { throw "No DATABASE_URL in $envPath." }
$databaseUrl = $line.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'")

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$outFile = Join-Path $BackupDir "healthiam-$stamp.dump"

# pg_dump reads the whole connection, password included, from the URI.
& $PgDumpExe --format=custom --no-owner --file=$outFile $databaseUrl
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE." }

$size = [math]::Round((Get-Item $outFile).Length / 1MB, 1)
Write-Host "Wrote $outFile ($size MB)."

if ($KeepDays -gt 0) {
    $cutoff = (Get-Date).AddDays(-$KeepDays)
    $stale = Get-ChildItem -Path $BackupDir -Filter 'healthiam-*.dump' |
        Where-Object { $_.LastWriteTime -lt $cutoff }
    foreach ($file in $stale) {
        Remove-Item $file.FullName -Force
        Write-Host "Pruned $($file.Name)."
    }
}

Write-Host @"

Restore with:
    pg_restore --clean --if-exists --no-owner --dbname "<DATABASE_URL>" "$outFile"

Copy these dumps off this server. A backup that only exists on the machine it is
backing up is not a backup.
"@ -ForegroundColor DarkGray
