"""
Uber Eats operations: login, search, menu, cart, checkout, orders.

Uses direct HTTP API calls for most operations (fast, reliable).
Browser (Playwright) is used for login and optional place_order fallback.
Cart add/remove and item options use the web JSON API (addItemsToDraftOrderV2, etc.).
Submit uses checkoutOrdersByDraftOrdersV1 when API path succeeds; set UBEREATS_PLACE_ORDER_BROWSER_ONLY=1 to force the browser button.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Page, TimeoutError as PwTimeout

from . import api
from . import cart_api
from . import preferences
from .browser import UberEatsConfig, manager
from .urls import web_home_url, web_locale_path

TIMEOUT = 12_000

LOGIN_DEBUG_PATH = Path.home() / ".ubereats-login-debug.jsonl"
# Human-readable timeline for one login attempt (tail -f in another terminal). No cookie values.
LOGIN_TRACE_PATH = Path.home() / ".ubereats-mcp-login-trace.jsonl"
# Copies of session/config before login clears them — restored if login times out or fails.
_PRELOGIN_SESSION_BAK = Path.home() / ".ubereats-session.json.prelogin.bak"
_PRELOGIN_CONFIG_BAK = Path.home() / ".ubereats-config.json.prelogin.bak"


def _login_snapshot_before_clear() -> None:
    """Preserve disk session so a failed login does not wipe a previously working session."""
    if api.SESSION_PATH.exists():
        shutil.copy2(api.SESSION_PATH, _PRELOGIN_SESSION_BAK)
    if api.CONFIG_PATH.exists():
        shutil.copy2(api.CONFIG_PATH, _PRELOGIN_CONFIG_BAK)


def _login_remove_prelogin_backups() -> None:
    _PRELOGIN_SESSION_BAK.unlink(missing_ok=True)
    _PRELOGIN_CONFIG_BAK.unlink(missing_ok=True)


def _login_restore_prelogin_if_any() -> bool:
    """After timeout or failed detection, restore session files from before clear_session."""
    restored = False
    if _PRELOGIN_SESSION_BAK.exists():
        shutil.copy2(_PRELOGIN_SESSION_BAK, api.SESSION_PATH)
        _PRELOGIN_SESSION_BAK.unlink(missing_ok=True)
        restored = True
    if _PRELOGIN_CONFIG_BAK.exists():
        shutil.copy2(_PRELOGIN_CONFIG_BAK, api.CONFIG_PATH)
        _PRELOGIN_CONFIG_BAK.unlink(missing_ok=True)
        restored = True
    if restored:
        manager.config = UberEatsConfig.load() or UberEatsConfig()
    return restored


def _browser_login_debug_enabled() -> bool:
    """Structured login trace for MCP/Claude failures. Disable: UBEREATS_BROWSER_DEBUG=0."""
    v = os.environ.get("UBEREATS_BROWSER_DEBUG", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _login_trace_enabled() -> bool:
    """Append-only login timeline for debugging. Disable: UBEREATS_LOGIN_TRACE=0."""
    v = os.environ.get("UBEREATS_LOGIN_TRACE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _login_trace_reset() -> None:
    if not _login_trace_enabled():
        return
    try:
        LOGIN_TRACE_PATH.write_text("", encoding="utf-8")
    except Exception:
        pass


def _login_trace_url_safe(url: str) -> str:
    """Host + path prefix only (no query tokens)."""
    try:
        u = urlparse(url)
        path = (u.path or "")[:120]
        return f"{u.netloc}{path}" or "(empty)"
    except Exception:
        return "(unparseable-url)"


def _login_trace(event: str, **fields: Any) -> None:
    if not _login_trace_enabled():
        return
    rec: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    try:
        with open(LOGIN_TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _login_trace_storage_audit() -> dict[str, Any]:
    """Re-read session/config from disk for the trace log — counts and flags only, no cookie values."""
    audit: dict[str, Any] = {"session_path": str(api.SESSION_PATH), "config_path": str(api.CONFIG_PATH)}
    sp, cp = api.SESSION_PATH, api.CONFIG_PATH
    if sp.exists():
        try:
            raw = sp.read_text(encoding="utf-8")
            audit["session_file_exists"] = True
            audit["session_file_bytes"] = len(raw.encode("utf-8"))
            data = json.loads(raw)
            cookies = data.get("cookies") or []
            audit["session_cookie_entries"] = len(cookies) if isinstance(cookies, list) else 0
            names = [c.get("name") for c in cookies if isinstance(c, dict)]
            audit["session_includes_sid_cookie"] = any((n or "").lower() == "sid" for n in names)
            audit["uberish_cookie_entries"] = sum(
                1
                for c in cookies
                if isinstance(c, dict)
                and (
                    "uber" in (c.get("domain") or "").lower()
                    or "ubereats" in (c.get("domain") or "").lower()
                )
            )
        except Exception as e:
            audit["session_file_exists"] = True
            audit["session_read_error"] = str(e)
    else:
        audit["session_file_exists"] = False

    if cp.exists():
        try:
            raw_c = cp.read_text(encoding="utf-8")
            audit["config_file_exists"] = True
            audit["config_file_bytes"] = len(raw_c.encode("utf-8"))
            cfg = json.loads(raw_c)
            audit["config_has_sid_field"] = bool(str(cfg.get("sid") or "").strip())
            audit["config_has_csrf_field"] = bool(str(cfg.get("csrf_token") or "").strip())
            audit["config_user_id_nonempty"] = bool(str(cfg.get("user_id") or "").strip())
        except Exception as e:
            audit["config_file_exists"] = True
            audit["config_read_error"] = str(e)
    else:
        audit["config_file_exists"] = False

    return audit


def _login_debug_reset() -> None:
    if not _browser_login_debug_enabled():
        return
    try:
        LOGIN_DEBUG_PATH.write_text("", encoding="utf-8")
    except Exception:
        pass


def _login_debug_log(event: str, **fields: Any) -> None:
    if not _browser_login_debug_enabled():
        return
    rec: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    try:
        with open(LOGIN_DEBUG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


async def _login_cookie_summary(page: Page) -> dict[str, Any]:
    """Uber-related cookie names only (no values)."""
    uber_names: list[str] = []
    sid_in_jar = False
    try:
        for c in await page.context.cookies():
            dom = (c.get("domain") or "").lower().lstrip(".")
            if "ubereats.com" not in dom and "uber.com" not in dom:
                continue
            n = c.get("name") or ""
            uber_names.append(n)
            if n.lower() == "sid" and (c.get("value") or "").strip():
                sid_in_jar = True
        return {
            "uber_cookie_names": sorted(set(uber_names)),
            "uber_cookie_count": len(uber_names),
            "sid_present_in_jar": sid_in_jar,
        }
    except Exception as e:
        return {"cookie_summary_error": str(e)}


# ── helpers ──────────────────────────────────────────────────────────────────

def _has_auth_signals() -> bool:
    """Check if we've captured auth tokens/session from network traffic."""
    cfg = manager.config
    return bool(cfg.sid or cfg.user_id or cfg.cookies.get("sid"))


async def _sync_session_hints_from_browser(page: Page) -> None:
    """Refresh config from Playwright's cookie jar (HttpOnly-safe).

    Login used to rely on Set-Cookie headers in fetch responses; Uber often sets `sid`
    only on the real browser cookie store, so the wait loop never saw `_has_auth_signals`.
    """
    cfg = manager.config
    try:
        for c in await page.context.cookies():
            dom = (c.get("domain") or "").lower().lstrip(".")
            if "ubereats.com" not in dom and "uber.com" not in dom:
                continue
            name, val = (c.get("name") or ""), (c.get("value") or "")
            if not name:
                continue
            cfg.cookies[name] = val
            if name.lower() == "sid" and val:
                cfg.sid = val
    except Exception:
        pass


async def _check_login_dom(page: Page) -> bool:
    """DOM heuristic: no sign-in links visible."""
    try:
        sign_in = page.get_by_role("link", name=re.compile(r"sign\s*in|log\s*in", re.I))
        count = await sign_in.count()
        return count == 0
    except Exception:
        return True


# ── Login (browser only) ────────────────────────────────────────────────────


async def _login_page_wait(page: Page, ms: int, *, trace_label: str = "") -> bool:
    """Sleep on the page; return False if the browser was closed (user or concurrent tool)."""
    try:
        if page.is_closed():
            _login_trace("page_wait_skipped_closed", ms=ms, label=trace_label)
            return False
        await page.wait_for_timeout(ms)
        ok = not page.is_closed()
        if not ok:
            _login_trace("page_closed_after_wait", ms=ms, label=trace_label)
        return ok
    except Exception as e:
        _login_debug_log("page_wait_failed", ms=ms, error=str(e))
        _login_trace("page_wait_exception", ms=ms, label=trace_label, error=str(e))
        return False


async def _login_session_valid_probe() -> bool:
    """True if ~/.ubereats-session.json authenticates a lightweight Uber API call."""
    if not api.SESSION_PATH.exists():
        return False
    raw = await api.get_saved_addresses()
    return "error" not in raw


async def login(*, force: bool = False) -> dict[str, Any]:
    """Open a headed browser for the user to log in. Captures session via network interception.

    When *force* is False (default), skips the browser entirely if the saved session already
    passes an API probe—avoids clear_session + restore cycles that look like “magic” re-login.
    Set *force* True to wipe disk session and sign in again (e.g. switch Uber account).
    """
    _login_debug_reset()
    _login_trace_reset()
    _login_trace(
        "login_invoke",
        web_locale=web_locale_path() or "(site_root)",
        trace_file=str(LOGIN_TRACE_PATH),
        force=force,
    )
    _login_debug_log(
        "login_start",
        hint="UBEREATS_BROWSER_DEBUG=0 to disable this log",
        log_file=str(LOGIN_DEBUG_PATH),
        force=force,
    )
    if _browser_login_debug_enabled():
        print(
            f"\n>>> Login debug trace: {LOGIN_DEBUG_PATH} (set UBEREATS_BROWSER_DEBUG=0 to disable)\n",
            file=sys.stderr,
        )
    if _login_trace_enabled():
        print(
            f"\n>>> Login timeline (other terminal): tail -f {LOGIN_TRACE_PATH}\n"
            f"    (disable: UBEREATS_LOGIN_TRACE=0)\n",
            file=sys.stderr,
        )

    if not force and await _login_session_valid_probe():
        _login_trace("login_skipped_session_already_valid")
        _login_debug_log("login_skipped", reason="session_probe_ok")
        return {
            "status": "success",
            "message": (
                "Already signed in: your saved session works for Uber’s API, so the login "
                "browser was not opened."
            ),
            "skipped_browser": True,
            "assistant_hint": (
                "The assistant may have assumed you were logged out because another call failed "
                "(transient API error, wrong tool, or missing config). Prefer uber_eats_whoami / "
                "uber_eats_saved_addresses before login. Use force=true only to re-authenticate."
            ),
        }

    async with manager.exclusive_browser_session():
        try:
            return await _login_run_inner()
        except Exception as e:
            # Any unexpected error after clear_session would leave the user with no session file.
            _login_restore_prelogin_if_any()
            _login_debug_log("login_unhandled_exception", error=str(e), traceback=traceback.format_exc())
            _login_trace(
                "login_unhandled_exception",
                error=str(e),
                exc_type=type(e).__name__,
                session_restored=api.SESSION_PATH.exists(),
            )
            raise
        finally:
            await manager._close_unlocked()


