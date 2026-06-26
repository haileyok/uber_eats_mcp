"""
Uber Eats site URLs. The web app often requires a country-language path (e.g. /cl-en);
opening https://www.ubereats.com/ alone can fail or redirect badly in some regions.

Override with UBEREATS_WEB_LOCALE (e.g. us-en, cl-en). Set to empty to use the site root.
"""

from __future__ import annotations

import os

BASE_URL = "https://www.ubereats.com"


def web_locale_path() -> str:
    """Segment after the host, e.g. ``cl-en``, ``us-en``. Empty string = use ``/`` only."""
    return os.environ.get("UBEREATS_WEB_LOCALE", "us-en").strip().strip("/")


def web_home_url() -> str:
    """Landing URL for Playwright (login, address picker, discovery). Includes trailing slash."""
    p = web_locale_path()
    if not p:
        return f"{BASE_URL}/"
    return f"{BASE_URL}/{p}/"
