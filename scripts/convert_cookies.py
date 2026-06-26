#!/usr/bin/env python3
"""
Convert cookies exported by "Get cookies.txt LOCALLY" Chrome extension
into a Playwright storage_state JSON file at ~/.ubereats-session.json.

Supports both export formats the extension offers:
  - JSON:  array of Chrome cookie objects (name, value, domain, path, expirationDate, secure, ...)
  - Netscape cookies.txt:  tab-separated text format

Usage:
  python scripts/convert_cookies.py <exported_file>

Example:
  # On your laptop: export cookies from the extension while on ubereats.com
  # SCP to cloud:
  scp ubereats.com_cookies.json hailey.coder:~/uber_eats_mcp-cdp/
  # On cloud:
  cd ~/uber_eats_mcp-cdp
  python scripts/convert_cookies.py ubereats.com_cookies.json

The output is written to ~/.ubereats-session.json (overwrites if exists).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SESSION_PATH = Path.home() / ".ubereats-session.json"

# Chrome sameSite numeric values → Playwright string values
_SAMESITE_MAP = {
    "no_restriction": "None",
    "lax": "Lax",
    "strict": "Strict",
    "unspecified": "Lax",
    0: "None",
    1: "Lax",
    2: "Strict",
}


def convert_json_cookie(c: dict) -> dict:
    """Convert a Chrome cookie object to Playwright's cookie format."""
    expires = c.get("expirationDate")
    if expires is not None:
        # Chrome uses seconds since epoch (float). Playwright wants the same.
        expires = float(expires)
        if expires < 0:
            expires = -1  # session cookie in Playwright
    else:
        expires = -1

    same_site = c.get("sameSite", "unspecified")
    same_site = _SAMESITE_MAP.get(same_site, _SAMESITE_MAP.get(str(same_site).lower(), "Lax"))

    return {
        "name": c.get("name", ""),
        "value": c.get("value", ""),
        "domain": c.get("domain", ""),
        "path": c.get("path", "/"),
        "expires": expires,
        "httpOnly": bool(c.get("httpOnly", False)),
        "secure": bool(c.get("secure", False)),
        "sameSite": same_site,
    }


def parse_netscape_txt(text: str) -> list[dict]:
    """Parse Netscape cookies.txt format into Chrome cookie objects."""
    cookies = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path, secure, expiration, name, value = parts[:7]
        cookies.append({
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "secure": secure.upper() == "TRUE",
            "expirationDate": float(expiration) if expiration else -1,
            "httpOnly": False,
            "sameSite": "unspecified",
        })
    return cookies


def detect_and_parse(raw_text: str) -> list[dict]:
    """Detect whether the input is JSON or Netscape format and parse accordingly."""
    stripped = raw_text.strip()
    if stripped.startswith("["):
        # JSON array of cookie objects
        data = json.loads(stripped)
        if isinstance(data, list):
            return data
        raise ValueError("Expected a JSON array of cookie objects.")
    if stripped.startswith("{"):
        # Could be a single cookie object or a Playwright storage_state
        data = json.loads(stripped)
        if isinstance(data, dict):
            if "cookies" in data:
                # Already Playwright storage_state format
                print("File is already in Playwright storage_state format — copying as-is.")
                return data["cookies"]
            return [data]
    # Assume Netscape cookies.txt format
    return parse_netscape_txt(raw_text)


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <exported_cookies_file>", file=sys.stderr)
        sys.exit(1)

    input_path = Path(sys.argv[1])
    if not input_path.exists():
        print(f"Error: {input_path} not found.", file=sys.stderr)
        sys.exit(1)

    raw = input_path.read_text(encoding="utf-8")
    cookies_raw = detect_and_parse(raw)

    if not cookies_raw:
        print("Error: No cookies found in the file.", file=sys.stderr)
        sys.exit(1)

    # Filter to Uber-related cookies only (in case the export includes other domains).
    uber_cookies = [
        c for c in cookies_raw
        if "ubereats" in c.get("domain", "").lower() or "uber" in c.get("domain", "").lower()
    ]
    if uber_cookies:
        cookies_raw = uber_cookies
        print(f"Filtered to {len(uber_cookies)} Uber-related cookies.")

    # Convert to Playwright format.
    pw_cookies = [convert_json_cookie(c) for c in cookies_raw]

    storage_state = {
        "cookies": pw_cookies,
        "origins": [],
    }

    SESSION_PATH.write_text(json.dumps(storage_state, indent=2))
    print(f"Wrote {len(pw_cookies)} cookies to {SESSION_PATH}")
    print("The MCP server will auto-load these into Chrome's cookie jar on startup.")


if __name__ == "__main__":
    main()