async def _login_run_inner() -> dict[str, Any]:
    """Body of login while holding exclusive_browser_session (browser lock)."""
    had_session = api.SESSION_PATH.exists()
    had_config = api.CONFIG_PATH.exists()
    _login_trace("pre_clear", session_file_on_disk=had_session, config_file_on_disk=had_config)
    _login_snapshot_before_clear()
    manager.clear_session()
    _login_debug_log("after_clear_session", cleared=True)
    _login_trace("after_clear_session")
    await manager._close_unlocked()
    await manager._launch_intercepted_page_unlocked()
    page = manager._page
    if not page:
        session_restored = _login_restore_prelogin_if_any()
        _login_trace("no_page_after_launch", session_restored=session_restored)
        return {
            "status": "error",
            "message": "Could not open a browser page for login.",
            "session_restored": session_restored,
            "assistant_hint": "Try again; ensure no other Uber Eats MCP browser flow is stuck.",
            "trace_file": str(LOGIN_TRACE_PATH) if _login_trace_enabled() else "",
        }
    _login_debug_log("browser_ready", headed_intercept=True)
    _login_trace("browser_ready")

    try:
        target = web_home_url()
        _login_trace("goto_start", target=target)
        await page.goto(target, wait_until="domcontentloaded")
        _login_debug_log("after_goto", url=page.url, wait="domcontentloaded")
        _login_trace("goto_ok", page_url=_login_trace_url_safe(page.url))
    except Exception as e:
        _login_debug_log("goto_failed", error=str(e), traceback=traceback.format_exc())
        session_restored = _login_restore_prelogin_if_any()
        _login_trace("goto_failed", error=str(e), session_restored=session_restored)
        return {
            "status": "error",
            "message": f"Could not load Uber Eats in the browser: {e}",
            "session_restored": session_restored,
            "assistant_hint": "Network, locale URL (UBEREATS_WEB_LOCALE), or Playwright issue. Prior session was restored if a backup existed.",
            "trace_file": str(LOGIN_TRACE_PATH) if _login_trace_enabled() else "",
        }

    if not await _login_page_wait(page, 2000, trace_label="after_goto"):
        _login_debug_log("browser_closed_early", phase="after_goto")
        session_restored = _login_restore_prelogin_if_any()
        _login_trace("browser_closed_early_after_goto", session_restored=session_restored)
        return {
            "status": "error",
            "message": (
                "The login browser closed before you could finish (or another tool restarted the browser). "
                "Run login only once at a time and keep the Chromium window open."
            ),
            "session_restored": session_restored,
            "assistant_hint": "Do not start a second login or address/discovery browser until the first finishes.",
            "trace_file": str(LOGIN_TRACE_PATH) if _login_trace_enabled() else "",
        }

    sign_in = page.get_by_role("link", name=re.compile(r"sign\s*in|log\s*in|iniciar", re.I))
    try:
        await sign_in.first.click(timeout=5000)
        _login_debug_log("sign_in_click", ok=True)
        _login_trace("sign_in_click", ok=True)
    except Exception as e:
        _login_debug_log("sign_in_click", ok=False, error=str(e))
        _login_trace("sign_in_click", ok=False, error=str(e))

    print(
        "\n>>> Browser opened. Please log in to Uber Eats.\n"
        ">>> The window will close automatically after login.\n",
        file=sys.stderr,
    )

    logged_in = False
    page_closed_during_login = False
    timeout_ms = 3 * 60 * 1000
    start = asyncio.get_event_loop().time()
    loop_n = 0

    while not logged_in and (asyncio.get_event_loop().time() - start) * 1000 < timeout_ms:
        if not await _login_page_wait(page, 1500, trace_label="poll_interval"):
            page_closed_during_login = True
            _login_debug_log("browser_closed_mid_login", loop=loop_n)
            break
        loop_n += 1
        try:
            cookies_before = await _login_cookie_summary(page)
            await _sync_session_hints_from_browser(page)
            cfg = manager.config
            has_auth = _has_auth_signals()
            url = page.url
            on_ubereats = "ubereats.com" in url
            sign_in_count: int | None = None
            try:
                loc = page.get_by_role("link", name=re.compile(r"sign\s*in|log\s*in", re.I))
                sign_in_count = await loc.count()
            except Exception:
                sign_in_count = None

            _login_debug_log(
                "poll",
                loop=loop_n,
                page_url=url,
                on_ubereats_domain=on_ubereats,
                has_auth_signals=has_auth,
                config_sid_len=len(cfg.sid or ""),
                config_user_id_set=bool(cfg.user_id),
                sign_in_link_count=sign_in_count,
                **cookies_before,
            )
            _login_trace(
                "poll",
                loop=loop_n,
                page_url=_login_trace_url_safe(url),
                on_ubereats=on_ubereats,
                has_auth=has_auth,
                config_sid_len=len(cfg.sid or ""),
                sid_in_jar=cookies_before.get("sid_present_in_jar"),
                uber_cookie_count=cookies_before.get("uber_cookie_count"),
                sign_in_link_count=sign_in_count,
            )

            if not has_auth:
                continue

            if on_ubereats and await _check_login_dom(page):
                _login_debug_log("login_detected", via="auth_plus_dom", loop=loop_n)
                _login_trace("login_detected", via="auth_plus_dom", loop=loop_n)
                logged_in = True
                break
            if on_ubereats:
                if not await _login_page_wait(page, 2000, trace_label="before_login_detected_ubereats"):
                    page_closed_during_login = True
                    break
                _login_debug_log("login_detected", via="auth_plus_ubereats_url", loop=loop_n)
                _login_trace("login_detected", via="auth_plus_ubereats_url", loop=loop_n)
                logged_in = True
                break
            _login_debug_log(
                "auth_but_not_ubereats",
                loop=loop_n,
                page_url=url,
                hint="Complete OAuth until you land back on ubereats.com",
            )
        except Exception as e:
            _login_debug_log(
                "poll_error",
                loop=loop_n,
                error=str(e),
                traceback=traceback.format_exc(),
            )
            _login_trace("poll_error", loop=loop_n, error=str(e))

    elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)
    _login_debug_log(
        "loop_exit",
        logged_in=logged_in,
        elapsed_ms=elapsed_ms,
        loops=loop_n,
        page_closed=page_closed_during_login,
    )
    _login_trace(
        "loop_exit",
        logged_in=logged_in,
        elapsed_ms=elapsed_ms,
        loops=loop_n,
        page_closed=page_closed_during_login,
    )

    if page_closed_during_login:
        session_restored = _login_restore_prelogin_if_any()
        _login_trace("page_closed_mid_login_done", session_restored=session_restored)
        return {
            "status": "error",
            "message": (
                "The login window closed while waiting for sign-in. "
                "If Claude tried login twice at once, run a single login and wait for it to finish."
            ),
            "session_restored": session_restored,
            "assistant_hint": "Only one headed browser session at a time; avoid parallel uber_eats_login calls.",
            "trace_file": str(LOGIN_TRACE_PATH) if _login_trace_enabled() else "",
        }

    await _login_page_wait(page, 2000, trace_label="post_loop")
    result = ""
    cfg = manager.config
    session_restored_after_failed_login = False
    if logged_in:
        try:
            result = await manager.save_session()
            _login_remove_prelogin_backups()
            _login_debug_log("save_session", ok=True, message_preview=str(result)[:200])
            _login_trace(
                "save_session_ok",
                session_file_exists=api.SESSION_PATH.exists(),
                config_file_exists=api.CONFIG_PATH.exists(),
            )
            _login_trace("storage_audit_after_save", **_login_trace_storage_audit())
        except Exception as e:
            _login_debug_log("save_session", ok=False, error=str(e), traceback=traceback.format_exc())
            session_restored_after_failed_login = _login_restore_prelogin_if_any()
            _login_debug_log(
                "session_restored_after_save_failure",
                from_prelogin_backup=session_restored_after_failed_login,
            )
            cfg = manager.config
            _login_debug_log(
                "login_end",
                status="save_error",
                session_path=str(Path.home() / ".ubereats-session.json"),
                session_file_exists=api.SESSION_PATH.exists(),
            )
            _login_trace(
                "save_session_failed",
                error=str(e),
                session_restored=session_restored_after_failed_login,
                session_file_exists=api.SESSION_PATH.exists(),
            )
            err_out: dict[str, Any] = {
                "status": "error",
                "message": (
                    f"Signed in but saving the session failed: {e}. "
                    + (
                        "Your previous Uber session file was restored."
                        if session_restored_after_failed_login
                        else "Try login again."
                    )
                ),
                "session_restored": session_restored_after_failed_login,
            }
            if _browser_login_debug_enabled():
                err_out["debug_log"] = str(LOGIN_DEBUG_PATH)
            err_out["assistant_hint"] = (
                "Rare Playwright or disk error during save_session. If a prior session was restored, "
                "try API tools; otherwise run login again."
            )
            if _login_trace_enabled():
                err_out["trace_file"] = str(LOGIN_TRACE_PATH)
            _login_trace("login_done", status="error_save_session")
            return err_out
    else:
        session_restored_after_failed_login = _login_restore_prelogin_if_any()
        _login_debug_log("save_session_skipped", reason="login_not_confirmed")
        _login_trace("save_session_skipped", reason="login_not_confirmed", session_restored=session_restored_after_failed_login)
    cfg = manager.config

    _login_debug_log(
        "login_end",
        status="success" if logged_in else "timeout",
        session_path=str(Path.home() / ".ubereats-session.json"),
        session_file_exists=(Path.home() / ".ubereats-session.json").exists(),
    )

    if not logged_in:
        msg = (
            "Login timed out after 3 minutes. Please try again. "
            "If a browser window opened, sign in within that time; check the dock for a Chromium window behind other apps."
        )
        if session_restored_after_failed_login:
            msg += (
                " Your previous Uber session on this machine was restored—try search or address again without logging in. "
                "Favorites in preferences are stored separately from the Uber login session."
            )
        out_timeout: dict[str, Any] = {
            "status": "timeout",
            "message": msg,
            "session_restored": session_restored_after_failed_login,
            "debug_log": str(LOGIN_DEBUG_PATH) if _browser_login_debug_enabled() else "",
        }
        out_timeout["assistant_hint"] = (
            "get_preferences can show favorites while the Uber cookie session is missing or expired. "
            "Address and cart need a valid ~/.ubereats-session.json (from a successful login in the Playwright window)."
        )
        if _login_trace_enabled():
            out_timeout["trace_file"] = str(LOGIN_TRACE_PATH)
        _login_trace("login_done", status="timeout", session_file_exists=api.SESSION_PATH.exists())
        return out_timeout

    out: dict[str, Any] = {
        "status": "success",
        "message": result,
        "user": {
            "name": cfg.user_name or "(will show after your next browse or order)",
            "email": cfg.user_email or "(will show after your next browse or order)",
            "user_id": cfg.user_id or "(will show after your next browse or order)",
        },
        "address": cfg.address or "(use uber_eats_set_address to configure)",
        "api_endpoints_captured": len(cfg.captured_endpoints),
    }
    if _browser_login_debug_enabled():
        out["debug_log"] = str(LOGIN_DEBUG_PATH)
    if _login_trace_enabled():
        out["trace_file"] = str(LOGIN_TRACE_PATH)
    _login_trace("login_done", status="success", session_file_exists=api.SESSION_PATH.exists())
    return out


