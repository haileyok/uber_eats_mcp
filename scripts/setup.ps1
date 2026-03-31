# One-shot setup after git clone (Windows). Run: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Install uv first: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
}

Write-Host "==> Sync Python dependencies..."
uv sync

Write-Host "==> Install Chromium for Playwright..."
uv run playwright install chromium

$JsonPath = ($Root.Path -replace '\\', '/')
Write-Host ""
Write-Host "Paste this into Cursor MCP settings or .mcp.json (merge uber-eats into mcpServers):"
Write-Host ""
Write-Host @"
{
  "mcpServers": {
    "uber-eats": {
      "command": "uv",
      "args": ["run", "--directory", "$JsonPath", "uber-eats-mcp"]
    }
  }
}
"@
