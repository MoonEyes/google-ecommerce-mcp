#!/usr/bin/env sh
# google-ecommerce-mcp installer for macOS and Linux.
#
#   curl -LsSf https://raw.githubusercontent.com/MoonEyes/google-ecommerce-mcp/main/install.sh | sh
#
# 1. installs uv (Astral's official installer) if it is missing,
# 2. runs `google-ecommerce-mcp install`: asks your ids, opens the Google consent screen,
#    adds the server to Claude Desktop (the previous config is backed up).
# Options: sh install.sh --ga4 123 --gsc sc-domain:example.com
set -eu

SOURCE="https://github.com/MoonEyes/google-ecommerce-mcp/archive/refs/heads/main.zip"

echo "google-ecommerce-mcp: macOS / Linux installer"

if ! command -v uvx >/dev/null 2>&1; then
  echo "uv is not installed. Installing it from astral.sh (official installer)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

if ! command -v uvx >/dev/null 2>&1; then
  echo "uv was installed but uvx is not on PATH. Open a new terminal and run this installer again."
  exit 1
fi

# When piped from curl, stdin is the script itself: read answers from the terminal instead.
if [ -t 0 ] || [ ! -r /dev/tty ]; then
  exec uvx --refresh --from "$SOURCE" google-ecommerce-mcp install "$@"
else
  exec uvx --refresh --from "$SOURCE" google-ecommerce-mcp install "$@" < /dev/tty
fi