# ── Who Am I (API) ──────────────────────────────────────────────────────────

async def whoami() -> dict[str, Any]:
    """Get current user info from saved session + preferences summary."""
    cookies = api._load_cookies()
    if not cookies:
        return {"error": "You’re not signed in yet—open login once and we’re good to go."}

    cfg_data = None
    if api.CONFIG_PATH.exists():
        import json
        try:
            cfg_data = json.loads(api.CONFIG_PATH.read_text())
        except Exception:
            pass

    prefs = preferences.load_preferences()
    prefs_summary = {
        "default_address": prefs.get("default_address") or None,
        "default_payment": prefs.get("default_payment") or None,
        "default_tip_percent": prefs.get("default_tip_percent"),
        "language": prefs.get("language"),
        "has_dietary_info": bool(
            prefs.get("dietary", {}).get("restrictions")
            or prefs.get("dietary", {}).get("avoid")
        ),
        "favorite_restaurants": len(prefs.get("favorites", {}).get("restaurants", [])),
        "favorite_items": len(prefs.get("favorites", {}).get("items", [])),
        "taste_profile_built": bool(prefs.get("taste_profile", {}).get("orders_analyzed")),
    }

    return {
        "logged_in": True,
        "user_name": (cfg_data or {}).get("user_name", "Unknown"),
        "user_email": (cfg_data or {}).get("user_email", "Unknown"),
        "user_id": (cfg_data or {}).get("user_id", "Unknown"),
        "address": (cfg_data or {}).get("address", "Not set"),
        "coordinates": (
            {"lat": cfg_data["lat"], "lng": cfg_data["lng"]}
            if cfg_data and cfg_data.get("lat")
            else None
        ),
        "preferences": prefs_summary,
        "assistant_hint": f"session_saved={api.SESSION_PATH.exists()}",
    }


# ── Search (API) ────────────────────────────────────────────────────────────

async def search_restaurants(query: str) -> dict[str, Any]:
    """Search Uber Eats: stores plus catalog item rows (product-style results)."""
    q = (query or "").strip()
    merged: dict[str, Any]

    if q:
        raw_search = await api.get_search_feed_v1(q)
        if "error" not in raw_search:
            merged = api.parse_feed_search_results(raw_search)
            if not merged.get("error") and (
                (merged.get("stores") or []) or (merged.get("items") or [])
            ):
                raw_feed = await api.get_feed(query=q)
                if "error" not in raw_feed:
                    feed_parsed = api.parse_feed_search_results(raw_feed)
                    if not feed_parsed.get("error"):
                        merged = api.merge_parsed_search_results(merged, feed_parsed)
            else:
                raw = await api.get_feed(query=q)
                if "error" in raw:
                    return {"stores": [], "items": [], "error": raw["error"]}
                merged = api.parse_feed_search_results(raw)
        else:
            raw = await api.get_feed(query=q)
            if "error" in raw:
                return {"stores": [], "items": [], "error": raw["error"]}
            merged = api.parse_feed_search_results(raw)
    else:
        raw = await api.get_feed(query="")
        if "error" in raw:
            return {"stores": [], "items": [], "error": raw["error"]}
        merged = api.parse_feed_search_results(raw)
    if merged.get("error"):
        return merged

    stores = merged.get("stores") or []
    items = merged.get("items") or []
    if not stores and not items:
        return {
            "stores": [],
            "items": [],
            "feed_item_types": merged.get("feed_item_types", []),
            "error": f"No results for '{query}'. Try another term or check delivery address in config.",
            "assistant_hint": merged.get("assistant_hint", ""),
        }
    out = dict(merged)
    out.pop("error", None)
    return out


# ── Nearby Restaurants (API) ─────────────────────────────────────────────────

async def list_nearby_restaurants(limit: int = 15) -> list[dict[str, Any]]:
    """List restaurants on the Uber Eats home feed."""
    raw = await api.get_feed(page_size=min(limit + 20, 80))
    if "error" in raw:
        return [raw]

    stores = api.parse_feed_stores(raw)
    return stores[:limit] if stores else [{"error": "Could not load restaurants from the feed."}]


# ── Restaurant Menu (API) ───────────────────────────────────────────────────

def _extract_uuid(restaurant_url: str) -> str:
    """Extract store UUID from a URL or slug.

    URLs look like: /store/burger-king-la-reina/7V3DvzxDRPKCITaAd5Ch9Q
    The last path segment (after decoding) is a base64-encoded UUID.
    """
    import base64

    parts = restaurant_url.rstrip("/").split("/")
    slug_part = parts[-1].split("?")[0]

    # If it looks like a UUID already, return it
    if len(slug_part) == 36 and slug_part.count("-") == 4:
        return slug_part

    # Try base64 URL-safe decode (Uber uses URL-safe base64 for UUIDs)
    try:
        padded = slug_part + "=" * (4 - len(slug_part) % 4)
        raw_bytes = base64.urlsafe_b64decode(padded)
        import uuid
        return str(uuid.UUID(bytes=raw_bytes))
    except Exception:
        pass

    return slug_part


async def get_restaurant_menu(restaurant_url: str) -> dict[str, Any]:
    """Get full menu for a restaurant via API."""
    store_uuid = _extract_uuid(restaurant_url)
    raw = await api.get_store(store_uuid)
    if "error" in raw:
        return raw
    return api.parse_store_menu(raw)


# ── Item Options (API: getMenuItemV1) ───────────────────────────────────────

async def get_item_options(item_name: str, restaurant_url: str = "") -> dict[str, Any]:
    """Load customization options via getMenuItemV1 (no browser)."""
    if not restaurant_url:
        return {"error": "I need the restaurant link or slug to load options for that item."}

    store_uuid = _extract_uuid(restaurant_url)
    menu = await get_restaurant_menu(restaurant_url)
    if "error" in menu:
        return menu
    sections = menu.get("sections") or []
    catalog_item = cart_api.find_catalog_item_by_name(sections, item_name)
    if not catalog_item:
        return {"error": f"Could not find menu item matching '{item_name}'."}

    detail = await _get_menu_item_detail_for_cart(
        store_uuid,
        catalog_item.get("section_uuid") or "",
        catalog_item.get("subsection_uuid") or "",
        catalog_item.get("uuid") or "",
    )
    if "error" in detail:
        return detail

    groups = cart_api.summarize_customizations_for_options(detail)
    return {
        "item": item_name,
        "option_groups": groups,
        "source": "api",
        "raw_lines": [],
    }


# ── Cart (API: createDraftOrderV2 / addItemsToDraftOrderV2 / removeItemsFromDraftOrderV2) ──

async def _get_menu_item_detail_for_cart(
    store_uuid: str,
    section_uuid: str,
    subsection_uuid: str,
    menu_item_uuid: str,
) -> dict[str, Any]:
    """getMenuItemV1 with subsection fallback (grocery feeds often omit subsection)."""
    sub = (subsection_uuid or "").strip()
    attempts: list[dict[str, Any]] = []

    async def _try(
        *,
        subsection: str,
        dining_mode: str | None = "DELIVERY",
        cb_type: str | None = "EATER_ENDORSED",
    ) -> dict[str, Any]:
        res = await api.get_menu_item_v1(
            store_uuid=store_uuid,
            section_uuid=section_uuid,
            subsection_uuid=subsection,
            menu_item_uuid=menu_item_uuid,
            dining_mode=dining_mode,
            cb_type=cb_type,
        )
        attempts.append(res)
        return res

    # 1) Normal (web often uses DELIVERY, but grocery sometimes uses diningMode=null)
    detail = await _try(subsection=sub)
    if "error" not in detail:
        return detail

    # 2) If subsection is missing, try menuItemUuid as subsection (known web fallback)
    if not sub:
        detail2 = await _try(subsection=menu_item_uuid)
        if "error" not in detail2:
            return detail2

    # 3) Retry with diningMode omitted (captured: diningMode: null)
    detail3 = await _try(subsection=sub or menu_item_uuid, dining_mode=None)
    if "error" not in detail3:
        return detail3

    # 4) Retry without cbType (some verticals don't accept it)
    detail4 = await _try(subsection=sub or menu_item_uuid, dining_mode=None, cb_type=None)
    if "error" not in detail4:
        return detail4

    # Return the first failure (callers already attach assistant_hint).
    return attempts[0] if attempts else {"error": "Failed to load item detail."}


