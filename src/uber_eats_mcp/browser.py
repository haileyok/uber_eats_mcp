"""
Playwright browser lifecycle management for Uber Eats automation.

Handles launching/reusing Chromium, persisting login sessions,
capturing auth tokens from network requests,
and switching between headed (login) and headless (automation) modes.

Headed tools (login, set_address, …) share one window size. Override with
UBEREATS_BROWSER_VIEWPORT=WxH (e.g. 1100x800) if the default feels too large.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)

from .urls import BASE_URL, web_home_url

SESSION_PATH = Path.home() / ".ubereats-session.json"
CONFIG_PATH = Path.home() / ".ubereats-config.json"

_DEFAULT_VIEWPORT_W, _DEFAULT_VIEWPORT_H = 1280, 900


def headed_browser_viewport() -> tuple[int, int]:
    """Window + viewport size for headed interception (login, discovery, set_address, …)."""
    raw = os.environ.get("UBEREATS_BROWSER_VIEWPORT", "").strip().lower().replace("*", "x")
    if "x" in raw:
        try:
            a, b = raw.split("x", 1)
            w, h = int(a.strip()), int(b.strip())
            return max(320, min(w, 4096)), max(240, min(h, 2304))
        except ValueError:
            pass
    return _DEFAULT_VIEWPORT_W, _DEFAULT_VIEWPORT_H


NOISE_PATTERNS = frozenset([
    "/_events",
    "/pagead/",
    "/analytics",
    "/collect?",
    "google.com",
    "google.cl",
    "bing.com",
    "yahoo.com",
    "facebook.com",
    "doubleclick.net",
    "/sp.pl",
    "bat.bing",
    "/ramendca/",
    "/ramenphx/",
])

DEFAULT_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": BASE_URL,
    "referer": web_home_url(),
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "x-csrf-token": "x",
}


class UberEatsConfig:
    """Persistent config holding captured auth tokens and user info."""

    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.csrf_token: str = ""
        self.sid: str = ""
        self.user_id: str = ""
        self.user_name: str = ""
        self.user_email: str = ""
        self.address: str = ""
        self.lat: float = 0.0
        self.lng: float = 0.0
        self.captured_endpoints: list[dict[str, str]] = []

    def save(self) -> None:
        data = {
            "cookies": self.cookies,
            "csrf_token": self.csrf_token,
            "sid": self.sid,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "user_email": self.user_email,
            "address": self.address,
            "lat": self.lat,
            "lng": self.lng,
            "captured_endpoints": self.captured_endpoints[-50:],
        }
        CONFIG_PATH.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls) -> Optional["UberEatsConfig"]:
        if not CONFIG_PATH.exists():
            return None
        try:
            data = json.loads(CONFIG_PATH.read_text())
            cfg = cls()
            cfg.cookies = data.get("cookies", {})
            cfg.csrf_token = data.get("csrf_token", "")
            cfg.sid = data.get("sid", "")
            cfg.user_id = data.get("user_id", "")
            cfg.user_name = data.get("user_name", "")
            cfg.user_email = data.get("user_email", "")
            cfg.address = data.get("address", "")
            cfg.lat = data.get("lat", 0.0)
            cfg.lng = data.get("lng", 0.0)
            cfg.captured_endpoints = data.get("captured_endpoints", [])
            return cfg
        except Exception:
            return None

    @property
    def is_valid(self) -> bool:
        return bool(self.sid or self.cookies)


class BrowserManager:
    """Manages a single Playwright browser instance across tool invocations."""

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self.config: UberEatsConfig = UberEatsConfig.load() or UberEatsConfig()
        self._captured_api_calls: list[dict[str, Any]] = []
        # True when the active page was created via launch_with_interception.
        self._headed_intercept_active: bool = False
        # Serialize close / launch / login so concurrent MCP tools cannot tear down the page mid-login.
        self._lifecycle_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def exclusive_browser_session(self) -> AsyncIterator[BrowserManager]:
        """Hold the browser lock for a whole flow (e.g. login). Caller must use _close_unlocked / _launch_* only."""
        async with self._lifecycle_lock:
            yield self

    async def _close_unlocked(self) -> None:
        """Tear down browser. Caller must hold _lifecycle_lock (or use close())."""
        self._headed_intercept_active = False
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._page = None

    async def _launch_intercepted_page_unlocked(self) -> Page:
        """Start headed Chromium + interception. Caller must hold _lifecycle_lock."""
        await self._close_unlocked()
        self._captured_api_calls = []

        width, height = headed_browser_viewport()

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                f"--window-size={width},{height}",
            ],
        )

        storage_state = str(SESSION_PATH) if SESSION_PATH.exists() else None

        self._context = await self._browser.new_context(
            storage_state=storage_state,
            viewport={"width": width, "height": height},
            user_agent=DEFAULT_HEADERS["user-agent"],
        )
        self._page = await self._context.new_page()

        self._page.on("response", self._on_response)

        self._headed_intercept_active = True
        return self._page

    async def ensure_interactive_page(self) -> Page:
        """Headed Chromium with response interception.

        Reuses the current page if it was already opened on this stack (avoids extra restarts).
        """
        async with self._lifecycle_lock:
            if self._headed_intercept_active and self._page and not self._page.is_closed():
                return self._page
            return await self._launch_intercepted_page_unlocked()

    async def launch(self, headless: bool = True) -> Page:
        """Launch (or reuse) a browser and return the active page."""
        if self._page and not self._page.is_closed():
            return self._page

        self._playwright = await async_playwright().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
        ]

        storage_state = str(SESSION_PATH) if SESSION_PATH.exists() else None

        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=launch_args,
        )
        self._context = await self._browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1280, "height": 900},
            user_agent=DEFAULT_HEADERS["user-agent"],
        )
        self._page = await self._context.new_page()
        self._headed_intercept_active = False
        return self._page

    async def launch_with_interception(self) -> Page:
        """Launch a headed browser with network interception."""
        async with self._lifecycle_lock:
            return await self._launch_intercepted_page_unlocked()

    def _is_noise(self, url: str) -> bool:
        return any(pattern in url for pattern in NOISE_PATTERNS)

    async def _on_response(self, response: Response) -> None:
        """Intercept network responses to capture auth tokens, API endpoints, and full payloads."""
        url = response.url
        request = response.request

        if "uber" not in url or self._is_noise(url):
            return

        try:
            headers = request.headers
            entry = {
                "url": url,
                "method": request.method,
                "status": response.status,
            }
            self._captured_api_calls.append(entry)

            if response.status == 200:
                csrf = headers.get("x-csrf-token", "")
                if csrf and csrf != "x":
                    self.config.csrf_token = csrf

                set_cookies = await response.header_values("set-cookie")
                for cookie_str in set_cookies:
                    if "sid=" in cookie_str:
                        sid = cookie_str.split("sid=")[1].split(";")[0]
                        if sid:
                            self.config.sid = sid

                if any(kw in url for kw in ["/me", "/profile", "/v1/auth", "/eater"]):
                    try:
                        body = await response.json()
                        if isinstance(body, dict):
                            for key in ("userUuid", "uuid", "user_id", "userId"):
                                if body.get(key):
                                    self.config.user_id = str(body[key])
                            for key in ("firstName", "first_name", "name"):
                                if body.get(key):
                                    self.config.user_name = str(body[key])
                            for key in ("email",):
                                if body.get(key):
                                    self.config.user_email = str(body[key])
                    except Exception:
                        pass

                if any(kw in url for kw in ["/address", "/location", "/places"]):
                    try:
                        body = await response.json()
                        if isinstance(body, dict):
                            addr = body.get("address") or body.get("streetAddress", "")
                            if addr:
                                self.config.address = str(addr)
                            lat = body.get("latitude") or body.get("lat")
                            lng = body.get("longitude") or body.get("lng")
                            if lat and lng:
                                self.config.lat = float(lat)
                                self.config.lng = float(lng)
                    except Exception:
                        pass

                if "ubereats.com" in url and ("/_p/api/" in url or "/api/" in url):
                    endpoint = url.split("ubereats.com")[-1].split("?")[0]
                    if endpoint not in [e.get("endpoint") for e in self.config.captured_endpoints]:
                        self.config.captured_endpoints.append({
                            "endpoint": endpoint,
                            "method": request.method,
                        })

        except Exception:
            pass

    async def close(self) -> None:
        """Tear down browser resources."""
        async with self._lifecycle_lock:
            await self._close_unlocked()

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    async def save_session(self) -> str:
        """Persist cookies / localStorage so future runs skip login."""
        if not self._context:
            return "No browser context to save."

        state = await self._context.storage_state()
        SESSION_PATH.write_text(json.dumps(state, indent=2))

        cookies = await self._context.cookies()
        for cookie in cookies:
            self.config.cookies[cookie["name"]] = cookie["value"]
            if cookie["name"] == "sid":
                self.config.sid = cookie["value"]

        self.config.captured_endpoints = self._captured_api_calls[-50:]
        self.config.save()

        return f"Session saved. Config saved to {CONFIG_PATH}"

    def has_session(self) -> bool:
        return SESSION_PATH.exists()

    def clear_session(self) -> str:
        cleared = []
        if SESSION_PATH.exists():
            SESSION_PATH.unlink()
            cleared.append("session")
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
            cleared.append("config")
        self.config = UberEatsConfig()
        return f"Cleared: {', '.join(cleared)}" if cleared else "Nothing to clear."

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def page(self) -> Optional[Page]:
        if self._page and not self._page.is_closed():
            return self._page
        return None

    async def ensure_page(self, headless: bool = True) -> Page:
        """Return existing page or launch a new browser."""
        if self.page:
            return self.page
        return await self.launch(headless=headless)

    def is_logged_in(self) -> bool:
        return self.has_session() and (self.config.is_valid or SESSION_PATH.exists())


manager = BrowserManager()
