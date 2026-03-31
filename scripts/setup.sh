#!/usr/bin/env bash
# One-shot setup after git clone: deps + Playwright Chromium + ready-to-paste MCP config.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "Install uv first: https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi

echo "==> Sync Python dependencies (uber-eats-mcp)..."
uv sync

echo "==> Install Chromium for Playwright (one-time, ~100MB+)..."
uv run playwright install chromium

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup done."
echo ""
echo "  This is an MCP server (Model Context Protocol), not a Cursor extension."
echo "  Add it in Cursor: Settings → MCP → Edit config"
echo "  Or in Claude Code: .mcp.json at your project root"
echo ""
echo "  Paste this (path is already set to this clone):"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
cat <<EOF
{
  "mcpServers": {
    "uber-eats": {
      "command": "uv",
      "args": ["run", "--directory", "$ROOT", "uber-eats-mcp"]
    }
  }
}
EOF
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  If you already have other MCP servers, merge the \"uber-eats\" block"
echo "  into your existing \"mcpServers\" object instead of replacing the file."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