async def _create_draft_order_via_browser(create_body: dict[str, Any]) -> dict[str, Any]:
    """
    createDraftOrderV2 via Playwright's real browser session.

    Uber's anti-bot protection blocks this mutation when sent from httpx (401).
    The browser succeeds because it carries the full live cookie jar.
    We navigate to ubereats.com if not already there, then use page.evaluate
    to make the same fetch call the browser would make natively.
    """
    try:
        page = await manager.ensure_interactive_page()
        if "ubereats.com" not in (page.url or ""):
            await page.goto("https://www.ubereats.com/", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

        result = await page.evaluate("""async (body) => {
            try {
                const resp = await fetch('/_p/api/createDraftOrderV2?localeCode=cl-en', {
                    method: 'POST',
                    headers: {'content-type': 'application/json', 'x-csrf-token': 'x'},
                    body: JSON.stringify(body)
                });
                return await resp.json();
            } catch (e) {
                return {status: 'failure', data: {message: String(e), code: 'FETCH_ERROR'}};
            }
        }""", create_body)
        return api._coerce_uber_json_body(result) if isinstance(result, dict) else {"error": "Unexpected browser response"}
    except Exception as exc:
        return {"error": f"Browser createDraftOrder failed: {exc}"}


async def _add_to_cart_browser_fallback(
    item_name: str,
    quantity: int = 1,
    restaurant_url: str | None = None,
) -> dict[str, Any]:
    """Legacy: DOM add-to-cart. Only used when UBEREATS_CART_BROWSER_FALLBACK=1."""
    page = await manager.ensure_interactive_page()

    if restaurant_url:
        if not restaurant_url.startswith("http"):
            restaurant_url = f"{web_home_url().rstrip('/')}/store/{restaurant_url}"
        if "/store/" not in page.url or restaurant_url.split("/store/")[-1] not in page.url:
            await page.goto(restaurant_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

    item_link = page.get_by_text(re.compile(re.escape(item_name), re.I)).first
    try:
        await item_link.click(timeout=TIMEOUT)
    except PwTimeout:
        return {"error": f"Could not find item '{item_name}' on the page."}

    await page.wait_for_timeout(2000)

    if quantity > 1:
        increase_btn = page.locator(
            '[data-testid*="increase"], [aria-label*="increase"], button:has-text("+")'
        )
        for _ in range(quantity - 1):
            try:
                await increase_btn.first.click(timeout=3000)
                await page.wait_for_timeout(300)
            except Exception:
                break

    add_btn = page.get_by_role("button", name=re.compile(
        r"add to (cart|order)|add \d|agregar|añadir", re.I
    ))
    try:
        await add_btn.first.click(timeout=TIMEOUT)
    except PwTimeout:
        add_btn = page.locator("button").filter(
            has_text=re.compile(r"add|agregar|añadir", re.I)
        )
        try:
            await add_btn.first.click(timeout=5000)
        except PwTimeout:
            return {
                "error": (
                    f"Found '{item_name}' but could not click 'Add to Cart'. "
                    "The item may have required customizations — "
                    "use uber_eats_get_item_options first."
                ),
            }

    await page.wait_for_timeout(1500)
    return {
        "success": True,
        "item": item_name,
        "quantity": quantity,
        "message": f"Added {item_name} x{quantity} to cart.",
        "source": "browser",
    }


async def add_to_cart(
    item_name: str = "",
    quantity: int = 1,
    restaurant_url: str | None = None,
    store_uuid: str = "",
    section_uuid: str = "",
    subsection_uuid: str = "",
    menu_item_uuid: str = "",
) -> dict[str, Any]:
    """
    Add a line via addItemsToDraftOrderV2 + getMenuItemV1 (no browser by default).

    Optimal path: pass store_uuid + menu_item_uuid. The store menu is loaded to resolve the real
    section_uuid / subsection_uuid (fixes search mistakes where section_uuid was set to the store UUID).

    Optional: section_uuid, subsection_uuid, item_name from search or restaurant_menu.

    Legacy path: restaurant_url + item_name (fuzzy match on the full menu).
    """
    su_p = (store_uuid or "").strip()
    sec_p = (section_uuid or "").strip()
    sub_p = (subsection_uuid or "").strip()
    mu_p = (menu_item_uuid or "").strip()
    if su_p and sec_p and sec_p.lower() == su_p.lower():
        sec_p = ""
    name_q = (item_name or "").strip()
    explicit_ids = bool(su_p and mu_p)

    # Only force browser fallback for the legacy "restaurant_url + item_name" path.
    # If explicit UUIDs are provided, API mutations are more reliable and match discovery traffic.
    if (
        not explicit_ids
        and os.environ.get("UBEREATS_CART_BROWSER_FALLBACK", "").strip() in ("1", "true", "yes")
    ):
        if not name_q or not restaurant_url:
            return {"error": "Browser cart fallback requires item_name and restaurant_url."}
        return await _add_to_cart_browser_fallback(name_q, quantity=quantity, restaurant_url=restaurant_url)

    if explicit_ids:
        store_raw = await api.get_store(su_p)
        if "error" in store_raw:
            return store_raw
        parsed_menu = api.parse_store_menu(store_raw)
        if "error" in parsed_menu:
            return parsed_menu
        data = store_raw.get("data") or {}
        currency = (
            (parsed_menu.get("currency") or data.get("currencyCode") or data.get("currency") or "CLP")
            .strip()
            or "CLP"
        )
        menu: dict[str, Any] = {
            "currency": currency,
            "sections": parsed_menu.get("sections") or [],
            "restaurant": parsed_menu.get("restaurant", ""),
            "uuid": parsed_menu.get("uuid", ""),
        }
        catalog_item = cart_api.catalog_item_from_explicit_uuids(
            menu_item_uuid=mu_p,
            section_uuid=sec_p,
            subsection_uuid=sub_p,
            name_hint=name_q,
        )
        found = cart_api.find_catalog_item_by_menu_item_uuid(menu, mu_p)
        if found:
            catalog_item["section_uuid"] = found.get("section_uuid") or ""
            catalog_item["subsection_uuid"] = found.get("subsection_uuid") or ""
            catalog_item["uuid"] = found.get("uuid") or mu_p
            if not catalog_item.get("name"):
                catalog_item["name"] = found.get("name") or ""
            pc = found.get("price_cents")
            if isinstance(pc, int) and pc:
                catalog_item["price_cents"] = pc
        elif not sec_p or sec_p.lower() == su_p.lower():
            return {
                "error": (
                    "Could not find this product in the store menu (bad or outdated IDs). "
                    "section_uuid must be the catalog section from the menu, not the store UUID."
                ),
                "assistant_hint": (
                    "Call uber_eats_restaurant_menu for this store, or uber_eats_search again; "
                    "copy section_uuid from the item row in items[] (it is never the same as store_uuid)."
                ),
            }
        store_resolved = su_p
    elif restaurant_url and name_q:
        store_resolved = _extract_uuid(restaurant_url)
        menu = await get_restaurant_menu(restaurant_url)
        if "error" in menu:
            return menu
        sections = menu.get("sections") or []
        catalog_item = cart_api.find_catalog_item_by_name(sections, name_q)
        if not catalog_item:
            return {
                "error": f"Could not find menu item matching '{item_name}'. Check spelling or menu.",
                "assistant_hint": (
                    "Re-run uber_eats_restaurant_menu for this URL and copy section_uuid, "
                    "subsection_uuid, and the item uuid into uber_eats_add_to_cart, or paste the exact title."
                ),
            }
    else:
        return {
            "error": (
                "Need either (store_uuid + menu_item_uuid, optional section/subsection/item_name) "
                "or (restaurant_url + item_name)."
            ),
            "assistant_hint": (
                "From uber_eats_search use store_uuid and menu_item_uuid; section_uuid must match the "
                "menu row (never the store UUID). Subsection/item_name optional."
            ),
        }

    detail = await _get_menu_item_detail_for_cart(
        store_resolved,
        catalog_item.get("section_uuid") or "",
        catalog_item.get("subsection_uuid") or "",
        catalog_item.get("uuid") or "",
    )
    if "error" in detail:
        # For some grocery/convenience SKUs, getMenuItemV1 can 404 even though the SKU exists
        # in getStoreV1. In that case, try the web "quick add" multicart createDraftOrderV2
        # shape (no getMenuItemV1 template).
        if explicit_ids and "ITEM_NOT_FOUND" in str(detail.get("error", "")):
            drafts_raw = await api.get_draft_orders()
            if "error" in drafts_raw:
                return drafts_raw
            draft_uuid_existing = cart_api.draft_order_uuid_for_store(drafts_raw, store_resolved)
            line_min = {
                "menuItemUuid": catalog_item.get("uuid") or "",
                "sectionUuid": catalog_item.get("section_uuid") or "",
                "subsectionUuid": catalog_item.get("subsection_uuid") or "",
                "title": catalog_item.get("name") or name_q or "item",
                "customizations": {},
                "imageURL": catalog_item.get("image") or "",
                "specialInstructions": "",
            }
            sci = cart_api.shopping_cart_item_for_create_draft(
                line_min, catalog_item, store_resolved, quantity
            )
            item_label_qa = line_min["title"]
            ids_used_qa = {
                "store_uuid": store_resolved,
                "section_uuid": catalog_item.get("section_uuid") or "",
                "subsection_uuid": catalog_item.get("subsection_uuid") or "",
                "menu_item_uuid": catalog_item.get("uuid") or "",
            }
            if draft_uuid_existing:
                # Cart exists for this store — add to it via addItemsToDraftOrderV2 (quick-add).
                cart_uuid_existing = cart_api.cart_uuid_for_store(drafts_raw, store_resolved)
                add_body = cart_api.build_add_items_body(
                    draft_uuid_existing,
                    store_resolved,
                    [sci],
                    cart_uuid=cart_uuid_existing,
                    is_quick_add=True,
                    is_new_cart_abstraction=True,
                    location_type="GROCERY_STORE",
                )
                added = await api.add_items_to_draft_order_v2(add_body)
                if "error" not in added:
                    return {
                        "success": True,
                        "item": item_label_qa,
                        "quantity": quantity,
                        "message": f"Added {item_label_qa} ×{quantity} to your cart.",
                        "source": "api",
                        "draft_order_uuid": draft_uuid_existing,
                        "note": "Used quick-add flow (item detail endpoint returned not found).",
                        "ids_used": ids_used_qa,
                    }
            else:
                currency = (menu.get("currency") or "").strip() or "CLP"
                payment_profile_uuid = ""
                business_details: dict[str, Any] | None = None
                prof_raw = await api.get_profiles_for_user_v1()
                if "error" not in prof_raw:
                    parsed_prof = api.parse_profiles_for_user(prof_raw)
                    if "error" not in parsed_prof:
                        sel = parsed_prof.get("selected_profile_uuid") or ""
                        for p in parsed_prof.get("profiles") or []:
                            if p.get("uuid") == sel:
                                payment_profile_uuid = p.get("default_payment_profile_uuid") or ""
                                if sel:
                                    business_details = {
                                        "profileType": p.get("type") or "Personal",
                                        "profileUUID": sel,
                                    }
                                break
                create_body = cart_api.build_create_draft_order_multicart_body(
                    [sci],
                    currency_code=currency,
                    payment_profile_uuid=payment_profile_uuid,
                    business_details=business_details,
                    is_quick_add=True,
                    remove_adapters=True,
                )
                created = await api.create_draft_order_v2(create_body)
                if "error" not in created:
                    du = cart_api.parse_create_draft_order_uuid(created)
                    if du:
                        return {
                            "success": True,
                            "item": item_label_qa,
                            "quantity": quantity,
                            "message": f"Added {item_label_qa} ×{quantity} to your cart.",
                            "source": "api",
                            "draft_order_uuid": du,
                            "note": "Used quick-add flow (item detail endpoint returned not found).",
                            "ids_used": ids_used_qa,
                        }

        err = dict(detail)
        err.setdefault(
            "assistant_hint",
            "UUIDs must come from the same store response as getMenuItemV1. "
            "For search hits use uber_eats_add_to_cart with explicit UUID fields.",
        )
        return err

    title = cart_api.title_from_menu_item_detail(detail)
    if title:
        catalog_item["name"] = catalog_item.get("name") or title
    pc = cart_api.price_cents_from_menu_item_detail(detail)
    if pc:
        catalog_item["price_cents"] = pc

    item_label = catalog_item.get("name") or name_q or "item"

    template = cart_api.extract_shopping_cart_line_template(detail)
    if template:
        line = cart_api.merge_catalog_into_line_template(template, catalog_item)
    else:
        line = {
            "uuid": catalog_item["uuid"],
            "menuItemUuid": catalog_item["uuid"],
            "sectionUuid": catalog_item.get("section_uuid") or "",
            "subsectionUuid": catalog_item.get("subsection_uuid") or "",
            "title": catalog_item.get("name") or item_label,
        }
    drafts_raw = await api.get_draft_orders()
    if "error" in drafts_raw:
        return drafts_raw

    draft_uuid = cart_api.draft_order_uuid_for_store(drafts_raw, store_resolved)
    if not draft_uuid:
        currency = (menu.get("currency") or "").strip() or "CLP"
        # Minimal body avoids checkMultipleDraftOrdersCap which Uber enforces at ~9 drafts.
        # Works for both restaurants and grocery stores.
        sci = cart_api.shopping_cart_item_for_create_draft(
            line, catalog_item, store_resolved, quantity,
            price=catalog_item.get("price_cents") or None,
        )
        create_body = {"isMulticart": True, "shoppingCartItems": [sci]}
        created = await api.create_draft_order_v2(create_body)
        if "error" not in created:
            draft_uuid = cart_api.parse_create_draft_order_uuid(created)
            if draft_uuid:
                return {
                    "success": True,
                    "item": item_label,
                    "quantity": quantity,
                    "message": f"Added {item_label} ×{quantity} to your cart.",
                    "source": "api",
                    "draft_order_uuid": draft_uuid,
                    "api_response_keys": list(created.keys()) if isinstance(created, dict) else [],
                    "ids_used": {
                        "store_uuid": store_resolved,
                        "section_uuid": catalog_item.get("section_uuid") or "",
                        "subsection_uuid": catalog_item.get("subsection_uuid") or "",
                        "menu_item_uuid": catalog_item.get("uuid") or "",
                    },
                }
        # httpx is blocked by Uber's anti-bot protection on draft creation (401).
        # Fall back to the same call via Playwright's real browser session.
        browser_created = await _create_draft_order_via_browser(create_body)
        if "error" not in browser_created:
            draft_uuid = cart_api.parse_create_draft_order_uuid(browser_created)
            if draft_uuid:
                return {
                    "success": True,
                    "item": item_label,
                    "quantity": quantity,
                    "message": f"Added {item_label} ×{quantity} to your cart.",
                    "source": "api",
                    "draft_order_uuid": draft_uuid,
                    "ids_used": {
                        "store_uuid": store_resolved,
                        "section_uuid": catalog_item.get("section_uuid") or "",
                        "subsection_uuid": catalog_item.get("subsection_uuid") or "",
                        "menu_item_uuid": catalog_item.get("uuid") or "",
                    },
                }
        return {
            "error": "Couldn't start a cart for this store.",
            "assistant_hint": (
                "createDraftOrderV2 failed via both httpx and browser. "
                "Try uber_eats_login to refresh the session. "
                f"Raw: {str(browser_created.get('error', browser_created))[:300]}"
            ),
        }

    line = cart_api.apply_quantity(line, quantity)
    # addItemsToDraftOrderV2 uses simple int quantity, not the nested itemQuantity object.
    # Browser captures (entry 43/50) always send {"quantity": N} without itemQuantity.
    line = {**line, "quantity": quantity}
    line.pop("itemQuantity", None)
    # Ensure price is set (browser always includes it).
    if not line.get("price") and catalog_item.get("price_cents"):
        line["price"] = catalog_item["price_cents"]

    # Match web mutation shape: include cartUUID when available.
    cart_uuid = ""
    try:
        d_detail = await api.get_draft_order(draft_uuid)
        if "error" not in d_detail:
            d = (d_detail.get("data") or {}).get("draftOrder") or {}
            sc = d.get("shoppingCart") or {}
            cart_uuid = (
                str(sc.get("cartUUID") or sc.get("cartUuid") or sc.get("cartUuid", "") or "")
            )
    except Exception:
        cart_uuid = ""

    # Heuristic: if the user came from store page / restaurant flow (restaurant_url path),
    # do not force grocery-only fields like locationType/isNewCartAbstraction.
    is_quick_add = bool(explicit_ids and not restaurant_url)
    body = cart_api.build_add_items_body(
        draft_uuid,
        store_resolved,
        [line],
        cart_uuid=cart_uuid,
        is_new_cart_abstraction=True if is_quick_add else None,
        location_type="GROCERY_STORE" if is_quick_add else None,
        is_quick_add=is_quick_add,
        num_clicks=None,
        should_update_draft_order_metadata=False,
    )
    added = await api.add_items_to_draft_order_v2(body)
    if "error" in added:
        # Some payloads omit storeUuid at top level
        alt = {"draftOrderUUID": draft_uuid, "items": body.get("items", [])}
        added2 = await api.add_items_to_draft_order_v2(alt)
        if "error" not in added2:
            added = added2
        elif explicit_ids:
            # Existing draft is stale/invalid — create a fresh one via quick-add.
            currency = (menu.get("currency") or "").strip() or "CLP"
            sci_fb = cart_api.shopping_cart_item_for_create_draft(
                line, catalog_item, store_resolved, quantity
            )
            payment_profile_uuid_fb = ""
            business_details_fb: dict[str, Any] | None = None
            prof_raw_fb = await api.get_profiles_for_user_v1()
            if "error" not in prof_raw_fb:
                parsed_fb = api.parse_profiles_for_user(prof_raw_fb)
                if "error" not in parsed_fb:
                    sel_fb = parsed_fb.get("selected_profile_uuid") or ""
                    for p_fb in parsed_fb.get("profiles") or []:
                        if p_fb.get("uuid") == sel_fb:
                            payment_profile_uuid_fb = p_fb.get("default_payment_profile_uuid") or ""
                            if sel_fb:
                                business_details_fb = {
                                    "profileType": p_fb.get("type") or "Personal",
                                    "profileUUID": sel_fb,
                                }
                            break
            create_fb = cart_api.build_create_draft_order_multicart_body(
                [sci_fb],
                currency_code=currency,
                payment_profile_uuid=payment_profile_uuid_fb,
                business_details=business_details_fb,
                is_quick_add=True,
                remove_adapters=True,
            )
            created_fb = await api.create_draft_order_v2(create_fb)
            if "error" not in created_fb:
                du_fb = cart_api.parse_create_draft_order_uuid(created_fb)
                if du_fb:
                    return {
                        "success": True,
                        "item": item_label,
                        "quantity": quantity,
                        "message": f"Added {item_label} ×{quantity} to your cart.",
                        "source": "api",
                        "draft_order_uuid": du_fb,
                        "ids_used": {
                            "store_uuid": store_resolved,
                            "section_uuid": catalog_item.get("section_uuid") or "",
                            "subsection_uuid": catalog_item.get("subsection_uuid") or "",
                            "menu_item_uuid": catalog_item.get("uuid") or "",
                        },
                    }
            return {
                "error": "Couldn't add that to your cart—try signing in again, then we can retry.",
                "assistant_hint": (
                    f"addItemsToDraftOrderV2: {added.get('error', added)}. "
                    "If stuck, capture a browser add in discovery and compare ~/.ubereats-api-log.jsonl."
                ),
            }
        else:
            return {
                "error": "Couldn't add that to your cart—try signing in again, then we can retry.",
                "assistant_hint": (
                    f"addItemsToDraftOrderV2: {added.get('error', added)}. "
                    "If stuck, capture a browser add in discovery and compare ~/.ubereats-api-log.jsonl."
                ),
            }

    if "error" in added:
        return {
            "error": "Couldn't add that to your cart—try signing in again, then we can retry.",
            "assistant_hint": (
                f"addItems failed: {added.get('error')}. "
                "Re-login; optional UBEREATS_CSRF_TOKEN from browser x-csrf-token header."
            ),
        }

    return {
        "success": True,
        "item": item_label,
        "quantity": quantity,
        "message": f"Added {item_label} ×{quantity} to your cart.",
        "source": "api",
        "draft_order_uuid": draft_uuid,
        "api_response_keys": list(added.keys()) if isinstance(added, dict) else [],
        "ids_used": {
            "store_uuid": store_resolved,
            "section_uuid": catalog_item.get("section_uuid") or "",
            "subsection_uuid": catalog_item.get("subsection_uuid") or "",
            "menu_item_uuid": catalog_item.get("uuid") or "",
        },
    }


async def remove_from_cart(item_name: str) -> dict[str, Any]:
    """Remove a cart line by matching title via removeItemsFromDraftOrderV2."""
    drafts_raw = await api.get_draft_orders()
    if "error" in drafts_raw:
        return drafts_raw

    orders = drafts_raw.get("data", {}).get("draftOrders") or []
    if not orders:
        return {"error": "Cart is empty."}

    d0 = orders[0]
    draft_uuid = d0.get("uuid") or ""
    if not draft_uuid:
        return {"error": "No draft order uuid."}

    detail = await api.get_draft_order(draft_uuid)
    if "error" in detail:
        return detail

    parsed = api.parse_draft_order_cart(detail)
    target = cart_api.normalize_name(item_name)
    match_uuids: list[str] = []
    for it in parsed.get("items") or []:
        title = it.get("title") or ""
        if cart_api.normalize_name(title) == target or target in cart_api.normalize_name(title):
            u = it.get("shopping_cart_item_uuid") or it.get("shoppingCartItemUUID")
            if u:
                match_uuids.append(u)

    if not match_uuids:
        return {"error": f"No cart line matching '{item_name}'."}

    # Prefer web-shaped remove body when cartUUID is present.
    d = (detail.get("data") or {}).get("draftOrder") or {}
    sc = d.get("shoppingCart") or {}
    cart_uuid = str(sc.get("cartUUID") or sc.get("cartUuid") or "")
    store_uuid = str(d.get("storeUuid") or d.get("restaurantUUID") or sc.get("storeUuid") or "")
    if cart_uuid and store_uuid:
        rem_body = cart_api.build_remove_items_body_v2(
            cart_uuid=cart_uuid,
            draft_order_uuid=draft_uuid,
            store_uuid=store_uuid,
            shopping_cart_item_uuids=match_uuids[:1],
            location_type="GROCERY_STORE",
        )
    else:
        rem_body = cart_api.build_remove_items_body(draft_uuid, match_uuids[:1])
    rem = await api.remove_items_from_draft_order_v2(rem_body)
    if "error" in rem:
        return rem
    return {
        "success": True,
        "item": item_name,
        "message": f"Removed “{item_name}” from your cart.",
        "source": "api",
    }


async def update_cart_line_quantity(
    quantity: int,
    *,
    item_name: str = "",
    shopping_cart_item_uuid: str = "",
    grocery_store: bool = False,
) -> dict[str, Any]:
    """
    Change how many of one line item are in the cart (updateItemInDraftOrderV2).

    Prefer shopping_cart_item_uuid from uber_eats_view_cart items[] when the user changes
    quantity on a specific line; otherwise match by item_name (substring ok).
    """
    if quantity < 1:
        return {"error": "Quantity must be at least 1."}
    scu = (shopping_cart_item_uuid or "").strip()
    name_q = (item_name or "").strip()
    if not scu and not name_q:
        return {
            "error": "Pass shopping_cart_item_uuid from view_cart, or item_name to match a line.",
            "assistant_hint": "Call uber_eats_view_cart and use items[].shopping_cart_item_uuid.",
        }

    drafts_raw = await api.get_draft_orders()
    if "error" in drafts_raw:
        return drafts_raw

    orders = drafts_raw.get("data", {}).get("draftOrders") or []
    if not orders:
        return {"error": "Cart is empty."}

    hit: dict[str, Any] | None = None
    for d in orders:
        du = d.get("uuid") or ""
        if not du:
            continue
        detail = await api.get_draft_order(str(du))
        if "error" in detail:
            continue
        hit = api.find_shopping_cart_line_raw(
            detail,
            shopping_cart_item_uuid=scu,
            title_query=name_q,
        )
        if hit:
            break

    if not hit:
        return {
            "error": f"Could not find a cart line matching '{name_q or scu}'.",
            "assistant_hint": "Run uber_eats_view_cart and copy shopping_cart_item_uuid or the exact title.",
        }

    line = cart_api.apply_quantity_force(dict(hit["line"]), quantity)
    loc = "GROCERY_STORE" if grocery_store else None
    body = cart_api.build_update_item_in_draft_order_v2_body(
        hit["draft_order_uuid"],
        hit["store_uuid"],
        line,
        location_type=loc,
    )
    out = await api.update_item_in_draft_order_v2(body)
    if "error" in out:
        return {
            **out,
            "assistant_hint": (
                "If this fails repeatedly, capture updateItemInDraftOrderV2 in "
                "~/.ubereats-api-log.jsonl and compare the body shape."
            ),
        }

    title = hit["line"].get("title")
    if isinstance(title, dict):
        label = title.get("text") or "item"
    else:
        label = str(title or "item")

    return {
        "success": True,
        "item": label,
        "quantity": quantity,
        "draft_order_uuid": hit["draft_order_uuid"],
        "shopping_cart_item_uuid": hit["shopping_cart_item_uuid"],
        "message": f"Updated “{label}” to ×{quantity}.",
        "source": "api",
    }


async def _view_cart_browser() -> dict[str, Any]:
    """Open the cart panel and return its contents (DOM scrape)."""
    page = await manager.ensure_interactive_page()

    cart_btn = page.get_by_role("button", name=re.compile(r"cart|carrito|cesta|\d+ item", re.I))
    try:
        await cart_btn.first.click(timeout=TIMEOUT)
    except PwTimeout:
        cart_btn = page.locator('[data-testid*="cart"], [aria-label*="cart"], [aria-label*="Cart"]')
        try:
            await cart_btn.first.click(timeout=5000)
        except PwTimeout:
            return {"items": [], "total": "", "source": "browser", "message": "Could not open cart — it may be empty."}

    await page.wait_for_timeout(2000)

    cart_container = page.locator('[role="dialog"], [data-testid*="cart"], [aria-label*="cart"]')
    try:
        cart_text = await cart_container.first.inner_text(timeout=5000)
    except Exception:
        cart_text = await page.inner_text("body")

    lines = [l.strip() for l in cart_text.splitlines() if l.strip()]

    items: list[dict[str, str]] = []
    charges: list[dict[str, str]] = []
    total = ""
    restaurant = ""

    for line in lines:
        lower = line.lower()
        if re.search(r"^total\b", lower) and re.search(r"[\$€£]\d", line):
            total = line
        elif re.search(r"subtotal|service fee|delivery fee|tax|discount|propina|tip", lower, re.I):
            charges.append({"label": line})
        elif re.search(r"[\$€£]\s*\d", line) and not re.search(r"free delivery|promo", lower):
            items.append({"text": line})
        elif not restaurant and len(line) > 3 and not re.search(r"[\$€£]", line):
            if not re.search(r"cart|your order|checkout|view|item", lower):
                restaurant = restaurant or line

    return {
        "source": "browser",
        "restaurant": restaurant,
        "items": items,
        "charges": charges,
        "total": total,
        "raw_summary": "\n".join(lines[:25]),
    }


async def view_cart() -> dict[str, Any]:
    """Cart contents: prefer draft-order API, else browser cart panel."""
    raw = await api.get_draft_orders()
    if "error" in raw:
        return await _view_cart_browser()

    orders = raw.get("data", {}).get("draftOrders") or []
    if not orders:
        cv = await api.get_carts_view()
        if "error" not in cv:
            cv_p = api.parse_carts_view(cv)
            if cv_p.get("carts"):
                return {
                    "source": "api",
                    "carts": cv_p["carts"],
                    "items": [],
                    "message": "Here's a quick summary of your cart.",
                }
        return {
            "source": "api",
            "items": [],
            "restaurant": "",
            "total": "",
            "message": "Cart is empty.",
        }

    du = orders[0].get("uuid", "")
    detail = await api.get_draft_order(du) if du else {}
    if "error" not in detail:
        parsed = api.parse_draft_order_cart(detail)
    else:
        parsed = api.parse_draft_order_cart(raw)

    cv = await api.get_carts_view()
    cv_p = api.parse_carts_view(cv) if "error" not in cv else {}
    restaurant = ""
    subtotal_line = ""
    delivery_line = ""
    for c in cv_p.get("carts", []):
        if c.get("draft_order_uuid") == parsed.get("draft_order_uuid"):
            restaurant = c.get("restaurant", "")
            subtotal_line = c.get("subtotal_line", "")
            delivery_line = c.get("delivery_line", "")
            break
    if not restaurant and cv_p.get("carts"):
        c0 = cv_p["carts"][0]
        restaurant = c0.get("restaurant", "")
        subtotal_line = c0.get("subtotal_line", "")
        delivery_line = c0.get("delivery_line", "")

    return {
        "source": "api",
        "restaurant": restaurant,
        "items": parsed.get("items", []),
        "subtotal_line": subtotal_line,
        "delivery_line": delivery_line,
        "draft_order_uuid": parsed.get("draft_order_uuid", ""),
        "message": "Here's your cart.",
    }


# ── Checkout (browser fallback) ─────────────────────────────────────────────

async def _checkout_preview_browser() -> dict[str, Any]:
    """Navigate to checkout and return the order summary (DOM). Does NOT place the order."""
    page = await manager.ensure_interactive_page()

    checkout_btn = page.get_by_role("button", name=re.compile(
        r"checkout|go to checkout|pagar|ir a pagar|realizar pedido", re.I
    ))
    try:
        await checkout_btn.first.click(timeout=TIMEOUT)
    except PwTimeout:
        checkout_btn = page.locator("a, button").filter(
            has_text=re.compile(r"checkout|pagar", re.I)
        )
        try:
            await checkout_btn.first.click(timeout=5000)
        except PwTimeout:
            return {"error": "Could not find checkout button. Is there something in your cart?"}

    await page.wait_for_timeout(4000)

    body_text = await page.inner_text("body")
    lines = [l.strip() for l in body_text.splitlines() if l.strip()]

    items: list[str] = []
    charges: list[str] = []
    total = ""
    address = ""
    payment = ""

    for line in lines:
        lower = line.lower()
        if re.search(r"^total\b", lower) and re.search(r"[\$€£]\d", line):
            total = total or line
        elif re.search(r"subtotal|service fee|delivery fee|tax|discount|tip", lower):
            charges.append(line)
        elif re.search(r"deliver to|address|dirección", lower):
            address = address or line
        elif re.search(r"payment|pay with|visa|master|amex|tarjeta", lower):
            payment = payment or line
        elif re.search(r"[\$€£]\s*\d", line) and len(line) < 80:
            items.append(line)

    return {
        "status": "checkout_preview",
        "source": "browser",
        "items": items[:15],
        "charges": charges,
        "total": total,
        "delivery_address": address,
        "payment_method": payment,
        "url": page.url,
        "message": (
            "Walk them through what you see: food, fees, address, then tip and how they want to pay."
        ),
    }


async def checkout_preview() -> dict[str, Any]:
    """Checkout summary: prefer getCheckoutPresentationV1, else browser checkout."""
    raw = await api.get_draft_orders()
    if "error" in raw:
        return await _checkout_preview_browser()

    orders = raw.get("data", {}).get("draftOrders") or []
    if not orders:
        out = await _checkout_preview_browser()
        if isinstance(out, dict) and "error" not in out:
            out["note"] = "Opened checkout in the browser to pull up your summary."
        return out

    du = orders[0].get("uuid", "")
    chk = await api.get_checkout_presentation(du)
    if "error" in chk:
        return await _checkout_preview_browser()

    summary = api.parse_checkout_payloads(chk)
    if "error" in summary:
        return await _checkout_preview_browser()

    pm = summary.get("payment_methods") or []
    msg = (
        "Walk them through this order like a friend: totals, address, fees, then tip. "
        "Ask how they want to pay and how much to tip the driver."
    )
    if not pm:
        msg += (
            " Saved cards didn’t show up in this summary—that happens sometimes; "
            "they’ll usually pick a card when you place the order."
        )
    d0 = orders[0]
    pay_prof = d0.get("paymentProfileUUID") or ""
    total_e5 = int(summary.get("total_amount_e5") or 0)
    sess = summary.get("checkout_session_uuid") or ""
    pre_checkout: dict[str, Any] = {"attempted": False}
    if sess and pay_prof and total_e5 > 0:
        da = d0.get("deliveryAddress") or {}
        addr = da.get("address") or {}
        ac = addr.get("addressComponents") or {}
        cc = ac.get("countryCode")
        use_credits = bool(d0.get("useCredits", True))
        pre_body = api.build_pre_checkout_actions_body(
            checkout_session_uuid=sess,
            payment_profile_uuid=pay_prof,
            order_total_e5=total_e5,
            currency_code=summary.get("currency") or "CLP",
            use_credits=use_credits,
            country_iso2=cc if isinstance(cc, str) else None,
        )
        pre_res = await api.get_pre_checkout_actions(pre_body)
        pre_checkout = {"attempted": True, "response": pre_res}
    elif not sess:
        pre_checkout = {
            "attempted": False,
            "note": "Checkout prep step was skipped—usually still fine to continue and choose payment at the end.",
            "assistant_hint": (
                "No checkoutSessionUUID in presentation; set UBEREATS_CHECKOUT_SESSION_UUID if place_order fails."
            ),
        }
    else:
        pre_checkout = {
            "attempted": False,
            "note": "Almost there—double-check totals before placing.",
            "assistant_hint": "Missing payment profile or total for payments pre-step.",
        }

    return {
        "status": "checkout_preview",
        "source": "api",
        "draft_order_uuid": du,
        "subtotal": summary.get("subtotal", ""),
        "total": summary.get("total", ""),
        "currency": summary.get("currency", ""),
        "fare_breakdown": summary.get("fare_breakdown", []),
        "items": summary.get("cart_items", []),
        "tip_options": summary.get("tip_options", []),
        "delivery_address": summary.get("delivery_address", ""),
        "delivery_address_title": summary.get("delivery_address_title", ""),
        "payment_methods": summary.get("payment_methods", []),
        "checkout_session_uuid": sess,
        "pre_checkout_actions": pre_checkout,
        "message": msg,
    }


async def _place_order_browser() -> dict[str, Any]:
    """Click 'Place Order' on the checkout page. Opens headed browser for visibility."""
    page = await manager.ensure_interactive_page()

    if "checkout" not in page.url:
        return {
            "error": "We need to be on the checkout screen first—pull up a checkout preview, then try placing.",
        }

    place_btn = page.get_by_role("button", name=re.compile(
        r"place order|confirm order|realizar pedido|pedir", re.I
    ))
    try:
        await place_btn.first.click(timeout=TIMEOUT)
    except PwTimeout:
        return {
            "error": (
                "Could not find 'Place Order' button. "
                "The checkout page may need manual interaction — check the browser window."
            ),
        }

    await page.wait_for_timeout(5000)

    body_text = await page.inner_text("body")
    lines = [l.strip() for l in body_text.splitlines() if l.strip()]

    confirmation_keywords = ["confirmed", "on its way", "preparing", "confirmado", "en camino"]
    is_confirmed = any(kw in body_text.lower() for kw in confirmation_keywords)

    return {
        "status": "order_placed" if is_confirmed else "pending",
        "source": "browser",
        "message": "Order placed successfully!" if is_confirmed else "Order submitted. Check the browser for confirmation.",
        "url": page.url,
        "page_summary": "\n".join(lines[:20]),
    }


async def _place_order_via_api() -> dict[str, Any] | None:
    """POST checkoutOrdersByDraftOrdersV1 (+ optional getPreCheckoutActions)."""
    raw = await api.get_draft_orders()
    if "error" in raw:
        return {"error": raw["error"], "try_browser": True}
    orders = raw.get("data", {}).get("draftOrders") or []
    if not orders:
        return {"error": "No draft order to submit.", "try_browser": True}
    du = orders[0].get("uuid") or ""
    if not du:
        return {"error": "Draft order missing uuid.", "try_browser": True}

    dr = await api.get_draft_order(du)
    if "error" in dr:
        return {"error": dr["error"], "try_browser": True}
    draft = dr["data"]["draftOrder"]

    chk = await api.get_checkout_presentation(du)
    if "error" in chk:
        return {"error": chk["error"], "try_browser": True}
    summary = api.parse_checkout_payloads(chk)
    if "error" in summary:
        return {"error": summary["error"], "try_browser": True}

    total_e5 = int(summary.get("total_amount_e5") or 0)
    currency = summary.get("currency") or "CLP"
    pay = draft.get("paymentProfileUUID") or ""
    if not pay:
        return {
            "error": "Choose how you want to pay first, then I can place the order.",
            "try_browser": True,
            "assistant_hint": "No paymentProfileUUID on draft; use uber_eats_set_checkout_payment.",
        }
    if total_e5 <= 0:
        return {
            "error": "I couldn’t read the total—open checkout preview again, or we’ll finish in the browser.",
            "try_browser": True,
            "assistant_hint": "total_amount_e5 missing from checkout presentation.",
        }

    sess = summary.get("checkout_session_uuid") or ""
    use_credits = bool(draft.get("useCredits", True))
    if sess:
        da = draft.get("deliveryAddress") or {}
        addr = da.get("address") or {}
        ac = addr.get("addressComponents") or {}
        cc = ac.get("countryCode")
        pre_body = api.build_pre_checkout_actions_body(
            checkout_session_uuid=sess,
            payment_profile_uuid=pay,
            order_total_e5=total_e5,
            currency_code=currency,
            use_credits=use_credits,
            country_iso2=cc if isinstance(cc, str) else None,
        )
        pre = await api.get_pre_checkout_actions(pre_body)
        if "error" in pre:
            return {"error": pre["error"], "pre_checkout": pre, "try_browser": True}

    co_body = api.build_checkout_orders_request(
        draft,
        order_total_e5=total_e5,
        currency_code=currency,
    )
    out = await api.checkout_orders_by_draft_orders_v1(co_body)
    if "error" in out:
        return {
            "error": out["error"],
            "try_browser": True,
            "checkout_submit_response": out,
        }

    return {
        "status": "order_placed",
        "source": "api",
        "message": "You’re all set—the order went through. They should see confirmation from Uber Eats.",
        "draft_order_uuid": du,
        "submit_response": out,
    }


async def place_order() -> dict[str, Any]:
    """
    Place the order: tries API (checkoutOrdersByDraftOrdersV1) first, then browser click.
    Set UBEREATS_PLACE_ORDER_BROWSER_ONLY=1 to skip API. Set UBEREATS_PLACE_ORDER_NO_BROWSER_FALLBACK=1
    to return API errors without opening the browser.
    """
    browser_only = os.environ.get("UBEREATS_PLACE_ORDER_BROWSER_ONLY", "").strip().lower() in (
        "1", "true", "yes",
    )
    no_fallback = os.environ.get("UBEREATS_PLACE_ORDER_NO_BROWSER_FALLBACK", "").strip().lower() in (
        "1", "true", "yes",
    )

    if not browser_only:
        api_res = await _place_order_via_api()
        if api_res and "error" not in api_res:
            return api_res
        if api_res and "error" in api_res and no_fallback:
            api_res["source"] = "api"
            return api_res

    return await _place_order_browser()


# ── Orders (API) ─────────────────────────────────────────────────────────────

async def track_orders() -> dict[str, Any]:
    """Get past orders via API; includes active orders when available."""
    raw = await api.get_past_orders()
    if "error" in raw:
        return raw

    orders = api.parse_orders(raw)
    active_raw = await api.get_active_orders()
    active_list: list[dict[str, Any]] = []
    if "error" not in active_raw:
        active_list = api.parse_active_orders(active_raw)
    return {"orders": orders[:10], "active_orders": active_list, "source": "api"}


async def track_active_orders() -> dict[str, Any]:
    """Active / in-progress orders only (getActiveOrdersV1)."""
    raw = await api.get_active_orders()
    if "error" in raw:
        return raw
    return {"active_orders": api.parse_active_orders(raw), "source": "api"}


async def reorder_from_past_order(order_uuid: str) -> dict[str, Any]:
    """Resolve store + previous line items for a reorder. Adding items still uses browser until cart API is captured."""
    raw = await api.get_past_orders()
    if "error" in raw:
        return raw
    orders = api.parse_orders(raw)
    match = next((o for o in orders if o.get("uuid") == order_uuid), None)
    if not match:
        return {"error": f"No past order with uuid {order_uuid} in recent history."}
    su = match.get("store_uuid")
    if not su:
        return {
            "error": "Order payload had no store UUID; use uber_eats_search to find the restaurant.",
            "restaurant": match.get("restaurant"),
            "previous_items": match.get("items", []),
        }
    return {
        "order_uuid": order_uuid,
        "restaurant": match.get("restaurant"),
        "store_uuid": su,
        "previous_items": match.get("items", []),
        "message": (
            f"Open the menu for this place (restaurant_url=\"{su}\"), then add what you want to the cart."
        ),
    }


async def get_menu_item_detail(
    store_uuid: str,
    section_uuid: str,
    subsection_uuid: str,
    menu_item_uuid: str,
) -> dict[str, Any]:
    """Full item payload including customizations (getMenuItemV1). IDs come from menu or search items[]."""
    return await _get_menu_item_detail_for_cart(
        store_uuid,
        section_uuid,
        subsection_uuid,
        menu_item_uuid,
    )


async def list_payment_methods() -> dict[str, Any]:
    """Payment methods from checkout eligibility (requires a non-empty cart)."""
    raw = await api.get_draft_orders()
    if "error" in raw:
        return raw
    orders = raw.get("data", {}).get("draftOrders") or []
    if not orders:
        return {
            "error": "Cart’s empty—add something tasty first, then we can pick a card.",
            "payment_methods": [],
        }

    du = orders[0].get("uuid", "")
    chk = await api.get_checkout_presentation(
        du,
        payload_types=[
            "subtotal",
            "total",
            "paymentProfilesEligibility",
            "locationInfo",
        ],
    )
    if "error" in chk:
        return chk
    summary = api.parse_checkout_payloads(chk)
    if "error" in summary:
        return summary
    methods = summary.get("payment_methods") or []
    out: dict[str, Any] = {
        "draft_order_uuid": du,
        "payment_methods": methods,
        "delivery_address": summary.get("delivery_address", ""),
    }
    if not methods:
        out["message"] = (
            "No saved cards listed here—that’s normal sometimes. "
            "They’ll pick how to pay when you confirm the order."
        )
    else:
        out["message"] = "Here are the cards you can use for this order."
    return out


async def search_history() -> dict[str, Any]:
    """Recent search queries from the account (API)."""
    raw = await api.get_search_home_v2()
    if "error" in raw:
        return raw
    return {"history": api.parse_search_history(raw), "source": "api"}


async def apply_promo(code: str) -> dict[str, Any]:
    """Apply a promo code to the current session/cart context."""
    return await api.apply_promo_v1(code)


async def list_uber_profiles() -> dict[str, Any]:
    """Personal / Business / Family Uber profiles and selected profile."""
    raw = await api.get_profiles_for_user_v1()
    if "error" in raw:
        return raw
    return api.parse_profiles_for_user(raw)


async def switch_uber_profile(profile_uuid: str) -> dict[str, Any]:
    """Switch active Uber profile (e.g. Personal vs Business)."""
    return await api.select_profile_v1(profile_uuid)


async def checkout_set_tip(draft_order_uuid: str, tip_percent: int) -> dict[str, Any]:
    """Set upfront tip on the draft order. tip_percent: 0, 5, 10, 15, 20 (must match checkout options)."""
    chk = await api.get_checkout_presentation(
        draft_order_uuid,
        payload_types=["upfrontTipping"],
    )
    if "error" in chk:
        return chk
    parsed = api.parse_checkout_payloads(chk)
    target = tip_percent * 100
    tip_opt = None
    for opt in parsed.get("tip_options", []):
        if opt.get("percent") == target:
            tip_opt = opt
            break
    if not tip_opt:
        return {
            "error": f"No tip option matching {tip_percent}%",
            "tip_options": parsed.get("tip_options", []),
        }

    dr = await api.get_draft_order(draft_order_uuid)
    if "error" in dr:
        return dr
    draft = dr["data"]["draftOrder"]
    body = api.build_update_draft_order_body_from_draft(draft)
    if tip_opt.get("percent") == 0:
        body["upfrontTipOption"] = {
            "amount": {"amountE5": {"low": 0, "high": 0, "unsigned": False}},
            "percent": 0,
        }
    else:
        amt = int(tip_opt.get("amount") or 0)
        body["upfrontTipOption"] = {
            "amount": {"amountE5": {"low": amt, "high": 0, "unsigned": False}},
            "percent": int(tip_opt["percent"]),
        }

    out = await api.update_draft_order_v2(body)
    if "error" in out:
        return out
    return {"success": True, "tip_percent": tip_percent, "response": out}


async def checkout_set_payment_profile(
    draft_order_uuid: str,
    payment_profile_uuid: str,
    *,
    use_credits: bool | None = None,
    set_as_default: bool = False,
) -> dict[str, Any]:
    """Set payment card/profile and sync draft order (selector + updateDraftOrder; optional profilePatch)."""
    sel = await api.post_payment_selector_change(payment_profile_uuid)
    if "error" in sel:
        return sel
    dr = await api.get_draft_order(draft_order_uuid)
    if "error" in dr:
        return dr
    draft = dr["data"]["draftOrder"]
    body = api.build_update_draft_order_body_from_draft(draft)
    body["paymentProfileUUID"] = payment_profile_uuid
    if use_credits is not None:
        body["useCredits"] = use_credits
    out = await api.update_draft_order_v2(body)
    if "error" in out:
        return out
    result: dict[str, Any] = {
        "success": True,
        "payment_profile_uuid": payment_profile_uuid,
        "response": out,
    }
    if set_as_default:
        prof_raw = await api.get_profiles_for_user_v1()
        if "error" in prof_raw:
            result["profile_patch"] = {"error": prof_raw["error"]}
        else:
            parsed = api.parse_profiles_for_user(prof_raw)
            profiles = parsed.get("profiles") or []
            sel_id = parsed.get("selected_profile_uuid", "")
            p = next((x for x in profiles if x.get("uuid") == sel_id), None)
            if p is None and profiles:
                p = profiles[0]
            if p and p.get("uuid"):
                patch = await api.profile_patch_default_payment(
                    profile_uuid=p["uuid"],
                    profile_type=str(p.get("type") or "Personal"),
                    default_payment_profile_uuid=payment_profile_uuid,
                )
                result["profile_patch"] = patch
            else:
                result["profile_patch"] = {"error": "Could not resolve account profile for profilePatch."}
    return result


async def checkout_savings() -> dict[str, Any]:
    """Promo/savings offers at checkout (requires draft order and checkout totals)."""
    raw = await api.get_draft_orders()
    if "error" in raw:
        return raw
    orders = raw.get("data", {}).get("draftOrders") or []
    if not orders:
        return {"error": "No draft order — add items to cart first."}
    d0 = orders[0]
    du = d0.get("uuid", "")
    store = d0.get("storeUuid") or d0.get("restaurantUUID")
    pay = d0.get("paymentProfileUUID")
    if not store or not pay:
        return {"error": "Draft order missing store or payment profile."}

    chk = await api.get_checkout_presentation(du, payload_types=["fareBreakdown", "subtotal"])
    if "error" in chk:
        return chk
    fee = api.parse_delivery_fee_e5_from_checkout(chk)

    return await api.get_savings_v1(
        draft_order_uuid=du,
        store_uuid=store,
        payment_profile_uuid=pay,
        delivery_fee_e5=fee,
    )


# ── Address (API) ────────────────────────────────────────────────────────────

async def list_saved_addresses() -> dict[str, Any]:
    """Get all saved delivery addresses via API."""
    raw = await api.get_saved_addresses()
    if "error" in raw:
        return raw
    return api.parse_saved_addresses(raw)


async def switch_to_address(label: str) -> dict[str, Any]:
    """Switch to a saved address by its label (e.g. 'home', 'work', 'Igal')."""
    raw = await api.get_saved_addresses()
    if "error" in raw:
        return raw

    parsed = api.parse_saved_addresses(raw)
    all_addresses = parsed.get("addresses", {})

    label_lower = label.lower().strip()
    match = None
    for _category, addrs in all_addresses.items():
        for addr in addrs:
            if addr["label"].lower() == label_lower or label_lower in addr["name"].lower():
                match = addr
                break
        if match:
            break

    if not match:
        available = [
            a["label"] or a["name"]
            for addrs in all_addresses.values()
            for a in addrs
            if a["label"]
        ]
        seen = set()
        unique = [x for x in available if x not in seen and not seen.add(x)]
        return {
            "error": f"No saved address matching '{label}'. Available: {', '.join(unique)}",
        }

    result = await api.switch_address(
        place_id=match["place_id"],
        provider=match["provider"],
        latitude=match["latitude"],
        longitude=match["longitude"],
        address=match["full_address"],
    )

    if result.get("status") == "success" or "error" not in result:
        import json
        cookies = api._load_cookies()
        cookies["uev2.loc"] = json.dumps({
            "latitude": match["latitude"],
            "longitude": match["longitude"],
        })

        if api.SESSION_PATH.exists():
            state = json.loads(api.SESSION_PATH.read_text())
            cookie_list = state.get("cookies", [])
            found = False
            for c in cookie_list:
                if c["name"] == "uev2.loc":
                    c["value"] = cookies["uev2.loc"]
                    found = True
                    break
            if not found:
                cookie_list.append({
                    "name": "uev2.loc",
                    "value": cookies["uev2.loc"],
                    "domain": ".ubereats.com",
                    "path": "/",
                })
            state["cookies"] = cookie_list
            api.SESSION_PATH.write_text(json.dumps(state, indent=2))

        if api.CONFIG_PATH.exists():
            cfg = json.loads(api.CONFIG_PATH.read_text())
            cfg["address"] = match["full_address"]
            cfg["lat"] = match["latitude"]
            cfg["lng"] = match["longitude"]
            pid = (match.get("place_id") or "").strip()
            prov = (match.get("provider") or "").strip()
            if pid:
                cfg["place_reference"] = pid
                cfg["place_reference_type"] = prov or "google_places"
            api.CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

        return {
            "success": True,
            "switched_to": match["label"] or match["name"],
            "address": match["full_address"],
            "coordinates": {"lat": match["latitude"], "lng": match["longitude"]},
        }

    return result


async def get_addresses() -> dict[str, Any]:
    """Get the current delivery address from config."""
    import json

    loc = api._load_location()
    cookies = api._load_cookies()

    # Also try the uev2.loc cookie
    loc_cookie = cookies.get("uev2.loc", "")
    cookie_loc = None
    if loc_cookie:
        try:
            cookie_loc = json.loads(loc_cookie)
        except Exception:
            pass

    return {
        "current_address": (loc or {}).get("address", "Not set"),
        "saved_coordinates": (
            {"lat": loc["latitude"], "lng": loc["longitude"]}
            if loc and loc.get("latitude")
            else None
        ),
        "cookie_coordinates": (
            {"lat": cookie_loc.get("latitude"), "lng": cookie_loc.get("longitude")}
            if cookie_loc
            else None
        ),
    }


async def analyze_order_history() -> dict[str, Any]:
    """Fetch past orders and build/update the taste profile from them."""
    raw = await api.get_past_orders()
    if "error" in raw:
        return raw

    orders = api.parse_orders(raw)
    if not orders:
        return {"message": "No past orders found to analyze."}

    taste_profile = preferences.build_taste_profile(orders)
    return {
        "orders_analyzed": len(orders),
        "taste_profile": taste_profile,
        "message": f"Analyzed {len(orders)} orders. Taste profile updated.",
    }


async def set_address(address: str) -> dict[str, Any]:
    """Set delivery address. Uses headed browser for the address picker (Google Places autocomplete)."""
    page = await manager.ensure_interactive_page()
    await page.goto(web_home_url(), wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    # Try the header address picker first, then the landing page combobox
    address_el = page.locator(
        '[data-testid*="address"], [data-testid*="location"], '
        '[aria-label*="address"], [aria-label*="deliver"]'
    )
    clicked_picker = False
    try:
        await address_el.first.click(timeout=5000)
        clicked_picker = True
    except PwTimeout:
        pass

    if clicked_picker:
        await page.wait_for_timeout(2000)

    input_selectors = [
        'input[placeholder*="address" i]',
        'input[placeholder*="dirección" i]',
        'input[placeholder*="Enter" i]',
        'input[placeholder*="deliver" i]',
        'input[type="search"]',
        '[role="combobox"]',
        'input[aria-autocomplete]',
    ]
    address_input = None
    for sel in input_selectors:
        loc = page.locator(sel)
        if await loc.count() > 0:
            address_input = loc.first
            break

    if not address_input:
        return {"error": "Could not find address input field."}

    try:
        await address_input.fill(address, timeout=5000)
    except PwTimeout:
        try:
            await address_input.click(timeout=3000)
            await address_input.fill(address, timeout=5000)
        except PwTimeout:
            return {"error": "Could not type into address field."}

    await page.wait_for_timeout(2500)

    suggestion_selectors = [
        '[role="option"]',
        '[data-testid*="suggestion"]',
        '[data-testid*="address-suggestion"]',
        'ul[role="listbox"] li',
    ]
    suggestion_clicked = False
    for sel in suggestion_selectors:
        loc = page.locator(sel)
        if await loc.count() > 0:
            try:
                await loc.first.click(timeout=5000)
                suggestion_clicked = True
                break
            except Exception:
                continue

    if not suggestion_clicked:
        return {"error": "No address suggestions appeared. Try a more specific address."}

    await page.wait_for_timeout(2000)

    save_btn = page.locator("button").filter(
        has_text=re.compile(r"save|confirm|done|guardar|confirmar|deliver here|find food", re.I)
    )
    try:
        await save_btn.first.click(timeout=5000)
    except Exception:
        pass

    await page.wait_for_timeout(3000)
    await manager.save_session()

    manager.config.address = address
    manager.config.save()

    return {
        "success": True,
        "address": address,
        "message": f"Address updated to: {address}",
    }


