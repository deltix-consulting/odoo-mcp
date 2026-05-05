# odoo-mcp bootstrap installer for Windows 10+.
#
# Run from PowerShell (not cmd):
#   powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#
# Or pipe:
#   irm https://raw.githubusercontent.com/deltix-consulting/odoo-mcp/main/scripts/install.ps1 | iex
#
# Mirrors scripts/install.sh: same nine steps, same attestation policy,
# same soft-fail semantics for environmental issues.

[CmdletBinding()]
param(
    [switch]$SkipVerification,
    [switch]$Git
)

$ErrorActionPreference = 'Stop'
$Repo          = 'deltix-consulting/odoo-mcp'
$DefaultHome   = Join-Path $env:USERPROFILE 'odoo-mcp'
$InstallDir    = if ($env:ODOO_MCP_HOME) { $env:ODOO_MCP_HOME } else { $DefaultHome }
$TotalSteps    = 9

function Step([int]$n, [string]$msg) {
    Write-Host ""
    Write-Host ("[{0}/{1}] {2}" -f $n, $TotalSteps, $msg)
}

function Fail([string]$msg, [string]$fix = '') {
    Write-Host ""
    Write-Host ("Error: {0}" -f $msg) -ForegroundColor Red
    if ($fix) { Write-Host ("Fix: {0}" -f $fix) -ForegroundColor Yellow }
    exit 1
}

