[CmdletBinding()]
param(
    [string]$InstallRoot,
    [string]$BinDir,
    [switch]$NoPathUpdate
)

$ErrorActionPreference = "Stop"
$SourceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Installer = Join-Path $SourceRoot "scripts\install.py"

if (-not $env:LOCALAPPDATA -and -not $InstallRoot) {
    throw "LOCALAPPDATA is unavailable; pass -InstallRoot explicitly."
}

if ($InstallRoot) {
    $resolvedRoot = [System.IO.Path]::GetFullPath($InstallRoot)
    $env:CW_INSTALL_ROOT = $resolvedRoot
}
if ($BinDir) {
    $resolvedBin = [System.IO.Path]::GetFullPath($BinDir)
    $env:CW_BIN_DIR = $resolvedBin
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source $Installer $SourceRoot
} else {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $launcher) {
        throw "Python 3.10 or newer is required. Install Python, then rerun install.ps1."
    }
    & $launcher.Source -3 $Installer $SourceRoot
}
if ($LASTEXITCODE -ne 0) {
    throw "CW staged installation failed with exit code $LASTEXITCODE."
}

$cwBin = if ($BinDir) {
    [System.IO.Path]::GetFullPath($BinDir)
} elseif ($InstallRoot) {
    Join-Path ([System.IO.Path]::GetFullPath($InstallRoot)) "bin"
} else {
    Join-Path $env:LOCALAPPDATA "Queopius\CW\bin"
}

if (-not $NoPathUpdate) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($userPath -split ";" | Where-Object { $_ })
    $normalizedBin = $cwBin.TrimEnd("\")
    $present = $entries | Where-Object {
        $_.Trim().TrimEnd("\").Equals($normalizedBin, [StringComparison]::OrdinalIgnoreCase)
    }
    if (-not $present) {
        $updated = (@($entries) + $cwBin) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $updated, "User")
        Write-Host "Added CW to the current user's PATH. Open a new PowerShell session to inherit it."
    }
}

if (-not (($env:Path -split ";") -contains $cwBin)) {
    $env:Path = "$cwBin;$env:Path"
}

$cw = Join-Path $cwBin "cw.cmd"
if (-not (Test-Path -LiteralPath $cw -PathType Leaf)) {
    throw "CW launcher was not created at $cw"
}
& $cw version --json
if ($LASTEXITCODE -ne 0) {
    throw "Installed CW launcher failed its final smoke test."
}
