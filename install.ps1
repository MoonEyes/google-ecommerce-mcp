# google-ecommerce-mcp installer for Windows.
#
#   irm https://raw.githubusercontent.com/MoonEyes/google-ecommerce-mcp/main/install.ps1 | iex
#
# 1. installs uv (Astral's official installer) if it is missing,
# 2. runs `google-ecommerce-mcp install`: asks your ids, opens the Google consent screen,
#    adds the server to Claude Desktop (the previous config is backed up).
# Options work when the script is saved first: .\install.ps1 --ga4 123 --gsc sc-domain:example.com

$ErrorActionPreference = "Stop"
$Source = "https://github.com/MoonEyes/google-ecommerce-mcp/archive/refs/heads/main.zip"

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

& uvx --refresh --from $Source google-ecommerce-mcp install @args
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installer stopped with code $LASTEXITCODE. See docs/TROUBLESHOOTING.md." -ForegroundColor Yellow
}
