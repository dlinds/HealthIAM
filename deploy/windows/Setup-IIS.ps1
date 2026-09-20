<#
.SYNOPSIS
    Configures IIS as the TLS reverse proxy in front of HealthIAM.

.DESCRIPTION
    Three of the settings this deployment depends on live in applicationHost.config, at
    machine scope, and cannot be expressed in the site's web.config at all. Each of them
    fails quietly rather than loudly, which is why this is a script and not a paragraph:

      preserveHostHeader   Off by default, so IIS forwards "Host: 127.0.0.1:8000". Django
                           then builds absolute URLs from that, and Entra SSO fails with
                           AADSTS50011 (redirect URI mismatch) while everything else
                           appears to work.
      allowedServerVariables
                           A rewrite rule may only set a server variable named here.
                           Without it the rule is rejected and IIS answers HTTP 500.50 --
                           and with it missing, X-Forwarded-Proto never reaches the app,
                           so sign-in loops with no error.
      proxyTimeout         30 seconds by default. "Sync now" reads a whole directory in
                           one request and will exceed that on any real domain.

    Re-runnable. Safe to run against an existing site; it updates rather than duplicates.

.EXAMPLE
    .\Setup-IIS.ps1 -InstallRoot C:\HealthIAM -Hostname iam.corp.example.org `
        -CertificateThumbprint A1B2C3...

.NOTES
    Run elevated. Install URL Rewrite 2.1 and then Application Request Routing 3.0 first;
    ARR must be installed after URL Rewrite or its proxy settings do not appear.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\HealthIAM',
    [Parameter(Mandatory = $true)][string]$Hostname,

    # Thumbprint of a certificate in Cert:\LocalMachine\My. Omit to list the candidates
    # and stop, which is the usual first run.
    [string]$CertificateThumbprint,

    [string]$SiteName = 'HealthIAM',
    [int]$UpstreamPort = 8000,
    # Must exceed the longest "Sync now" on your domain. 300s matches serve.py.
    [int]$ProxyTimeoutSeconds = 300
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Step { param([string]$Message) Write-Host "`n==> $Message" -ForegroundColor Cyan }

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this from an elevated PowerShell.'
}

Import-Module WebAdministration -ErrorAction Stop

Write-Step 'Checking for URL Rewrite and ARR'
$modules = (Get-WebGlobalModule).Name
foreach ($needed in @('RewriteModule', 'ApplicationRequestRouting')) {
    if ($modules -notcontains $needed) {
        throw @"
The IIS module '$needed' is not installed.

Install in this order, then re-run:
  1. URL Rewrite 2.1   (rewrite_amd64_en-US.msi)
  2. Application Request Routing 3.0  (requestRouterAMD64.msi)

ARR must come second; installed first it does not register its proxy settings.
"@
    }
}

# --- The three machine-scope settings ---------------------------------------------------

Write-Step 'Applying server-level proxy settings'

Set-WebConfigurationProperty -PSPath 'MACHINE/WEBROOT/APPHOST' `
    -Filter 'system.webServer/proxy' -Name 'enabled' -Value 'True'

# Without this Django sees Host: 127.0.0.1:8000 and OIDC redirect URIs come out wrong.
Set-WebConfigurationProperty -PSPath 'MACHINE/WEBROOT/APPHOST' `
    -Filter 'system.webServer/proxy' -Name 'preserveHostHeader' -Value 'True'

# The 30s default kills "Sync now" mid-read.
Set-WebConfigurationProperty -PSPath 'MACHINE/WEBROOT/APPHOST' `
    -Filter 'system.webServer/proxy' -Name 'timeout' `
    -Value ([TimeSpan]::FromSeconds($ProxyTimeoutSeconds).ToString())

