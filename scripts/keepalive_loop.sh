#!/usr/bin/env bash
# Keep the Uber Eats session alive by pinging getFeedV1 every 15 minutes.
#
# Usage:
#   ./scripts/keepalive_loop.sh
#
# Run this in a separate terminal or as a background service (systemd, tmux, etc.)
# It calls the MCP server's keepalive function via Python, which makes a
# getFeedV1 API call through Chrome to refresh sliding session cookies.
#
# Requires:
#   - Chrome running with --remote-debugging-port=9222 (via start_chrome.sh)
#   - UBEREATS_CDP_PORT and UBEREATS_WEB_LOCALE env vars (or defaults below)

set -euo pipefail

INTERVAL="${UBEREATS_KEEPALIVE_INTERVAL:-900}"  # 15 minutes in seconds
CDP_PORT="${UBEREATS_CDP_PORT:-9222}"
LOCALE="${UBEREATS_WEB_LOCALE:-us-en}"

echo "Uber Eats keepalive loop"
echo "  Interval: ${INTERVAL}s ($(( INTERVAL / 60 )) min)"
echo "  CDP port: ${CDP_PORT}"
echo "  Locale:   ${LOCALE}"
echo "  Press Ctrl-C to stop."
echo ""

while true; do
    echo "[$(date '+%H:%M:%S')] Pinging session..."

    uv run python3 -c "
import asyncio, os
os.environ['UBEREATS_CDP_PORT'] = '${CDP_PORT}'
os.environ['UBEREATS_WEB_LOCALE'] = '${LOCALE}'
from uber_eats_mcp.browser import manager

async def ping():
    result = await manager.keepalive()
    print(f'  → {result}')

asyncio.run(ping())
" 2>&1 | grep -v "DeprecationWarning\|trace-deprecation"

    echo "[$(date '+%H:%M:%S')] Sleeping ${INTERVAL}s..."
    sleep "$INTERVAL"
done
