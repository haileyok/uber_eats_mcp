"""
Direct HTTP client for the Uber Eats internal API.

Uses session cookies captured during browser login. No browser needed
for API calls — just httpx with the right cookies and headers.

Endpoint reference (reverse-engineered):
  POST /_p/api/getFeedV1        — home feed + layered search (cacheKey + userQuery)
  POST /_p/api/getSearchFeedV1  — global search bar (SEARCH_BAR / GLOBAL_SEARCH)
  POST /_p/api/getStoreV1       — restaurant detail + full menu
  POST /api/getPastOrdersV1     — order history
  POST /_p/api/setTargetLocationV1
  POST /_p/api/upsertDeliveryLocationV2
  POST /_p/api/getCartsViewForEaterUuidV1
  POST /_p/api/getDraftOrdersByEaterUuidV1
  POST /_p/api/getDraftOrderByUuidV2
  POST /_p/api/getCheckoutPresentationV1
  POST /_p/api/getActiveOrdersV1
  POST /_p/api/updateDraftOrderV2
  POST /_p/api/getSearchHomeV2
  POST /_p/api/getProfilesForUserV1
  POST /_p/api/selectProfileV1
  POST /_p/api/applyPromoV1
  POST /_p/api/getSavingsV1
  POST /_p/api/getMenuItemV1
  POST /_p/api/createDraftOrderV2
  POST /_p/api/updateItemInDraftOrderV2
  POST /_p/api/addItemsToDraftOrderV2
  POST /_p/api/removeItemsFromDraftOrderV2
  POST /_p/api/checkoutOrdersByDraftOrdersV1
  POST payments.ubereats.com/api/getPreCheckoutActions
  POST payments.ubereats.com/api/profilePatch

MCP call trace (optional): each POST is appended as JSON to ~/.ubereats-mcp-api-log.jsonl
(endpoint + url + status_code) for diffing with browser discovery (~/.ubereats-api-log.jsonl).
Set UBEREATS_LOG_API_CALLS=0 to disable. UBEREATS_API_CALL_LOG overrides the file path.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import httpx

from .browser import manager as browser_manager, cdp_enabled
from .urls import BASE_URL, web_home_url

SESSION_PATH = Path.home() / ".ubereats-session.json"
CONFIG_PATH = Path.home() / ".ubereats-config.json"
# Append-only JSONL of MCP HTTP calls (endpoint + URL + status) for diffing vs ~/.ubereats-api-log.jsonl.
# Disable: UBEREATS_LOG_API_CALLS=0. Override path: UBEREATS_API_CALL_LOG=~/.custom.jsonl
API_CALL_LOG_DEFAULT = Path.home() / ".ubereats-mcp-api-log.jsonl"
PAYMENTS_BASE_URL = "https://payments.ubereats.com"
# Uber's own public frontend key, extracted from browser network traffic.
# This is NOT a secret — it's embedded in Uber's web client and safe to commit.
# Override with UBEREATS_PAYMENTS_API_KEY env var if Uber rotates it.
UBER_PUBLIC_PAYMENTS_CLIENT_KEY = "production_u2bkf0z5pn0e552g"
# Uber's own public payments flow identifier, extracted from browser network traffic.
# This is NOT a secret — it's embedded in Uber's web client and safe to commit.
# Override with UBEREATS_PAYMENTS_USE_CASE_KEY env var if Uber rotates it.
UBER_PUBLIC_PAYMENTS_USE_CASE_ID = "ttuwvggglnipzqmqjtqvloykgtworjtsrwzniukw"

# Match browser client; mutations may reject bare "x" CSRF without session cookies.
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "x-csrf-token": "x",
    "user-agent": CHROME_UA,
}


def _load_config() -> dict[str, Any]:
    """Minimal config from ~/.ubereats-config.json (csrf, etc.)."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _csrf_token_value() -> str:
    """CSRF for /_p/api/* — config csrf_token from login capture; cookie fallback; env override."""
    override = os.environ.get("UBEREATS_CSRF_TOKEN", "").strip()
    if override:
        return override
    cfg = _load_config()
    t = (cfg.get("csrf_token") or "").strip()
    if t:
        return t
    cookies = _load_cookies()
    for name in ("csrf", "csrftoken", "ct0", "_csrf"):
        if name in cookies and cookies[name]:
            return cookies[name]
    return "x"


def _api_headers(csrf: str = "") -> dict[str, str]:
    """Build headers for Uber API calls.

    When *csrf* is provided (e.g. read from the live Chrome cookie jar), use it
    instead of the stale disk-based value. Falls back to _csrf_token_value() when
    empty (non-CDP path).
    """
    token = csrf.strip() if csrf else _csrf_token_value()
    return {
        **DEFAULT_HEADERS,
        "origin": BASE_URL,
        "referer": web_home_url(),
        "x-csrf-token": token,
    }


def _coerce_uber_json_body(parsed: Any) -> dict[str, Any]:
    """
    Uber often returns HTTP 200 with {status: 'failure', data: {code, message}}.
    Normalize so callers can use `if 'error' in result`.
    """
    if not isinstance(parsed, dict):
        return {"error": "Invalid API response shape."}
    if parsed.get("status") != "failure":
        return parsed
    data = parsed.get("data") or {}
    msg = data.get("message") or ""
    code = data.get("code") or ""
    detail = f"{code} {msg}".strip() or "unknown"
    out = dict(parsed)
    out["error"] = f"Uber API failure: {detail}"
    return out


def _load_cookies() -> dict[str, str]:
    """Build a cookie dict from the Playwright storage-state file."""
    if not SESSION_PATH.exists():
        return {}
    try:
        state = json.loads(SESSION_PATH.read_text())
        cookies: dict[str, str] = {}
        for c in state.get("cookies", []):
            cookies[c["name"]] = c["value"]
        return cookies
    except Exception:
        return {}


def _load_location() -> dict[str, Any] | None:
    """Load saved location from config."""
    if not CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        lat, lng = cfg.get("lat", 0), cfg.get("lng", 0)
        address = cfg.get("address", "")
        if lat and lng:
            return {"latitude": lat, "longitude": lng, "address": address}
    except Exception:
        pass
    return None


def _build_cookies() -> dict[str, str]:
    """Merge session cookies with the location cookie."""
    cookies = _load_cookies()

    loc = _load_location()
    if loc:
        cookies["uev2.loc"] = json.dumps(
            {"latitude": loc["latitude"], "longitude": loc["longitude"]}
        )
    elif "uev2.loc" not in cookies:
        existing = _load_cookies()
        if "uev2.loc" in existing:
            cookies["uev2.loc"] = existing["uev2.loc"]

    return cookies


def _api_call_logging_enabled() -> bool:
    v = os.environ.get("UBEREATS_LOG_API_CALLS", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _api_call_log_path() -> Path:
    custom = os.environ.get("UBEREATS_API_CALL_LOG", "").strip()
    return Path(custom).expanduser() if custom else API_CALL_LOG_DEFAULT


def _redact_url_query(url: str) -> str:
    """Strip payments API key from query for logs."""
    try:
        p = urlparse(url)
        if not p.query:
            return url
        pairs = parse_qs(p.query, keep_blank_values=True)
        if "key" in pairs:
            pairs["key"] = ["***"]
        flat = [(k, vi) for k, vs in pairs.items() for vi in vs]
        new_q = urlencode(flat)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, p.fragment))
    except Exception:
        return url


def _append_mcp_api_call_log(
    *,
    full_url: str,
    status_code: int | None,
) -> None:
    if not _api_call_logging_enabled():
        return
    safe_url = _redact_url_query(full_url)
    try:
        pu = urlparse(safe_url)
        endpoint = pu.path or ""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "mcp",
            "method": "POST",
            "host": pu.netloc or "",
            "endpoint": endpoint,
            "url": safe_url,
            "status_code": status_code,
        }
        log_path = _api_call_log_path()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _read_csrf_from_browser() -> str:
    """Read the CSRF token from the live Chrome cookie jar (CDP path)."""
    if not cdp_enabled():
        return ""
    try:
        page = await browser_manager.ensure_cdp_page()
        ctx = page.context
        for c in await ctx.cookies():
            name = c.get("name", "").lower()
            if name in ("csrf", "csrftoken", "ct0", "_csrf"):
                val = c.get("value", "")
                if val:
                    return val
    except Exception:
        pass
    return ""