# A rewrite rule may only set a server variable that is allowed here.
$allowed = (Get-WebConfiguration -PSPath 'MACHINE/WEBROOT/APPHOST' `
        -Filter 'system.webServer/rewrite/allowedServerVariables').Collection
if ($allowed.Name -notcontains 'HTTP_X_FORWARDED_PROTO') {
    Add-WebConfiguration -PSPath 'MACHINE/WEBROOT/APPHOST' `
        -Filter 'system.webServer/rewrite/allowedServerVariables' `
        -Value @{ name = 'HTTP_X_FORWARDED_PROTO' }
    Write-Host '    Allowed HTTP_X_FORWARDED_PROTO.' -ForegroundColor DarkGray
} else {
    Write-Host '    HTTP_X_FORWARDED_PROTO already allowed.' -ForegroundColor DarkGray
}

# --- The site ---------------------------------------------------------------------------

Write-Step "Creating the '$SiteName' site"

# A dedicated empty directory, deliberately NOT the install root. If the rewrite module is
# ever disabled or the rule removed, a site rooted at the source tree would serve .env --
# SECRET_KEY and the directory bind password -- as a plain text file.
$siteRoot = Join-Path $InstallRoot 'iisroot'
New-Item -ItemType Directory -Force -Path $siteRoot | Out-Null
Copy-Item -Path (Join-Path $PSScriptRoot 'web.config') -Destination $siteRoot -Force

$rendered = (Get-Content (Join-Path $siteRoot 'web.config') -Raw).Replace('127.0.0.1:8000', "127.0.0.1:$UpstreamPort")
[System.IO.File]::WriteAllText((Join-Path $siteRoot 'web.config'), $rendered, (New-Object System.Text.UTF8Encoding($false)))

if (-not (Test-Path "IIS:\AppPools\$SiteName")) { New-WebAppPool -Name $SiteName | Out-Null }
# No .NET is loaded: everything is proxied. And the pool must not recycle or idle out,
# or the proxy stops answering on an otherwise healthy server.
Set-ItemProperty "IIS:\AppPools\$SiteName" -Name managedRuntimeVersion -Value ''
Set-ItemProperty "IIS:\AppPools\$SiteName" -Name processModel.idleTimeout -Value ([TimeSpan]::Zero)
Set-ItemProperty "IIS:\AppPools\$SiteName" -Name recycling.periodicRestart.time -Value ([TimeSpan]::Zero)

if (-not (Get-Website -Name $SiteName -ErrorAction SilentlyContinue)) {
    New-Website -Name $SiteName -PhysicalPath $siteRoot -ApplicationPool $SiteName `
        -HostHeader $Hostname -Port 80 | Out-Null
} else {
    Set-ItemProperty "IIS:\Sites\$SiteName" -Name physicalPath -Value $siteRoot
}

# --- TLS ------------------------------------------------------------------------------

Write-Step 'Binding the certificate'

if (-not $CertificateThumbprint) {
    Write-Host 'No -CertificateThumbprint given. Candidates in LocalMachine\My:' -ForegroundColor Yellow
    Get-ChildItem Cert:\LocalMachine\My |
        Where-Object { $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) } |
        Select-Object Thumbprint, Subject, NotAfter | Format-Table -AutoSize
    Write-Warning "Re-run with -CertificateThumbprint to finish. The site is created but has no HTTPS binding yet."
    return
}

$cert = Get-Item "Cert:\LocalMachine\My\$CertificateThumbprint" -ErrorAction SilentlyContinue
if (-not $cert) { throw "No certificate with thumbprint $CertificateThumbprint in Cert:\LocalMachine\My." }
if (-not $cert.HasPrivateKey) { throw 'That certificate has no private key; IIS cannot use it.' }

if (-not (Get-WebBinding -Name $SiteName -Protocol https -ErrorAction SilentlyContinue)) {
    New-WebBinding -Name $SiteName -Protocol https -Port 443 -HostHeader $Hostname -SslFlags 1
}
# SslFlags 1 is SNI, so the binding is keyed on hostname:port.
$binding = Get-WebBinding -Name $SiteName -Protocol https
$binding.AddSslCertificate($CertificateThumbprint, 'My')

Write-Step 'Opening the firewall'
foreach ($port in @(80, 443)) {
    $ruleName = "HealthIAM HTTPS ($port)"
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP `
            -LocalPort $port -Action Allow | Out-Null
    }
}
# Nothing is opened for $UpstreamPort on purpose: waitress listens on loopback only, so
# the app cannot be reached except through IIS and its TLS.

Restart-WebAppPool -Name $SiteName
Start-Website -Name $SiteName -ErrorAction SilentlyContinue

Write-Step 'Done'
Get-WebConfiguration -PSPath 'MACHINE/WEBROOT/APPHOST' -Filter 'system.webServer/proxy' |
    Select-Object enabled, preserveHostHeader, timeout | Format-List

Write-Host @"
Check it end to end by signing in at https://$Hostname/ -- not just loading the page.
A successful sign-in is what proves X-Forwarded-Proto is arriving; the page renders
perfectly well without it and the login form simply returns to itself.

If the certificate is later renewed by autoenrolment its thumbprint changes and this
binding keeps pointing at the old one. Re-run with the new thumbprint; `netsh http show
sslcert` shows what is currently bound.
"@ -ForegroundColor DarkGray
