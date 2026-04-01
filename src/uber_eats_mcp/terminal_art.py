"""
ASCII banners for CLI output.

MCP servers use stdio JSON-RPC: never print to stdout except the protocol.
Startup art goes to stderr only. Disable with UBEREATS_QUIET=1.
"""

from __future__ import annotations

import sys
from typing import TextIO

# Compact banner when the MCP server starts (stderr).
STARTUP_LINES = (
    r"  ╭────────────────────────────────────────────╮",
    r"  │  uber-eats-mcp  ·  hungry yet?             │",
    r"  ╰────────────────────────────────────────────╯",
    r"      ~     ~     ~",
)

# Shown by setup scripts (stdout).
SETUP_LINES = (
    r"",
    r"  ╔════════════════════════════════════════════╗",
    r"  ║  uber-eats-mcp  ·  setup (deps + browser)  ║",
    r"  ╚════════════════════════════════════════════╝",
    r"",
)


def print_startup_banner(stream: TextIO | None = None) -> None:
    if stream is None:
        stream = sys.stderr
    for line in STARTUP_LINES:
        print(line, file=stream, flush=True)


def print_setup_banner(stream: TextIO | None = None) -> None:
    if stream is None:
        stream = sys.stdout
    for line in SETUP_LINES:
        print(line, file=stream, flush=True)


if __name__ == "__main__":
    print_setup_banner()
