# google-ecommerce-mcp installer for Windows.
#
# Save it, read it, then run it:  .\install.ps1
# The README gives a one-line form pinned to an exact commit, and the SHA-256 of this file.
#
# 1. installs uv (Astral's official installer) if it is missing,
# 2. runs `google-ecommerce-mcp install`, pinned to the version this script was released with:
#    asks your ids, opens the Google consent screen,
#    adds the server to Claude Desktop (the previous config is backed up).
# Options: .\install.ps1 --ga4 123 --gsc sc-domain:example.com

$ErrorActionPreference = "Stop"

Write-Host "google-ecommerce-mcp: Windows installer" -ForegroundColor Cyan

if (-not (Get-Command uvx -ErrorAction SilentlyContinue)) {
    Write-Host "uv is not installed. Installing it from astral.sh (official installer)..."
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uvx -ErrorAction SilentlyContinue)) {
        Write-Host "uv was installed but uvx is not on PATH. Open a new terminal and run this installer again." -ForegroundColor Yellow
        return
    }
}

& uvx --refresh google-ecommerce-mcp==0.3.1 install @args
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installer stopped with code $LASTEXITCODE. See docs/TROUBLESHOOTING.md." -ForegroundColor Yellow
}
