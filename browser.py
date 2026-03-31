"""
Playwright browser lifecycle management for Uber Eats automation.

Handles launching/reusing Chromium, persisting login sessions,
capturing auth tokens from network requests,
and switching between headed (login) and headless (automation) modes.

API discovery mode writes full request/response payloads to
~/.ubereats-api-log.jsonl so we can reverse-engineer endpoints.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Request,
    Response,
    async_playwright,
)

SESSION_PATH = Path.home() / ".ubereats-session.json"
CONFIG_PATH = Path.home() / ".ubereats-config.json"
API_LOG_PATH = Path.home() / ".ubereats-api-log.jsonl"
BASE_URL = "https://www.ubereats.com"

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
    "origin": "https://www.ubereats.com",
    "referer": "https://www.ubereats.com/",
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
        self._discovery_mode: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

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
        return self._page

    async def launch_with_interception(self, wide: bool = False) -> Page:
        """Launch a headed browser with network interception for token capture."""
        await self.close()
        self._captured_api_calls = []

        width, height = (1280, 900) if wide else (420, 800)

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

        return self._page

    async def launch_discovery(self) -> Page:
        """Launch a headed browser in API discovery mode.

        Like launch_with_interception but also captures full request/response
        bodies for all /_p/api/ and /api/ calls, writing them to API_LOG_PATH.
        Uses a full-size desktop viewport so the user can access all UI elements.
        """
        if API_LOG_PATH.exists():
            API_LOG_PATH.unlink()
        page = await self.launch_with_interception(wide=True)
        self._discovery_mode = True
        return page

    def _is_noise(self, url: str) -> bool:
        return any(pattern in url for pattern in NOISE_PATTERNS)

    def _is_api_call(self, url: str) -> bool:
        return (
            "ubereats.com" in url
            and ("/_p/api/" in url or "/api/" in url)
            and not self._is_noise(url)
        )

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

                if self._is_api_call(url):
                    endpoint = url.split("ubereats.com")[-1].split("?")[0]
                    if endpoint not in [e.get("endpoint") for e in self.config.captured_endpoints]:
                        self.config.captured_endpoints.append({
                            "endpoint": endpoint,
                            "method": request.method,
                        })

            # In discovery mode, capture full payloads for API calls
            if self._discovery_mode and self._is_api_call(url):
                await self._log_api_call(request, response)

        except Exception:
            pass

    async def _log_api_call(self, request: Request, response: Response) -> None:
        """Write a full API call record to the JSONL log file."""
        try:
            endpoint = request.url.split("ubereats.com")[-1].split("?")[0]

            request_body = None
            if request.method == "POST":
                try:
                    raw = request.post_data
                    if raw:
                        request_body = json.loads(raw)
                except Exception:
                    request_body = request.post_data

            response_body = None
            try:
                response_body = await response.json()
            except Exception:
                pass

            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "method": request.method,
                "endpoint": endpoint,
                "url": request.url,
                "request_body": request_body,
                "response_status": response.status,
                "response_body": response_body,
            }

            with open(API_LOG_PATH, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        except Exception:
            pass

    async def close(self) -> None:
        """Tear down browser resources."""
        self._discovery_mode = False
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

    # ------------------------------------------------------------------
    # API discovery log
    # ------------------------------------------------------------------

    @staticmethod
    def get_api_log() -> list[dict[str, Any]]:
        """Read all captured API call records from the log file."""
        if not API_LOG_PATH.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in API_LOG_PATH.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
        return records

    @staticmethod
    def get_api_log_summary() -> dict[str, Any]:
        """Summarize the captured API log: unique endpoints with call counts and sample bodies."""
        records = BrowserManager.get_api_log()
        if not records:
            return {"total_calls": 0, "endpoints": [], "message": "No API calls captured yet."}

        endpoints: dict[str, dict[str, Any]] = {}
        for rec in records:
            ep = rec.get("endpoint", "")
            if ep not in endpoints:
                endpoints[ep] = {
                    "endpoint": ep,
                    "method": rec.get("method", ""),
                    "call_count": 0,
                    "statuses": set(),
                    "sample_request": rec.get("request_body"),
                    "sample_response_keys": None,
                }
            info = endpoints[ep]
            info["call_count"] += 1
            info["statuses"].add(rec.get("response_status", 0))

            resp = rec.get("response_body")
            if resp and isinstance(resp, dict) and info["sample_response_keys"] is None:
                info["sample_response_keys"] = list(resp.keys())[:15]

        result_endpoints = []
        for info in sorted(endpoints.values(), key=lambda x: x["call_count"], reverse=True):
            result_endpoints.append({
                "endpoint": info["endpoint"],
                "method": info["method"],
                "call_count": info["call_count"],
                "statuses": sorted(info["statuses"]),
                "sample_request": info["sample_request"],
                "sample_response_keys": info["sample_response_keys"],
            })

        return {
            "total_calls": len(records),
            "unique_endpoints": len(result_endpoints),
            "endpoints": result_endpoints,
        }


manager = BrowserManager()
