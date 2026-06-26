#!/usr/bin/env bash
# Start Chrome with a remote debugging port for CDP-based API calls.
#
# Usage:
#   ./scripts/start_chrome.sh [port] [--headless]
#
# Examples:
#   ./scripts/start_chrome.sh                  # headed via xvfb (default, passes Cloudflare)
#   ./scripts/start_chrome.sh 9222 --headless  # truly headless (may be blocked by Cloudflare)
#
# On macOS, this uses the Google Chrome.app binary automatically.
# On Linux, it expects `google-chrome` or `chromium` on PATH.
#
# Chrome 136+ ignores --remote-debugging-port for the default profile, so we
# use a dedicated user-data-dir. Override with UBEREATS_CHROME_USER_DATA_DIR.
#
# IMPORTANT: headed mode (default) is recommended because headless Chrome gets
# challenged by Cloudflare, which blocks user-specific API endpoints (cart,
# draft orders, saved addresses). Headed Chrome via xvfb passes Cloudflare
# reliably on headless servers.

set -euo pipefail

PORT="${1:-9222}"
HEADLESS_FLAG="${2:-}"

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

USER_DATA_DIR="${UBEREATS_CHROME_USER_DATA_DIR:-${HOME}/.ubereats-chrome-profile}"

ARGS=(
  "--remote-debugging-port=${PORT}"
  "--user-data-dir=${USER_DATA_DIR}"
  "--no-first-run"
  "--disable-gpu"
  "--no-sandbox"
  "--disable-blink-features=AutomationControlled"
)

if [[ "${HEADLESS_FLAG}" == "--headless" ]]; then
  ARGS+=("--headless=new")
  echo "Starting Chrome in HEADLESS mode on CDP port ${PORT}..."
  echo "  WARNING: headless Chrome may be blocked by Cloudflare."
  echo "  If user-specific API calls 401, use headed mode (default) instead."
  exec "${CHROME}" "${ARGS[@]}"
fi

# Headed mode — use xvfb on Linux if no display is available.
if [[ "$(uname)" == "Linux" ]] && [[ -z "${DISPLAY:-}" ]]; then
  if ! command -v xvfb-run &>/dev/null; then
    echo "Error: No DISPLAY and xvfb-run not found." >&2
    echo "Install xvfb: sudo dnf install -y xorg-x11-server-Xvfb" >&2
    echo "Or run with --headless (but Cloudflare may block it)." >&2
    exit 1
  fi
  echo "Starting Chrome in HEADED mode via xvfb on CDP port ${PORT}..."
  echo "  Binary: ${CHROME}"
  echo "  User data dir: ${USER_DATA_DIR}"
  echo "  Display: virtual (xvfb)"
  echo ""
  echo "Connect with UBEREATS_CDP_PORT=${PORT}"
  exec xvfb-run -a --server-args="-screen 0 1280x1024x24" "${CHROME}" "${ARGS[@]}"
fi

echo "Starting Chrome in HEADED mode on CDP port ${PORT}..."
echo "  Binary: ${CHROME}"
echo "  User data dir: ${USER_DATA_DIR}"
echo ""
echo "Connect with UBEREATS_CDP_PORT=${PORT}"

exec "${CHROME}" "${ARGS[@]}"