async def _post(path: str, body: dict | None = None) -> dict[str, Any]:
    """Make an authenticated POST to the Uber Eats API.

    When CDP is enabled, routes through Chrome via page.evaluate(fetch) — a real
    browser fetch from within the page's JavaScript context. This satisfies Uber's
    anti-bot detection because the request has a real browser TLS fingerprint,
    uses the page's origin, and carries Chrome's live cookie jar automatically.
    Falls back to httpx when CDP is not configured.
    """
    full_url = f"{BASE_URL.rstrip('/')}{path}" if path.startswith("/") else f"{BASE_URL.rstrip('/')}/{path}"

    if cdp_enabled():
        page = await browser_manager.ensure_cdp_page()

        # Ensure the page is on ubereats.com so fetch() uses the right origin + cookies.
        # Without this, fetch() from chrome://new-tab-page/ is cross-origin and cookies
        # are not sent, causing 401s.
        current_url = page.url or ""
        if "ubereats.com" not in current_url:
            await page.goto(web_home_url(), wait_until="domcontentloaded")
            # Wait briefly for any Cloudflare challenge to resolve.
            await page.wait_for_timeout(2000)

        # Read CSRF from live Chrome cookie jar.
        csrf = await _read_csrf_from_browser()
        headers = _api_headers(csrf=csrf)

        # Use page.evaluate(fetch) — this is a real browser fetch from within the
        # page's JS context. It carries Chrome's cookies, uses the page's TLS
        # fingerprint, and processes set-cookie responses automatically.
        result = await page.evaluate(
            """async ({url, body, headers}) => {
                try {
                    const resp = await fetch(url, {
                        method: 'POST',
                        headers: headers,
                        body: JSON.stringify(body || {}),
                        credentials: 'include',
                    });
                    const text = await resp.text();
                    let parsed = null;
                    try { parsed = JSON.parse(text); } catch(e) {}
                    return { status: resp.status, body: parsed, text: text.slice(0, 2000) };
                } catch(e) {
                    return { status: 0, body: null, text: String(e) };
                }
            }""",
            {"url": full_url, "body": body or {}, "headers": headers},
        )

        status = result.get("status", 0) if isinstance(result, dict) else 0
        _append_mcp_api_call_log(full_url=full_url, status_code=status)

        if status in (401, 403):
            return {"error": "Session expired or invalid. Use uber_eats_login to re-authenticate."}
        if status != 200:
            text = result.get("text", "") if isinstance(result, dict) else ""
            return {"error": f"API returned {status}: {text[:200]}"}

        parsed = result.get("body") if isinstance(result, dict) else None
        if parsed is None:
            text = result.get("text", "") if isinstance(result, dict) else ""
            return {"error": f"Failed to parse response: {text[:200]}"}

        if isinstance(parsed, dict):
            return _coerce_uber_json_body(parsed)
        return parsed

    # Fallback: httpx with session file cookies (non-CDP headed-browser path).
    cookies = _build_cookies()
    if not cookies:
        return {"error": "No session found. Use uber_eats_login first."}

    async with httpx.AsyncClient(
        base_url=BASE_URL,
        headers=_api_headers(),
        cookies=cookies,
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        resp = await client.post(path, json=body or {})
    _append_mcp_api_call_log(full_url=full_url, status_code=resp.status_code)

    if resp.status_code in (401, 403):
        return {"error": "Session expired or invalid. Use uber_eats_login to re-authenticate."}
    if resp.status_code != 200:
        return {"error": f"API returned {resp.status_code}: {resp.text[:200]}"}

    try:
        parsed = resp.json()
    except Exception:
        return {"error": f"Failed to parse response: {resp.text[:200]}"}

    if isinstance(parsed, dict):
        return _coerce_uber_json_body(parsed)
    return parsed


async def _post_absolute_url(url: str, body: dict | None = None) -> dict[str, Any]:
    """POST to an absolute URL (e.g. payments.ubereats.com).

    When CDP is enabled, routes through Chrome via page.evaluate(fetch). Real cookie
    scoping applies — only cookies whose domain matches the target host are sent.
    Falls back to httpx when CDP is not configured.
    """
    if cdp_enabled():
        page = await browser_manager.ensure_cdp_page()

        # Ensure the page is on ubereats.com so fetch() carries the right cookies.
        current_url = page.url or ""
        if "ubereats.com" not in current_url:
            await page.goto(web_home_url(), wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

        # Read CSRF from live Chrome cookie jar.
        csrf = await _read_csrf_from_browser()
        headers = _api_headers(csrf=csrf)
        headers["origin"] = BASE_URL
        headers["referer"] = web_home_url()

        result = await page.evaluate(
            """async ({url, body, headers}) => {
                try {
                    const resp = await fetch(url, {
                        method: 'POST',
                        headers: headers,
                        body: JSON.stringify(body || {}),
                        credentials: 'include',
                    });
                    const text = await resp.text();
                    let parsed = null;
                    try { parsed = JSON.parse(text); } catch(e) {}
                    return { status: resp.status, body: parsed, text: text.slice(0, 2000) };
                } catch(e) {
                    return { status: 0, body: null, text: String(e) };
                }
            }""",
            {"url": url, "body": body or {}, "headers": headers},
        )

        status = result.get("status", 0) if isinstance(result, dict) else 0
        _append_mcp_api_call_log(full_url=url, status_code=status)

        if status in (401, 403):
            return {"error": "Session expired or invalid. Use uber_eats_login to re-authenticate."}
        if status != 200:
            text = result.get("text", "") if isinstance(result, dict) else ""
            return {"error": f"API returned {status}: {text[:200]}"}

        parsed = result.get("body") if isinstance(result, dict) else None
        if parsed is None:
            text = result.get("text", "") if isinstance(result, dict) else ""
            return {"error": f"Failed to parse response: {text[:200]}"}

        if isinstance(parsed, dict):
            return _coerce_uber_json_body(parsed)
        return parsed

    # Fallback: httpx with session file cookies (non-CDP headed-browser path).
    cookies = _build_cookies()
    if not cookies:
        return {"error": "No session found. Use uber_eats_login first."}

    headers = {
        **_api_headers(),
        "origin": BASE_URL,
        "referer": web_home_url(),
    }
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.post(url, json=body or {}, headers=headers, cookies=cookies)
    _append_mcp_api_call_log(full_url=url, status_code=resp.status_code)

    if resp.status_code in (401, 403):
        return {"error": "Session expired or invalid. Use uber_eats_login to re-authenticate."}
    if resp.status_code != 200:
        return {"error": f"API returned {resp.status_code}: {resp.text[:200]}"}

    try:
        parsed = resp.json()
    except Exception:
        return {"error": f"Failed to parse response: {resp.text[:200]}"}

    if isinstance(parsed, dict):
        return _coerce_uber_json_body(parsed)
    return parsed


# ── Feed / Search ────────────────────────────────────────────────────────────

# getFeedV1 cacheKey tail matches the web client home feed; userQuery is layered on top.
# If product search stays empty, try UBEREATS_FEED_CACHE_TAIL=/DELIVERY/.../SEARCH////////
_FEED_CACHE_TAIL_DEFAULT = "/DELIVERY///0/0//%5B%5D/undefined//////HOME////////"


def _feed_cache_location_blob() -> dict[str, Any] | None:
    """Build the JSON object that is URL-encoded + base64'd into getFeedV1 cacheKey."""
    loc = _load_location()
    if not loc or not loc.get("latitude") or not loc.get("longitude"):
        raw = _load_cookies().get("uev2.loc", "")
        if raw:
            try:
                cj = json.loads(raw)
                lat, lng = cj.get("latitude"), cj.get("longitude")
                if lat and lng:
                    loc = {"latitude": lat, "longitude": lng, "address": ""}
            except Exception:
                pass
    if not loc or not loc.get("latitude") or not loc.get("longitude"):
        return None
    ref = ""
    rtype = ""
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            ref = (cfg.get("place_reference") or "").strip()
            rtype = (cfg.get("place_reference_type") or "").strip()
            if not (loc.get("address") or "").strip():
                addr = (cfg.get("address") or "").strip()
                if addr:
                    loc = {**loc, "address": addr}
        except Exception:
            pass
    if not ref:
        ref = os.environ.get("UBEREATS_PLACE_REFERENCE", "").strip()
        rtype = os.environ.get("UBEREATS_PLACE_REFERENCE_TYPE", "google_places").strip()
    return {
        "address": (loc.get("address") or "").strip(),
        "reference": ref,
        "referenceType": rtype,
        "latitude": float(loc["latitude"]),
        "longitude": float(loc["longitude"]),
    }


def _feed_cache_tail() -> str:
    t = os.environ.get("UBEREATS_FEED_CACHE_TAIL", "").strip()
    return t if t.startswith("/DELIVERY/") else _FEED_CACHE_TAIL_DEFAULT


def _encode_feed_cache_key(loc_blob: dict[str, Any]) -> str:
    inner = json.dumps(loc_blob, separators=(",", ":"), ensure_ascii=False)
    quoted = quote(inner, safe="")
    b64 = base64.b64encode(quoted.encode()).decode("ascii")
    return b64 + _feed_cache_tail()


def _action_url_to_absolute(action_url: str) -> str:
    if not action_url:
        return ""
    return f"{BASE_URL}{action_url}" if action_url.startswith("/") else action_url


def _query_param_from_action_url(action_url: str, key: str) -> str:
    if not action_url or "?" not in action_url:
        return ""
    try:
        q = parse_qs(urlparse(action_url).query)
        vals = q.get(key) or []
        return vals[0] if vals else ""
    except Exception:
        return ""


async def get_feed(
    query: str = "",
    offset: int = 0,
    page_size: int = 80,
) -> dict[str, Any]:
    """
    Home or search feed. When delivery location is known (config lat/lng + address),
    send the same cacheKey shape as the website so keyword search returns stores
    and catalog carousels (e.g. product names), not an empty REGULAR_STORE list.
    """
    q = (query or "").strip()
    loc_blob = _feed_cache_location_blob()
    body: dict[str, Any] = {
        "feedSessionCount": {"announcementCount": 0, "announcementLabel": ""},
        "userQuery": q,
        "date": "",
        "startTime": 0,
        "endTime": 0,
        "carouselId": "",
        "sortAndFilters": [],
        "billboardUuid": "",
        "feedProvider": "",
        "promotionUuid": "",
        "targetingStoreTag": "",
        "venueUUID": "",
        "selectedSectionUUID": "",
        "favorites": "",
        "vertical": "",
        "searchSource": "",
        "searchType": "",
        "keyName": "",
        "serializedRequestContext": "",
        "isUserInitiatedRefresh": bool(q),
    }
    if loc_blob:
        body["cacheKey"] = _encode_feed_cache_key(loc_blob)
    else:
        body["pageInfo"] = {"offset": offset, "pageSize": page_size}
    path = f"/_p/api/getFeedV1{_locale_query()}"
    return await _post(path, body)


async def get_search_feed_v1(user_query: str) -> dict[str, Any]:
    """
    Global search bar feed (matches web: SEARCH_BAR + GLOBAL_SEARCH + SEARCH_RESULTS).

    Prefer this over getFeedV1 alone for keyword queries; uses the same location cacheKey
    as getFeedV1 when lat/lng are configured.
    """
    q = (user_query or "").strip()
    loc_blob = _feed_cache_location_blob()
    body: dict[str, Any] = {
        "userQuery": q,
        "date": "",
        "startTime": 0,
        "endTime": 0,
        "sortAndFilters": [],
        "vertical": "ALL",
        "searchSource": "SEARCH_BAR",
        "displayType": "SEARCH_RESULTS",
        "searchType": "GLOBAL_SEARCH",
        "keyName": "",
        "cacheKey": "",
        "recaptchaToken": "",
    }
    if loc_blob:
        body["cacheKey"] = _encode_feed_cache_key(loc_blob)
    path = f"/_p/api/getSearchFeedV1{_locale_query()}"
    return await _post(path, body)


def parse_feed_stores(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract restaurant info from a getFeedV1 response."""
    if "error" in raw:
        return [raw]

    data = raw.get("data", {})
    feed_items = data.get("feedItems", [])
    stores: list[dict[str, Any]] = []

    for item in feed_items:
        if item.get("type") != "REGULAR_STORE":
            continue
        store = item.get("store", {})
        title = store.get("title", {}).get("text", "")
        if not title:
            continue

        rating_obj = store.get("rating") or {}
        meta_items = store.get("meta") or []
        meta2_items = store.get("meta2") or []

        is_closed = any(
            m.get("badgeType") == "CLOSED" for m in meta_items
        )
        eta_text = ""
        for m in meta_items:
            txt = m.get("text", "")
            if "min" in txt.lower():
                eta_text = txt
                break

        availability = ""
        for m in meta2_items:
            availability = m.get("text", "")

        action_url = store.get("actionUrl", "")
        store_uuid = item.get("uuid", "")

        stores.append({
            "name": title,
            "uuid": store_uuid,
            "url": f"{BASE_URL}{action_url}" if action_url.startswith("/") else action_url,
            "rating": rating_obj.get("text", ""),
            "eta": eta_text,
            "is_open": not is_closed,
            "availability": availability,
        })

    return stores


def _feed_item_title_text(row: dict[str, Any]) -> str:
    """Catalog row title may be a string or {\"text\": \"...\"}."""
    t = row.get("title")
    if isinstance(t, dict):
        return (t.get("text") or "").strip()
    if isinstance(t, str):
        return t.strip()
    return ""


def _store_dict_from_carousel_entry(st: dict[str, Any]) -> dict[str, Any] | None:
    title = (st.get("title") or {}).get("text", "")
    if not title:
        return None
    meta_items = st.get("meta") or []
    is_closed = any(m.get("badgeType") == "CLOSED" for m in meta_items)
    eta_text = ""
    for m in meta_items:
        txt = m.get("text", "")
        if "min" in txt.lower():
            eta_text = txt
            break
    action_url = st.get("actionUrl", "")
    return {
        "name": title,
        "uuid": st.get("storeUuid", ""),
        "url": _action_url_to_absolute(action_url),
        "rating": (st.get("rating") or {}).get("text", ""),
        "eta": eta_text,
        "is_open": not is_closed,
        "availability": "",
    }


def parse_feed_search_results(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Stores (REGULAR_STORE + carousel tiles) plus catalog item rows from
    CATALOG_ITEMS_CAROUSEL_PAYLOAD (global/product search on the web client).
    """
    if "error" in raw:
        return {
            "stores": [],
            "items": [],
            "feed_item_types": [],
            "error": raw["error"],
        }

    data = raw.get("data", {})
    feed_items = data.get("feedItems", [])
    type_counts: dict[str, int] = {}
    for fi in feed_items:
        t = fi.get("type") or "UNKNOWN"
        type_counts[t] = type_counts.get(t, 0) + 1

    base_stores = parse_feed_stores(raw)
    if base_stores and isinstance(base_stores[0], dict) and "error" in base_stores[0]:
        return {
            "stores": [],
            "items": [],
            "feed_item_types": sorted(type_counts.keys()),
            "error": base_stores[0]["error"],
        }

    seen_uuids: set[str] = {s.get("uuid", "") for s in base_stores if s.get("uuid")}
    merged: list[dict[str, Any]] = list(base_stores)
    items_out: list[dict[str, Any]] = []

    for item in feed_items:
        itype = item.get("type") or ""

        if itype == "REGULAR_CAROUSEL":
            carousel = item.get("carousel") or {}
            for st in carousel.get("stores") or []:
                row = _store_dict_from_carousel_entry(st)
                if not row:
                    continue
                u = row.get("uuid", "")
                if u and u in seen_uuids:
                    continue
                if u:
                    seen_uuids.add(u)
                merged.append(row)

        elif itype == "CATALOG_ITEMS_CAROUSEL_PAYLOAD":
            payload = item.get("payload") or {}
            header = payload.get("header") or {}
            title_block = header.get("title") or {}
            action_url = title_block.get("actionUrl") or ""
            store_uuid = _query_param_from_action_url(action_url, "storeUUID")
            store_name = (header.get("subtitle") or {}).get("text", "") or ""

            for row in payload.get("items") or []:
                cat = row.get("catalogItem") or {}
                if not cat:
                    continue
                mu = cat.get("uuid", "")
                sec = cat.get("sectionUuid", "")
                sub = cat.get("subsectionUuid", "") or mu
                if store_uuid and sec == store_uuid:
                    sec = ""
                row_out: dict[str, Any] = {
                    "name": cat.get("title", ""),
                    "menu_item_uuid": mu,
                    "section_uuid": sec,
                    "subsection_uuid": sub,
                    "store_uuid": store_uuid,
                    "store_name": store_name,
                    "price": cat.get("price", 0),
                    "image": cat.get("imageUrl", ""),
                    "collection_action_url": action_url,
                }
                row_out.update(_offer_fields_from_catalog_blob(cat))
                items_out.append(row_out)

        elif itype == "MINI_STORE_WITH_ITEMS":
            msi = item.get("miniStoreWithItems") or {}
            store = msi.get("store") or {}
            title = (store.get("title") or {}).get("text", "")
            action_url = store.get("actionUrl", "")
            store_uuid = _query_param_from_action_url(action_url, "storeUUID") or item.get("uuid", "")
            if title:
                rating_obj = store.get("rating") or {}
                meta_items = store.get("meta") or []
                meta2_items = store.get("meta2") or []
                is_closed = any(
                    m.get("badgeType") == "CLOSED" for m in meta_items
                )
                eta_text = ""
                for m in meta_items:
                    txt = m.get("text", "")
                    if "min" in txt.lower():
                        eta_text = txt
                        break
                availability = ""
                for m in meta2_items:
                    availability = m.get("text", "")
                row_store = {
                    "name": title,
                    "uuid": store_uuid,
                    "url": f"{BASE_URL}{action_url}" if action_url.startswith("/") else action_url,
                    "rating": rating_obj.get("text", ""),
                    "eta": eta_text,
                    "is_open": not is_closed,
                    "availability": availability,
                }
                u = row_store.get("uuid", "")
                if u and u in seen_uuids:
                    pass
                else:
                    if u:
                        seen_uuids.add(u)
                    merged.append(row_store)

            for row in msi.get("items") or []:
                mu = row.get("uuid", "")
                sec = row.get("sectionUuid", "")
                sub = row.get("subsectionUuid", "") or mu
                if store_uuid and sec == store_uuid:
                    sec = ""
                name = _feed_item_title_text(row)
                price = row.get("price", 0)
                if not isinstance(price, (int, float)):
                    price = 0
                img = row.get("imageUrl") or row.get("imageURL") or ""
                row_mini: dict[str, Any] = {
                    "name": name,
                    "menu_item_uuid": mu,
                    "section_uuid": sec,
                    "subsection_uuid": sub,
                    "store_uuid": store_uuid,
                    "store_name": title,
                    "price": price,
                    "image": img,
                    "collection_action_url": action_url,
                }
                row_mini.update(_offer_fields_from_catalog_blob(row))
                items_out.append(row_mini)

    hint = ""
    if not merged and not items_out:
        hint = (
            "No stores or catalog rows in feed. Ensure ~/.ubereats-config.json has "
            "address + lat/lng; run uber_eats_switch_address once so place_reference is saved, "
            "or set UBEREATS_PLACE_REFERENCE for Google place id."
        )

    return {
        "stores": merged,
        "items": items_out,
        "feed_item_types": sorted(type_counts.keys()),
        "assistant_hint": hint,
    }


def merge_parsed_search_results(
    a: dict[str, Any],
    b: dict[str, Any],
) -> dict[str, Any]:
    """
    Merge two parse_feed_search_results outputs: dedupe stores by uuid, items by
    (store_uuid, menu_item_uuid). Preserves order (first wins).
    """
    if a.get("error"):
        return dict(a)
    if b.get("error"):
        return dict(b)

    stores_a = a.get("stores") or []
    stores_b = b.get("stores") or []
    seen_u: set[str] = set()
    out_stores: list[dict[str, Any]] = []
    for s in stores_a + stores_b:
        u = s.get("uuid", "")
        if u:
            if u in seen_u:
                continue
            seen_u.add(u)
        out_stores.append(s)

    items_a = a.get("items") or []
    items_b = b.get("items") or []
    seen_i: set[tuple[str, str]] = set()
    out_items: list[dict[str, Any]] = []
    for it in items_a + items_b:
        su = it.get("store_uuid") or ""
        mu = it.get("menu_item_uuid") or ""
        if mu:
            key = (su, mu)
            if key in seen_i:
                continue
            seen_i.add(key)
        out_items.append(it)

    types_a = a.get("feed_item_types") or []
    types_b = b.get("feed_item_types") or []
    merged_types = sorted(set(types_a) | set(types_b))
    hint = (a.get("assistant_hint") or "").strip() or (b.get("assistant_hint") or "").strip()
    return {
        "stores": out_stores,
        "items": out_items,
        "feed_item_types": merged_types,
        "assistant_hint": hint,
    }


# ── Store / Menu ─────────────────────────────────────────────────────────────

async def get_store(store_uuid: str) -> dict[str, Any]:
    return await _post("/_p/api/getStoreV1", {
        "storeUuid": store_uuid,
        "diningMode": "DELIVERY",
        "time": {"asap": True},
        "cbType": "EATER_ENDORSED",
    })


def parse_store_menu(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract structured menu from a getStoreV1 response."""
    if "error" in raw:
        return raw

    data = raw.get("data", {})
    title = data.get("title", "Unknown")
    uuid = data.get("uuid", "")
    location = data.get("location", {})
    rating = data.get("rating", {})
    eta = data.get("etaRange", {})
    is_open = data.get("isOpen", False)
    hours_tagline = data.get("workingHoursTagline", "")
    categories = data.get("categories", [])

    sections: list[dict[str, Any]] = []
    catalog_map = data.get("catalogSectionsMap", {})

    for section_uuid_key, catalog_sections in catalog_map.items():
        for cat_section in catalog_sections:
            payload = cat_section.get("payload", {})
            std_payload = payload.get("standardItemsPayload", {})
            section_title = std_payload.get("title", {}).get("text", "")
            subsection_uuid = (
                cat_section.get("uuid")
                or payload.get("subsectionUUID")
                or std_payload.get("subsectionUUID")
                or ""
            )

            items: list[dict[str, Any]] = []
            for cat_item in std_payload.get("catalogItems", []):
                price_tag = cat_item.get("priceTagline", {})
                item_data = {
                    "uuid": cat_item.get("uuid", ""),
                    "name": cat_item.get("title", ""),
                    "description": cat_item.get("itemDescription", ""),
                    "price": price_tag.get("text", ""),
                    "price_cents": cat_item.get("price", 0),
                    "image": cat_item.get("imageUrl", ""),
                    # Items carry their own catalog-level sectionUuid/subsectionUuid.
                    # section_uuid_key is the store UUID (catalogSectionsMap key), not a section.
                    # subsection_uuid falls back to catalogSectionUUID if item field is absent.
                    "section_uuid": cat_item.get("sectionUuid") or section_uuid_key,
                    "subsection_uuid": (
                        cat_item.get("subsectionUuid")
                        or cat_section.get("catalogSectionUUID")
                        or subsection_uuid
                    ),
                }
                item_data.update(_offer_fields_from_catalog_blob(cat_item))
                items.append(item_data)

            if items:
                sections.append({
                    "section": section_title,
                    "items": items,
                })

    return {
        "restaurant": title,
        "uuid": uuid,
        "currency": data.get("currencyCode") or data.get("currency", ""),
        "address": location.get("address", ""),
        "rating": rating.get("ratingValue", ""),
        "review_count": rating.get("reviewCount", ""),
        "eta": eta.get("text", ""),
        "is_open": is_open,
        "hours": hours_tagline,
        "categories": categories,
        "sections": sections,
        "total_items": sum(len(s["items"]) for s in sections),
    }


# ── Orders ───────────────────────────────────────────────────────────────────

async def get_past_orders(last_uuid: str = "") -> dict[str, Any]:
    return await _post("/api/getPastOrdersV1?localeCode=en", {
        "lastWorkflowUUID": last_uuid,
    })


def parse_orders(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract order info from a getPastOrdersV1 response."""
    if "error" in raw:
        return [raw]

    data = raw.get("data", {})
    orders_map = data.get("ordersMap", {})
    orders: list[dict[str, Any]] = []

    for _uuid, order in orders_map.items():
        store_info = order.get("storeInfo", {})
        fare_info = order.get("fareInfo", {})
        base = order.get("baseEaterOrder", {})

        orders.append({
            "uuid": base.get("uuid", ""),
            "store_uuid": store_info.get("uuid") or store_info.get("storeUuid") or "",
            "restaurant": store_info.get("title", "Unknown"),
            "status": base.get("currentState", ""),
            "total_cents": fare_info.get("totalPrice", 0),
            "currency": base.get("currencyCode", ""),
            "date": base.get("lastStateChangeAt", ""),
            "items_count": len(order.get("itemInfoList", [])),
            "items": [
                {
                    "name": item.get("title", ""),
                    "quantity": item.get("quantity", 1),
                    "price": item.get("price", ""),
                }
                for item in order.get("itemInfoList", [])[:10]
            ],
        })

    return sorted(orders, key=lambda o: o.get("date", ""), reverse=True)


def _uber_text_field(value: Any) -> str:
    """Coerce Uber rich-text or plain string fields to a short string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("title") or "").strip()
    return str(value).strip()


def parse_active_orders(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Summarize getActiveOrdersV1 orders (structure varies by order type)."""
    if "error" in raw:
        return [raw]

    out: list[dict[str, Any]] = []
    for o in raw.get("data", {}).get("orders") or []:
        if not isinstance(o, dict):
            continue
        base = o.get("baseEaterOrder") or o.get("base") or {}
        store = o.get("storeInfo") or {}
        oi = o.get("orderInfo") or {}
        si = oi.get("storeInfo") or {}

        restaurant = _uber_text_field(store.get("title"))
        if not restaurant:
            restaurant = _uber_text_field(si.get("name")) or _uber_text_field(si.get("title"))

        cs = base.get("currentState", "") or o.get("state", "")
        phase = (oi.get("orderPhase") or "").strip()
        aos = o.get("activeOrderStatus") or {}
        headline = _uber_text_field(aos.get("title"))
        sub = _uber_text_field(aos.get("subtitle"))
        if cs:
            status = cs
        else:
            status = " · ".join(p for p in (phase, headline, sub) if p)

        store_uuid = (
            si.get("storeUUID")
            or si.get("uuid")
            or store.get("uuid")
            or store.get("storeUuid")
            or ""
        )
        if isinstance(store_uuid, str):
            store_uuid = store_uuid.strip()

        out.append({
            "uuid": o.get("uuid") or base.get("uuid", ""),
            "restaurant": restaurant or "Unknown",
            "status": status,
            "store_uuid": store_uuid,
        })
    return out


# ── Address ──────────────────────────────────────────────────────────────────

async def get_saved_addresses() -> dict[str, Any]:
    return await _post("/_p/api/getDeliveryLocationsV2", {})


def parse_saved_addresses(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract saved addresses from getDeliveryLocationsV2 response."""
    if "error" in raw:
        return raw

    data = raw.get("data", {})
    locations = data.get("deliveryLocations", {})
    result: dict[str, list[dict[str, Any]]] = {}

    for category, items in locations.items():
        parsed: list[dict[str, Any]] = []
        for item in items:
            loc = item.get("location") or {}
            personalization = loc.get("personalization") or {}
            coord = loc.get("coordinate") or {}
            delivery = item.get("deliveryPayload") or {}

            parsed.append({
                "label": personalization.get("label", ""),
                "name": loc.get("name", ""),
                "full_address": loc.get("fullAddress", ""),
                "latitude": coord.get("latitude", 0),
                "longitude": coord.get("longitude", 0),
                "place_id": loc.get("id", ""),
                "provider": loc.get("provider", ""),
                "uuid": delivery.get("UUID", ""),
            })
        if parsed:
            result[category.lower()] = parsed

    return {"addresses": result}


async def switch_address(
    place_id: str,
    provider: str,
    latitude: float,
    longitude: float,
    address: str = "",
) -> dict[str, Any]:
    """Switch to a saved address using setTargetLocationV1."""
    body = {
        "address": {
            "address1": address,
            "address2": "",
            "aptOrSuite": "",
            "eaterFormattedAddress": address,
            "subtitle": address,
            "title": address,
            "uuid": "",
        },
        "latitude": latitude,
        "longitude": longitude,
        "reference": place_id,
        "referenceType": provider,
        "type": provider,
        "source": "manual_auto_complete",
        "userState": "Unknown",
    }
    return await _post("/_p/api/setTargetLocationV1", body)


async def set_location(
    latitude: float,
    longitude: float,
    address: str = "",
    reference: str = "",
    reference_type: str = "google_places",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "address": {
            "address1": address,
            "address2": "",
            "aptOrSuite": "",
            "eaterFormattedAddress": address,
            "subtitle": address,
            "title": address,
            "uuid": "",
        },
        "latitude": latitude,
        "longitude": longitude,
        "reference": reference,
        "referenceType": reference_type,
        "type": reference_type,
        "source": "manual_auto_complete",
        "userState": "Unknown",
    }
    return await _post("/_p/api/setTargetLocationV1", body)


# ── Locale + _p/api helpers (discovery uses ?localeCode=us-en) ───────────────


def _locale_query() -> str:
    """Query suffix for Uber web API calls (e.g. cl-en, en-us)."""
    try:
        from . import preferences

        loc = preferences.load_preferences().get("locale_code")
        if isinstance(loc, str) and loc.strip():
            return f"?localeCode={loc.strip()}"
    except Exception:
        pass
    return "?localeCode=us-en"


async def _post_with_locale(path: str, body: dict | None = None) -> dict[str, Any]:
    """POST with locale query (matches captured traffic)."""
    return await _post(f"{path}{_locale_query()}", body)


# ── Cart / draft order ────────────────────────────────────────────────────────


async def get_carts_view() -> dict[str, Any]:
    return await _post_with_locale("/_p/api/getCartsViewForEaterUuidV1", {})


async def get_draft_orders(
    *,
    remove_adapters: bool = True,
    currency_code: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if remove_adapters:
        body["removeAdapters"] = True
    if currency_code:
        body["currencyCode"] = currency_code
    return await _post_with_locale("/_p/api/getDraftOrdersByEaterUuidV1", body)


async def get_draft_order(draft_order_uuid: str) -> dict[str, Any]:
    return await _post_with_locale(
        "/_p/api/getDraftOrderByUuidV2",
        {"draftOrderUUID": draft_order_uuid},
    )


async def get_menu_item_v1(
    *,
    store_uuid: str,
    section_uuid: str,
    subsection_uuid: str,
    menu_item_uuid: str,
    item_request_type: str = "ITEM",
    dining_mode: str | None = "DELIVERY",
    cb_type: str | None = "EATER_ENDORSED",
) -> dict[str, Any]:
    """Single menu item detail + customization options (captured from discovery)."""
    body: dict[str, Any] = {
        "itemRequestType": item_request_type,
        "storeUuid": store_uuid,
        "sectionUuid": section_uuid,
        "subsectionUuid": subsection_uuid,
        "menuItemUuid": menu_item_uuid,
    }
    if dining_mode is not None:
        body["diningMode"] = dining_mode
    if cb_type:
        body["cbType"] = cb_type
    return await _post_with_locale("/_p/api/getMenuItemV1", body)


CHECKOUT_PAYLOAD_TYPES_FULL: list[str] = [
    "cartItems",
    "basketSize",
    "fareBreakdown",
    "promotion",
    "upfrontTipping",
    "eta",
    "passBanner",
    "subsRenewalBanner",
    "basketSizeTracker",
    "deliveryOptInInfo",
    "fulfillmentPromotionInfo",
    "cartItemPromotions",
    "disclaimers",
    "subtotal",
    "total",
    "orderConfirmations",
    "restrictedItems",
    "canonicalProductStorePickerPayload",
    "storeSwitcherActionableBannerPayload",
    "taxProfiles",
    "messageBanner",
    "complements",
    "merchantMembership",
    "timeWindowPicker",
    "locationInfo",
    "venueSectionPicker",
    "paymentOrderEligibilityBreakdown",
    "splitPaymentMessageBanner",
    "paymentProfilesEligibility",
    "upsellCatalogSections",
    "subTotalFareBreakdown",
]


async def get_checkout_presentation(
    draft_order_uuid: str,
    *,
    payload_types: list[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "payloadTypes": payload_types or CHECKOUT_PAYLOAD_TYPES_FULL,
        "draftOrderUUID": draft_order_uuid,
        "isNewCartAbstraction": True,
    }
    return await _post_with_locale("/_p/api/getCheckoutPresentationV1", body)


async def post_payment_selector_change(selected_payment_profile_uuid: str) -> dict[str, Any]:
    return await _post_with_locale(
        "/api/postPaymentSelectorChange",
        {"selectedPaymentProfileUUID": selected_payment_profile_uuid},
    )


async def get_active_orders(
    timezone: str = "America/Santiago",
) -> dict[str, Any]:
    return await _post_with_locale(
        "/_p/api/getActiveOrdersV1",
        {
            "orderUuid": None,
            "timezone": timezone,
            "showAppUpsellIllustration": True,
            "isDirectTracking": False,
        },
    )


async def update_draft_order_v2(body: dict[str, Any]) -> dict[str, Any]:
    """Update draft checkout: tip, payment profile, address, schedule, promos."""
    return await _post_with_locale("/_p/api/updateDraftOrderV2", body)


async def create_draft_order_v2(body: dict[str, Any]) -> dict[str, Any]:
    """Create a draft order (empty store payload or web multicart body with shoppingCartItems)."""
    return await _post_with_locale("/_p/api/createDraftOrderV2", body)


async def update_item_in_draft_order_v2(body: dict[str, Any]) -> dict[str, Any]:
    """Change quantity or line payload on an existing draft (e.g. grocery cart abstraction)."""
    return await _post_with_locale("/_p/api/updateItemInDraftOrderV2", body)


async def add_items_to_draft_order_v2(body: dict[str, Any]) -> dict[str, Any]:
    """Add line items to an existing draft order."""
    return await _post_with_locale("/_p/api/addItemsToDraftOrderV2", body)


async def remove_items_from_draft_order_v2(body: dict[str, Any]) -> dict[str, Any]:
    """Remove line items by shoppingCartItemUUID."""
    return await _post_with_locale("/_p/api/removeItemsFromDraftOrderV2", body)


async def get_search_home_v2(*, drop_past_orders: bool = True) -> dict[str, Any]:
    return await _post_with_locale(
        "/_p/api/getSearchHomeV2",
        {"dropPastOrders": drop_past_orders},
    )


async def get_profiles_for_user_v1() -> dict[str, Any]:
    return await _post_with_locale("/_p/api/getProfilesForUserV1", {})


async def select_profile_v1(
    profile_uuid: str,
    *,
    select_source: str = "SELECT_PROFILE_SOURCE_CLIENT_PROFILE_SWITCH",
) -> dict[str, Any]:
    return await _post_with_locale(
        "/_p/api/selectProfileV1",
        {"profileUUID": profile_uuid, "selectProfileSource": select_source},
    )


async def apply_promo_v1(code: str) -> dict[str, Any]:
    return await _post_with_locale("/_p/api/applyPromoV1", {"code": code.strip()})


async def get_savings_v1(
    *,
    draft_order_uuid: str,
    store_uuid: str,
    payment_profile_uuid: str,
    delivery_fee_e5: int,
    auto_apply_promotion_uuid: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "CHECKOUT",
        "paymentProfileUuid": payment_profile_uuid,
        "autoApplyPromotionUuid": auto_apply_promotion_uuid,
        "deliveryFee": delivery_fee_e5,
        "storeUuid": store_uuid,
        "draftOrderUuid": draft_order_uuid,
    }
    return await _post_with_locale("/_p/api/getSavingsV1", body)


def _payments_url_with_location(
    path: str,
    *,
    locale_code: str = "en",
    latitude: float | None = None,
    longitude: float | None = None,
) -> str | None:
    loc = _load_location()
    lat = latitude if latitude is not None else (loc or {}).get("latitude")
    lng = longitude if longitude is not None else (loc or {}).get("longitude")
    if lat is None or lng is None:
        return None
    key = os.environ.get("UBEREATS_PAYMENTS_API_KEY", UBER_PUBLIC_PAYMENTS_CLIENT_KEY)
    ctx = quote(json.dumps({"latitude": lat, "longitude": lng}))
    return f"{PAYMENTS_BASE_URL}{path}?key={key}&ctx={ctx}&localeCode={locale_code}"


async def _post_payments_json(
    path: str,
    body: dict[str, Any],
    *,
    locale_code: str = "en",
    latitude: float | None = None,
    longitude: float | None = None,
) -> dict[str, Any]:
    url = _payments_url_with_location(path, locale_code=locale_code, latitude=latitude, longitude=longitude)
    if not url:
        return {"error": "Missing coordinates; set delivery location first."}
    return await _post_absolute_url(url, body)


async def get_pre_checkout_actions(
    body: dict[str, Any],
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    locale_code: str = "en",
) -> dict[str, Any]:
    """Payments API — requires checkoutSessionUUID and plan payload from checkout flow."""
    return await _post_payments_json(
        "/api/getPreCheckoutActions",
        body,
        locale_code=locale_code,
        latitude=latitude,
        longitude=longitude,
    )


async def profile_patch_default_payment(
    *,
    profile_uuid: str,
    profile_type: str,
    default_payment_profile_uuid: str,
    locale_code: str = "en",
) -> dict[str, Any]:
    """POST payments.ubereats.com/api/profilePatch — set default card on an account profile."""
    return await _post_payments_json(
        "/api/profilePatch",
        {
            "profile": {
                "uuid": profile_uuid,
                "type": profile_type,
                "defaultPaymentProfileUuid": default_payment_profile_uuid,
            }
        },
        locale_code=locale_code,
    )


async def checkout_orders_by_draft_orders_v1(body: dict[str, Any]) -> dict[str, Any]:
    """Submit checkout / place order for a draft (captured from web client)."""
    return await _post_with_locale("/_p/api/checkoutOrdersByDraftOrdersV1", body)


def extract_checkout_session_uuid(raw: dict[str, Any]) -> str:
    """
    Best-effort checkoutSessionUUID from getCheckoutPresentationV1 (or nested JSON).
    Often empty — set UBEREATS_CHECKOUT_SESSION_UUID for payments pre-step, or submit may still work.
    """
    env = os.environ.get("UBEREATS_CHECKOUT_SESSION_UUID", "").strip()
    if env:
        return env

    def walk(o: Any) -> str:
        if isinstance(o, dict):
            for k, v in o.items():
                kl = k.lower().replace("_", "")
                if isinstance(v, str) and len(v) >= 32:
                    if kl == "checkoutsessionuuid" or kl.endswith("checkoutsessionuuid"):
                        return v
                found = walk(v)
                if found:
                    return found
        elif isinstance(o, list):
            for x in o:
                found = walk(x)
                if found:
                    return found
        return ""

    return walk(raw)


def _amount_e5_from_checkout_value(val: dict[str, Any]) -> int:
    ae = val.get("amountE5")
    if isinstance(ae, int):
        return ae
    if isinstance(ae, dict) and "low" in ae:
        return int(ae["low"])
    return 0


def build_pre_checkout_actions_body(
    *,
    checkout_session_uuid: str,
    payment_profile_uuid: str,
    order_total_e5: int,
    currency_code: str,
    use_credits: bool,
    country_iso2: str | None = None,
    region_id: int | None = None,
    is_first_checkout: bool = True,
) -> dict[str, Any]:
    use_case = os.environ.get("UBEREATS_PAYMENTS_USE_CASE_KEY", UBER_PUBLIC_PAYMENTS_USE_CASE_ID)
    cc = (country_iso2 or os.environ.get("UBEREATS_COUNTRY_ISO2", "CL")).upper()
    rid = region_id if region_id is not None else int(os.environ.get("UBEREATS_REGION_ID", "148"))
    return {
        "checkoutSessionUUID": checkout_session_uuid,
        "useCaseKey": use_case,
        "estimatedPaymentPlan": {
            "defaultPaymentProfile": {
                "paymentProfileUUID": payment_profile_uuid,
                "currencyAmount": {
                    "amountE5": order_total_e5,
                    "currencyCode": currency_code or "CLP",
                },
                "priceStatus": "FINAL",
            },
            "useCredits": use_credits,
        },
        "orderContext": {
            "base": {
                "businessLocation": {
                    "type": "location",
                    "location": {"regionID": rid, "countryISO2": cc},
                }
            }
        },
        "isFirstCheckout": is_first_checkout,
    }


def build_checkout_orders_request(
    draft: dict[str, Any],
    *,
    order_total_e5: int,
    currency_code: str,
    timezone: str = "America/Santiago",
    tracking_code: str | None = None,
) -> dict[str, Any]:
    """Body for POST /_p/api/checkoutOrdersByDraftOrdersV1 (aligned with web client)."""
    du = draft.get("uuid") or ""
    sc = draft.get("shoppingCart") or {}
    su = draft.get("storeUuid") or sc.get("storeUuid") or ""
    pay = draft.get("paymentProfileUUID") or ""
    da = draft.get("deliveryAddress") or {}
    addr = da.get("address") or {}
    raw_city = (addr.get("city") or addr.get("address2") or "").strip()
    city_name = raw_city.split(",")[0].strip().lower() if raw_city else ""
    if not city_name:
        city_name = (os.environ.get("UBEREATS_CHECKOUT_CITY_NAME", "") or "santiago").lower()
    md = draft.get("orderMetadata") or {}
    um = md.get("uberMerchantType") or {}
    umt = um.get("type") or "MERCHANT_TYPE_RESTAURANT"
    vertical = "GROCERY" if umt != "MERCHANT_TYPE_RESTAURANT" else "RESTAURANT"
    tc = tracking_code
    if tc is None:
        tc = os.environ.get("UBEREATS_CHECKOUT_TRACKING_CODE", "{}")
    return {
        "draftOrderUUID": du,
        "storeInstructions": "",
        "extraPaymentData": "",
        "shareCPFWithRestaurant": False,
        "extraParams": {
            "timezone": timezone,
            "trackingCode": tc,
            "storeUuid": su,
            "cityName": city_name,
            "paymentIntent": "personal",
            "isTealiumEnabled": True,
            "paymentProfileTokenType": "braintree",
            "paymentProfileUuid": pay,
            "isNeutralZoneEnabled": True,
            "isScheduledOrder": False,
            "isBillSplitOrder": False,
            "isDraftOrderParticipant": False,
            "isEditScheduledOrder": False,
            "orderTotalFare": order_total_e5,
            "orderCurrency": currency_code or "CLP",
            "verticalLabel": vertical,
            "cookieConsent": True,
            "checkoutType": "drafting",
            "isAddOnOrder": False,
            "isMatchbox": False,
            "promotionUuid": "",
        },
    }


def build_update_draft_order_body_from_draft(draft: dict[str, Any]) -> dict[str, Any]:
    """Map getDraftOrderByUuidV2 draftOrder object to updateDraftOrderV2 request body."""
    promo = draft.get("promotionOptions") or {
        "autoApplyPromotionUUIDs": [],
        "selectedPromotionInstanceUUIDs": [],
        "skipApplyingPromotion": False,
    }
    tr = draft.get("targetDeliveryTimeRange")
    if not tr:
        tr = {"asap": True}

    body: dict[str, Any] = {
        "promotionOptions": promo,
        "upfrontTipOption": draft["upfrontTipOption"],
        "useCredits": draft.get("useCredits", True),
        "deliveryType": draft.get("deliveryType", "ASAP"),
        "extraPaymentProfiles": draft.get("extraPaymentProfiles") or [],
        "interactionType": draft.get("interactionType", "door_to_door"),
        "businessDetails": draft["businessDetails"],
        "cartLockOptions": draft.get("cartLockOptions"),
        "targetDeliveryTimeRange": tr,
        "deliveryAddress": draft["deliveryAddress"],
        "diningMode": draft.get("diningMode", "DELIVERY"),
        "draftOrderUUID": draft["uuid"],
    }
    if draft.get("paymentProfileUUID"):
        body["paymentProfileUUID"] = draft["paymentProfileUUID"]
    return body


def parse_delivery_fee_e5_from_checkout(raw: dict[str, Any]) -> int:
    """Best-effort delivery fee in amountE5.low from getCheckoutPresentationV1."""
    if "error" in raw:
        return 0
    cp = raw.get("data", {}).get("checkoutPayloads") or {}
    for ch in (cp.get("fareBreakdown") or {}).get("charges") or []:
        title = (ch.get("title") or {}).get("text", "").lower()
        meta = ch.get("fareBreakdownChargeMetadata") or {}
        fid = (meta.get("fareInfoID") or "").lower()
        if "delivery" not in fid and "delivery" not in title and "envío" not in title:
            continue
        infos = meta.get("analyticsInfo") or []
        if not infos:
            continue
        amt = infos[0].get("currencyAmount", {}).get("amountE5")
        if isinstance(amt, dict) and "low" in amt:
            return int(amt["low"])
    return 0


def parse_search_history(raw: dict[str, Any]) -> list[dict[str, str]]:
    """Recent search strings from getSearchHomeV2."""
    if "error" in raw:
        return []
    out: list[dict[str, str]] = []
    for item in raw.get("data", {}).get("searchHistory") or []:
        if item.get("type") == "searchHistory":
            t = item.get("title") or item.get("titleTerm") or ""
            if t:
                out.append({"title": t})
    return out[:30]


def parse_profiles_for_user(raw: dict[str, Any]) -> dict[str, Any]:
    """Uber account profiles (Personal, Business, …) from getProfilesForUserV1."""
    if "error" in raw:
        return raw
    result: list[dict[str, Any]] = []
    for p in raw.get("data", {}).get("profiles") or []:
        result.append({
            "uuid": p.get("uuid", ""),
            "name": p.get("name", ""),
            "type": p.get("type", ""),
            "status": p.get("status", ""),
            "default_payment_profile_uuid": p.get("defaultPaymentProfileUuid", ""),
        })
    sel = raw.get("data", {}).get("selectedProfile") or {}
    return {
        "profiles": result,
        "selected_profile_uuid": sel.get("profileUUID", ""),
    }


def _rich_text_to_plain(obj: Any) -> str:
    """Best-effort plain text from Uber rich text / title objects."""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        if "text" in obj and isinstance(obj["text"], str):
            return obj["text"]
        t = obj.get("text")
        if isinstance(t, dict):
            inner = t.get("text")
            if isinstance(inner, dict) and "text" in inner:
                return str(inner["text"])
            if isinstance(inner, str):
                return inner
        rtes = obj.get("richTextElements")
        if isinstance(rtes, list):
            parts: list[str] = []
            for el in rtes:
                parts.append(_rich_text_to_plain(el))
            return "".join(parts).strip()
    return ""


def _price_display_plain(obj: Any) -> str:
    """Plain price string from checkout/menu rich text (normalizes non-breaking spaces)."""
    return _rich_text_to_plain(obj).replace("\xa0", " ").strip()


def _offer_fields_from_catalog_blob(blob: dict[str, Any]) -> dict[str, Any]:
    """
    itemPromotion / promotion on catalog rows (feed search, menu, mini-store items, draft lines).

    Surfaces on_offer, regular/discounted prices, offer_summary for assistants to read aloud.
    """
    if not isinstance(blob, dict):
        return {}
    promo = blob.get("itemPromotion")
    if not isinstance(promo, dict):
        p2 = blob.get("promotion")
        promo = p2 if isinstance(p2, dict) else None
    if not isinstance(promo, dict):
        return {}

    orig = _price_display_plain(promo.get("originalPrice"))
    disc = _price_display_plain(promo.get("discountedPrice"))
    badge = _rich_text_to_plain(promo.get("badgeText") or promo.get("accessory"))
    if not badge:
        t = promo.get("title")
        badge = _rich_text_to_plain(t) if t is not None else ""

    out: dict[str, Any] = {}
    if orig and disc and orig != disc:
        out["on_offer"] = True
        out["regular_price"] = orig
        out["discounted_price"] = disc
        out["original_price"] = orig
        out["offer_summary"] = f"Was {orig}, now {disc}"
    elif orig and not disc:
        out["regular_price"] = orig
        out["original_price"] = orig
    if badge:
        out["offer_badge"] = badge[:240]
        out.setdefault("on_offer", True)
    if out.get("on_offer") or out.get("offer_badge"):
        return out
    desc = _rich_text_to_plain(promo.get("description") or promo.get("subtitle"))
    if desc and len(desc) < 160:
        out["offer_badge"] = desc
        out["on_offer"] = True
    return out


def parse_carts_view(raw: dict[str, Any]) -> dict[str, Any]:
    """Summarize getCartsViewForEaterUuidV1."""
    if "error" in raw:
        return raw
    carts = (
        raw.get("data", {})
        .get("cartsView", {})
        .get("carts", [])
    )
    out: list[dict[str, Any]] = []
    for c in carts:
        t1 = (c.get("tagline1") or {}).get("text", "")
        t2 = (c.get("tagline2") or {}).get("text", "")
        out.append({
            "draft_order_uuid": c.get("draftOrderUUID", ""),
            "restaurant": c.get("title", ""),
            "subtotal_line": t1,
            "delivery_line": t2,
            "item_count": c.get("itemCount", 0),
        })
    return {"carts": out}


def _format_clp_e5(amount_e5: int | None) -> str:
    if amount_e5 is None:
        return ""
    try:
        v = amount_e5 / 100_000.0
        if abs(v - round(v)) < 0.01:
            return str(int(round(v)))
        return f"{v:.2f}"
    except Exception:
        return str(amount_e5)


def _draft_line_title_plain(it: dict[str, Any]) -> str:
    t = it.get("title")
    if isinstance(t, dict):
        return (t.get("text") or "").strip()
    if isinstance(t, str):
        return t.strip()
    return ""


def find_shopping_cart_line_raw(
    draft_order_detail: dict[str, Any],
    *,
    shopping_cart_item_uuid: str = "",
    title_query: str = "",
) -> dict[str, Any] | None:
    """
    Locate one raw shoppingCart item from getDraftOrderByUuidV2 for update/remove flows.

    Returns draft_order_uuid, store_uuid, line (full dict), shopping_cart_item_uuid.
    """
    if "error" in draft_order_detail:
        return None
    draft = draft_order_detail.get("data", {}).get("draftOrder")
    if not draft:
        return None
    du = str(draft.get("uuid") or "")
    sc = draft.get("shoppingCart") or {}
    su = (
        str(draft.get("storeUuid") or draft.get("restaurantUUID") or "")
        or str(sc.get("storeUuid") or "")
    )
    items = sc.get("items") or []
    scu_need = (shopping_cart_item_uuid or "").strip().lower()

    if scu_need:
        for it in items:
            u = (it.get("shoppingCartItemUUID") or it.get("shoppingCartItemUuid") or "")
            if str(u).lower() == scu_need:
                return {
                    "draft_order_uuid": du,
                    "store_uuid": su,
                    "line": dict(it),
                    "shopping_cart_item_uuid": str(u),
                }
        return None

    tq = (title_query or "").strip().lower()
    if not tq:
        return None

    def norm(s: str) -> str:
        return " ".join(s.lower().split())

    nq = norm(tq)
    for it in items:
        title = _draft_line_title_plain(it)
        nt = norm(title)
        if nt == nq or nq in nt or nt in nq:
            u = it.get("shoppingCartItemUUID") or it.get("shoppingCartItemUuid") or ""
            return {
                "draft_order_uuid": du,
                "store_uuid": su,
                "line": dict(it),
                "shopping_cart_item_uuid": str(u),
            }
    return None


def parse_draft_order_cart(raw: dict[str, Any]) -> dict[str, Any]:
    """Line items + store from getDraftOrdersByEaterUuidV1 or getDraftOrderByUuidV2."""
    if "error" in raw:
        return raw

    draft = raw.get("data", {}).get("draftOrder")
    orders = raw.get("data", {}).get("draftOrders")
    if draft:
        cart_src = draft
    elif orders:
        cart_src = orders[0]
    else:
        return {"restaurant_uuid": "", "draft_order_uuid": "", "items": []}

    sc = cart_src.get("shoppingCart") or {}
    store_uuid = cart_src.get("storeUuid") or cart_src.get("restaurantUUID") or sc.get("storeUuid", "")
    draft_uuid = cart_src.get("uuid", "")
    items_out: list[dict[str, Any]] = []

    for it in sc.get("items") or []:
        title = _draft_line_title_plain(it) or (it.get("title") if isinstance(it.get("title"), str) else "")
        qty = it.get("quantity", 1)
        price_raw = it.get("price")
        price_txt = ""
        if isinstance(price_raw, int):
            if price_raw >= 100_000:
                price_txt = _format_clp_e5(price_raw)
            elif price_raw > 0:
                price_txt = str(price_raw)
        line_out: dict[str, Any] = {
            "title": title,
            "quantity": qty,
            "price": price_txt,
            "shopping_cart_item_uuid": it.get("shoppingCartItemUUID") or it.get("shoppingCartItemUuid", ""),
        }
        line_out.update(_offer_fields_from_catalog_blob(it))
        items_out.append(line_out)

    return {
        "restaurant_uuid": store_uuid,
        "draft_order_uuid": draft_uuid,
        "items": items_out,
    }


def parse_checkout_payloads(raw: dict[str, Any]) -> dict[str, Any]:
    """Structured checkout summary from getCheckoutPresentationV1."""
    if "error" in raw:
        return raw

    session_uuid = extract_checkout_session_uuid(raw)

    cp = raw.get("data", {}).get("checkoutPayloads") or {}
    sub = cp.get("subtotal") or {}
    tot = cp.get("total") or {}
    sub_val = (sub.get("subtotal") or {}).get("value") or {}
    tot_val = (tot.get("total") or {}).get("value") or {}
    sub_fmt = (sub.get("subtotal") or {}).get("formattedValue", "")
    tot_fmt = (tot.get("total") or {}).get("formattedValue", "")

    fare = cp.get("fareBreakdown") or {}
    charges: list[dict[str, str]] = []
    for ch in fare.get("charges") or []:
        title = (ch.get("title") or {}).get("text", "")
        val = (ch.get("value") or {}).get("text") or (ch.get("chargeValue") or {}).get("badgeChargeValue", {}).get("text", "")
        if title or val:
            charges.append({"title": title, "value": val})

    cart_block = cp.get("cartItems") or {}
    cart_items: list[dict[str, Any]] = []
    for ci in cart_block.get("cartItems") or []:
        name = _rich_text_to_plain(ci.get("title"))
        qty_obj = ci.get("quantity") or {}
        coeff = ((qty_obj.get("value") or {}).get("coefficient") or {})
        q = 1
        if isinstance(coeff, dict) and "low" in coeff:
            q = int(coeff.get("low", 1))
        elif isinstance(coeff, (int, float)):
            q = int(coeff)
        orig_p = _price_display_plain(ci.get("originalPrice"))
        disc_p = _price_display_plain(ci.get("discountedPrice"))
        pay = disc_p or orig_p
        item_out: dict[str, Any] = {
            "name": name,
            "quantity": q,
            "line_price": pay,
        }
        if orig_p and disc_p and orig_p != disc_p:
            item_out["regular_price"] = orig_p
            item_out["discounted_price"] = disc_p
            item_out["on_offer"] = True
            item_out["offer_summary"] = f"Was {orig_p}, now {disc_p}"
        cart_items.append(item_out)

    tip_block = cp.get("upfrontTipping") or {}
    tip_options: list[dict[str, Any]] = []
    for opt in tip_block.get("options") or []:
        tip_options.append({
            "label": opt.get("displayText", "").replace("\n", " "),
            "percent": opt.get("percent", 0),
            "amount": opt.get("amount", 0),
            "selected": opt.get("isSelectedTip", False),
        })

    loc = cp.get("locationInfo") or {}
    addr = loc.get("address") or {}
    addr_title = addr.get("title", "")
    addr_sub = _rich_text_to_plain(addr.get("subtitle"))

    pay = cp.get("paymentProfilesEligibility") or {}
    eligibility = pay.get("eligibilityPayloadList") or []
    payment_methods: list[dict[str, Any]] = []
    for ep in eligibility:
        if not isinstance(ep, dict):
            continue
        uuid = ep.get("paymentProfileUUID") or ep.get("uuid", "")
        label = ep.get("title") or ep.get("displayName") or ep.get("subtitle", "")
        if isinstance(label, dict):
            label = _rich_text_to_plain(label)
        payment_methods.append({
            "uuid": uuid,
            "label": str(label),
        })

    sub_e5 = _amount_e5_from_checkout_value(sub_val)
    tot_e5 = _amount_e5_from_checkout_value(tot_val)

    return {
        "subtotal": sub_fmt or _format_clp_e5(sub_val.get("amountE5")),
        "total": tot_fmt or _format_clp_e5(tot_val.get("amountE5")),
        "subtotal_amount_e5": sub_e5,
        "total_amount_e5": tot_e5,
        "currency": sub_val.get("currencyCode") or tot_val.get("currencyCode", ""),
        "fare_breakdown": charges,
        "cart_items": cart_items,
        "tip_options": tip_options,
        "delivery_address_title": addr_title,
        "delivery_address": addr_sub or addr_title,
        "payment_methods": payment_methods,
        "checkout_session_uuid": session_uuid,
    }
