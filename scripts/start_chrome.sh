#!/usr/bin/env bash
# Start Chrome with a remote debugging port for CDP-based API calls.
#
# Usage:
#   ./scripts/start_chrome.sh [port] [--headed]
#
# Examples:
#   ./scripts/start_chrome.sh                  # headless on port 9222
#   ./scripts/start_chrome.sh 9222 --headed    # headed on port 9222 (for login)
#
# On macOS, this uses the Google Chrome.app binary automatically.
# On Linux, it expects `google-chrome` or `chromium` on PATH.

set -euo pipefail

PORT="${1:-9222}"
HEADED="${2:-}"

# Detect the Chrome binary.
if [[ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]]; then
  CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
elif command -v google-chrome &>/dev/null; then
  CHROME="google-chrome"
elif command -v chromium &>/dev/null; then
  CHROME="chromium"
else
  echo "Error: Could not find Chrome/Chromium." >&2
  echo "Install Chrome or set CHROME_BIN to the binary path." >&2
  exit 1
fi

ARGS=(
  "--remote-debugging-port=${PORT}"
  "--disable-gpu"
  "--no-sandbox"
  "--disable-blink-features=AutomationControlled"
)

if [[ "${HEADED}" != "--headed" ]]; then
  ARGS+=("--headless=new")
fi

echo "Starting Chrome on CDP port ${PORT}..."
echo "  Binary: ${CHROME}"
echo "  Headless: $([[ "${HEADED}" == "--headed" ]] && echo "no" || echo "yes")"
echo ""
echo "Connect with UBEREATS_CDP_PORT=${PORT}"

exec "${CHROME}" "${ARGS[@]}"