function Have-Cmd([string]$name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# ----------------------------------------------------------------------
Step 1 'Checking platform'
$os = [Environment]::OSVersion
if ($os.Platform -ne 'Win32NT' -or $os.Version.Major -lt 10) {
    Fail "odoo-mcp on Windows requires Windows 10 or newer (detected $($os.VersionString))." `
         'Run this installer on a Windows 10+ machine.'
}
Write-Host "  Windows $($os.Version) detected."

# ----------------------------------------------------------------------
Step 2 'Checking for winget'
if (-not (Have-Cmd 'winget')) {
    Fail 'winget is required to install dependencies on Windows.' `
         'Install "App Installer" from the Microsoft Store, or download the MSI from https://aka.ms/getwinget'
}
Write-Host "  winget found."

# ----------------------------------------------------------------------
Step 3 'Checking for gh CLI and authentication'
if (-not (Have-Cmd 'gh')) {
    Write-Host '  gh not found. Installing via winget...'
    winget install --silent --id GitHub.cli --accept-package-agreements --accept-source-agreements | Out-Null
    if (-not (Have-Cmd 'gh')) {
        Fail 'gh installed but is not on PATH.' 'Open a new PowerShell window and re-run this installer.'
    }
}
gh auth status *> $null
if ($LASTEXITCODE -ne 0) {
    Fail 'gh CLI is not authenticated.' 'Run: gh auth login'
}
Write-Host "  gh CLI authenticated."

# ----------------------------------------------------------------------
Step 4 'Checking for uv'
if (-not (Have-Cmd 'uv')) {
    Write-Host '  uv not found. Installing via winget...'
    winget install --silent --id astral-sh.uv --accept-package-agreements --accept-source-agreements | Out-Null
    # winget puts uv into a dir not yet on PATH for this session.
    $uvBin = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'
    if (Test-Path $uvBin) { $env:Path = "$uvBin;$env:Path" }
    if (-not (Have-Cmd 'uv')) {
        Fail 'uv installed but is not on PATH.' 'Open a new PowerShell window and re-run this installer.'
    }
}
Write-Host "  uv found: $((Get-Command uv).Source)"

# ----------------------------------------------------------------------
Step 5 'Choosing install directory'
if (Test-Path $InstallDir) {
    Fail "Install directory already exists: $InstallDir" `
         "To update, run: cd '$InstallDir'; uv run odoo-mcp update"
}
Write-Host "  Will install to: $InstallDir"

# ----------------------------------------------------------------------
Step 6 'Fetching source'
$FetchedViaRelease = $false
$Tarball = $null
$TmpDir = $null

if (-not $Git) {
    $LatestTag = ''
    try {
        $LatestTag = (gh release view --repo $Repo --json tagName --jq .tagName 2>$null).Trim()
    } catch { $LatestTag = '' }

    if ($LatestTag) {
        Write-Host "  Latest release: $LatestTag"
        $TmpDir = Join-Path $env:TEMP ("odoo-mcp-install-" + [System.IO.Path]::GetRandomFileName())
        New-Item -ItemType Directory -Path $TmpDir | Out-Null
        gh release download $LatestTag --repo $Repo --pattern '*.tar.gz' --dir $TmpDir *> $null
        if ($LASTEXITCODE -eq 0) {
            $Tarball = Get-ChildItem -Path $TmpDir -Filter '*.tar.gz' | Select-Object -First 1 -ExpandProperty FullName
            if ($Tarball) {
                $FetchedViaRelease = $true
                Write-Host "  Downloaded $LatestTag tarball"
            }
        }
        if (-not $FetchedViaRelease) {
            Write-Host '  Release download failed; falling back to git clone.'
            Remove-Item -Recurse -Force $TmpDir -ErrorAction SilentlyContinue
            $TmpDir = $null
        }
    } else {
        Write-Host '  No releases published yet; falling back to git clone.'
    }
}

# ----------------------------------------------------------------------
Step 7 'Verifying release attestation'
if ($FetchedViaRelease) {
    if ($SkipVerification) {
        Write-Host '  Skipping attestation verification (--SkipVerification).' -ForegroundColor Yellow
    } else {
        gh auth status *> $null
        if ($LASTEXITCODE -eq 0) {
            $verifyOutput = & gh attestation verify `
                --owner deltix-consulting `
                --signer-workflow ".github/workflows/release.yml" `
                $Tarball 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Host '  Attestation verified.'
            } elseif ($verifyOutput -match '(?i)no attestations') {
                Write-Host ""
                Write-Host "  Warning: no attestations found for $LatestTag." -ForegroundColor Yellow
                Write-Host '  This can happen on free-tier GitHub orgs.'
                $reply = Read-Host '  Proceed anyway? [y/N]'
                if ($reply -notmatch '^(y|yes)$') {
                    Fail 'Aborted by user.' 'Re-run with -SkipVerification to bypass.'
                }
            } else {
                Write-Host ""
                Write-Host ("  Attestation verification FAILED for {0}:" -f $LatestTag) -ForegroundColor Red
                $verifyOutput | ForEach-Object { Write-Host "    $_" }
                Fail 'Refusing to install an unverified release tarball.' `
                     'If unexpected, file an issue. To bypass for environmental reasons only, re-run with -SkipVerification.'
            }
        } else {
            Write-Host '  Warning: gh CLI is not authenticated; skipping attestation check.' -ForegroundColor Yellow
        }
    }
    New-Item -ItemType Directory -Path $InstallDir | Out-Null
    tar -xzf $Tarball -C $InstallDir --strip-components=1
    Remove-Item -Recurse -Force $TmpDir -ErrorAction SilentlyContinue
    Write-Host "  Extracted $LatestTag into $InstallDir"
} else {
    Write-Host '  Skipped (no release tarball — using git clone).'
    gh repo clone $Repo $InstallDir -- --quiet
    Write-Host "  Cloned $Repo into $InstallDir"
}

# ----------------------------------------------------------------------
Step 8 'Installing Python dependencies (uv sync)'
Push-Location $InstallDir
try {
    uv sync
    if ($LASTEXITCODE -ne 0) { Fail 'uv sync failed.' '' }

    Write-Host ''
    Write-Host '  Installing odoo-mcp on PATH (uv tool install --editable)...'
    uv tool install --editable . --force | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail 'uv tool install failed.' '' }
    Write-Host "  odoo-mcp CLI installed."

    # uv puts tool entry points in %USERPROFILE%\.local\bin on Windows.
    # Make it available NOW (so the setup wizard launched below can resolve
    # odoo-mcp without restarting PowerShell) AND persist it on the User
    # PATH so future shells see it. We avoid appending if it's already
    # there, so re-running the installer doesn't pile up duplicates.
    $localBin = Join-Path $env:USERPROFILE '.local\bin'
    if ($env:Path -notlike "*$localBin*") {
        $env:Path = "$localBin;$env:Path"
    }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $userPath) { $userPath = '' }
    if (($userPath -split ';') -notcontains $localBin) {
        $newUserPath = if ($userPath) { "$userPath;$localBin" } else { $localBin }
        [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
        Write-Host ''
        Write-Host "  Added '$localBin' to your User PATH."
        Write-Host '  Open a new PowerShell window before using odoo-mcp commands.'
    }
    if (-not (Have-Cmd 'odoo-mcp')) {
        Write-Host "  Warning: 'odoo-mcp' is not on PATH after install." -ForegroundColor Yellow
    }
} finally {
    Pop-Location
}

# ----------------------------------------------------------------------
Step 9 'Launching setup wizard'
Write-Host '  Install complete. Running setup wizard...'
Push-Location $InstallDir
try {
    uv run odoo-mcp setup
} finally {
    Pop-Location
}
