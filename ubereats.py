"""
Uber Eats operations: login, search, menu, cart, checkout, orders.

Uses direct HTTP API calls for most operations (fast, reliable).
Browser (Playwright) is only used for login and cart/checkout
until those API endpoints are captured.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import Page, TimeoutError as PwTimeout

import api
import preferences
from browser import BASE_URL, manager

TIMEOUT = 12_000


# ── helpers ──────────────────────────────────────────────────────────────────

def _has_auth_signals() -> bool:
    """Check if we've captured auth tokens/session from network traffic."""
    cfg = manager.config
    return bool(cfg.sid or cfg.user_id or cfg.cookies.get("sid"))


async def _check_login_dom(page: Page) -> bool:
    """DOM heuristic: no sign-in links visible."""
    try:
        sign_in = page.get_by_role("link", name=re.compile(r"sign\s*in|log\s*in", re.I))
        count = await sign_in.count()
        return count == 0
    except Exception:
        return True


# ── Login (browser only) ────────────────────────────────────────────────────

async def login() -> dict[str, Any]:
    """Open a headed browser for the user to log in. Captures session via network interception."""
    page = await manager.launch_with_interception()
    await page.goto(f"{BASE_URL}/", wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    sign_in = page.get_by_role("link", name=re.compile(r"sign\s*in|log\s*in|iniciar", re.I))
    try:
        await sign_in.first.click(timeout=5000)
    except Exception:
        pass

    print(
        "\n>>> Browser opened. Please log in to Uber Eats.\n"
        ">>> The window will close automatically after login.\n",
        file=sys.stderr,
    )

    logged_in = False
    timeout_ms = 3 * 60 * 1000
    start = asyncio.get_event_loop().time()

    while not logged_in and (asyncio.get_event_loop().time() - start) * 1000 < timeout_ms:
        await page.wait_for_timeout(1500)
        try:
            if _has_auth_signals():
                url = page.url
                on_ubereats = "ubereats.com" in url
                if on_ubereats and await _check_login_dom(page):
                    logged_in = True
                    break
                if on_ubereats:
                    await page.wait_for_timeout(2000)
                    logged_in = True
                    break
        except Exception:
            pass

    await page.wait_for_timeout(2000)
    result = await manager.save_session()
    cfg = manager.config

    await manager.close()

    if not logged_in:
        return {
            "status": "timeout",
            "message": "Login timed out after 3 minutes. Please try again.",
        }

    return {
        "status": "success",
        "message": result,
        "user": {
            "name": cfg.user_name or "(captured on next API call)",
            "email": cfg.user_email or "(captured on next API call)",
            "user_id": cfg.user_id or "(captured on next API call)",
        },
        "address": cfg.address or "(use uber_eats_set_address to configure)",
        "api_endpoints_captured": len(cfg.captured_endpoints),
    }


# ── Who Am I (API) ──────────────────────────────────────────────────────────

async def whoami() -> dict[str, Any]:
    """Get current user info from saved session + preferences summary."""
    cookies = api._load_cookies()
    if not cookies:
        return {"error": "Not logged in. Use uber_eats_login first."}

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
        "session_file": str(api.SESSION_PATH.exists()),
        "preferences": prefs_summary,
    }


# ── Search (API) ────────────────────────────────────────────────────────────

async def search_restaurants(query: str) -> list[dict[str, Any]]:
    """Search Uber Eats for restaurants matching *query*."""
    raw = await api.get_feed(query=query)
    if "error" in raw:
        return [raw]

    stores = api.parse_feed_stores(raw)
    if not stores:
        return [{"error": f"No restaurants found for '{query}'. Try a different search term."}]
    return stores


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


# ── Item Options (browser fallback) ─────────────────────────────────────────

async def get_item_options(item_name: str, restaurant_url: str = "") -> dict[str, Any]:
    """Click on a menu item to see customization options. Still browser-based."""
    page = await manager.ensure_page(headless=True)

    if restaurant_url:
        if not restaurant_url.startswith("http"):
            restaurant_url = f"{BASE_URL}/store/{restaurant_url}"
        if "/store/" not in page.url:
            await page.goto(restaurant_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

    item_el = page.get_by_text(re.compile(re.escape(item_name), re.I)).first
    try:
        await item_el.click(timeout=TIMEOUT)
    except PwTimeout:
        return {"error": f"Could not find item '{item_name}' on the page."}

    await page.wait_for_timeout(2500)

    dialog = page.locator('[role="dialog"], [data-testid*="modal"], [data-testid*="customization"]')
    try:
        dialog_text = await dialog.first.inner_text(timeout=5000)
    except Exception:
        dialog_text = await page.inner_text("body")

    lines = [l.strip() for l in dialog_text.splitlines() if l.strip()]

    option_groups: list[dict[str, Any]] = []
    current_group: dict[str, Any] | None = None

    for line in lines:
        if line == item_name:
            continue
        is_header = (
            re.search(r"required|choose|select|pick|elige|escoge", line, re.I)
            or (len(line) < 50 and not re.search(r"[\$€£]\d", line) and line.endswith((":", "?")))
        )
        if is_header or (len(line) < 40 and line.isupper()):
            if current_group and current_group["options"]:
                option_groups.append(current_group)
            required = bool(re.search(r"required|obligatori", line, re.I))
            current_group = {"group": line, "required": required, "options": []}
        elif current_group is not None:
            price_match = re.search(r"[\$€£CLP]*\s*[\d,.]+", line)
            price = price_match.group(0).strip() if re.search(r"[\$€£+]\s*\d", line) and price_match else ""
            name = re.sub(r"[\$€£+]?\s*[\d,.]+$", "", line).strip()
            if name and len(name) > 1:
                current_group["options"].append({"name": name, "price": price})

    if current_group and current_group["options"]:
        option_groups.append(current_group)

    close_btn = page.locator('[aria-label="Close"], [data-testid*="close"], button:has-text("✕"), button:has-text("×")')
    try:
        await close_btn.first.click(timeout=3000)
    except Exception:
        await page.keyboard.press("Escape")

    return {
        "item": item_name,
        "option_groups": option_groups,
        "raw_lines": lines[:30] if not option_groups else [],
    }


# ── Cart (browser fallback) ─────────────────────────────────────────────────

async def add_to_cart(
    item_name: str,
    quantity: int = 1,
    restaurant_url: str | None = None,
) -> dict[str, Any]:
    """Find an item by name on the current restaurant page and add it to cart."""
    page = await manager.ensure_page(headless=True)

    if restaurant_url:
        if not restaurant_url.startswith("http"):
            restaurant_url = f"{BASE_URL}/store/{restaurant_url}"
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
    }


async def remove_from_cart(item_name: str) -> dict[str, Any]:
    """Remove an item from the cart by name."""
    page = await manager.ensure_page(headless=True)

    cart_btn = page.get_by_role("button", name=re.compile(r"cart|carrito|cesta", re.I))
    try:
        await cart_btn.first.click(timeout=TIMEOUT)
    except PwTimeout:
        cart_btn = page.locator('[data-testid*="cart"], [aria-label*="cart"], [aria-label*="Cart"]')
        try:
            await cart_btn.first.click(timeout=5000)
        except PwTimeout:
            return {"error": "Could not open cart."}

    await page.wait_for_timeout(2000)

    item_el = page.get_by_text(re.compile(re.escape(item_name), re.I)).first
    try:
        await item_el.wait_for(timeout=5000)
    except PwTimeout:
        return {"error": f"Item '{item_name}' not found in cart."}

    remove_btn = page.locator(
        '[data-testid*="remove"], [aria-label*="remove"], '
        '[aria-label*="Remove"], [aria-label*="delete"], '
        'button:has-text("Remove")'
    )
    try:
        await remove_btn.first.click(timeout=5000)
    except PwTimeout:
        decrease_btn = page.locator(
            '[data-testid*="decrease"], [aria-label*="decrease"], '
            'button:has-text("−"), button:has-text("-")'
        )
        try:
            await decrease_btn.first.click(timeout=5000)
        except PwTimeout:
            return {"error": f"Found '{item_name}' but could not find remove button."}

    await page.wait_for_timeout(1500)
    return {"success": True, "item": item_name, "message": f"Removed '{item_name}' from cart."}


async def _view_cart_browser() -> dict[str, Any]:
    """Open the cart panel and return its contents (DOM scrape)."""
    page = await manager.ensure_page(headless=True)

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
                    "message": "Cart summary from API (no draft order payload).",
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
        "message": "Cart from API.",
    }


# ── Checkout (browser fallback) ─────────────────────────────────────────────

async def _checkout_preview_browser() -> dict[str, Any]:
    """Navigate to checkout and return the order summary (DOM). Does NOT place the order."""
    page = await manager.ensure_page(headless=True)

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
            "Review the order above. Confirm with the user before placing. "
            "Ask which payment method to use and how much tip to add."
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
            out["note"] = "Browser fallback (no draft order for checkout API)."
        return out

    du = orders[0].get("uuid", "")
    chk = await api.get_checkout_presentation(du)
    if "error" in chk:
        return await _checkout_preview_browser()

    summary = api.parse_checkout_payloads(chk)
    if "error" in summary:
        return await _checkout_preview_browser()

    msg = (
        "Review the order above. Confirm with the user before placing. "
        "Ask which payment method to use and how much tip to add."
    )
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
        "message": msg,
    }


async def place_order() -> dict[str, Any]:
    """Click 'Place Order' on the checkout page. Opens headed browser for visibility."""
    page = await manager.ensure_page(headless=False)

    if "checkout" not in page.url:
        return {"error": "Not on the checkout page. Call uber_eats_checkout_preview first."}

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
        "message": "Order placed successfully!" if is_confirmed else "Order submitted. Check the browser for confirmation.",
        "url": page.url,
        "page_summary": "\n".join(lines[:20]),
    }


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
            f"Call uber_eats_restaurant_menu with restaurant_url=\"{su}\", then add items with "
            "uber_eats_add_to_cart (browser) until add-to-cart API is captured."
        ),
    }


async def get_menu_item_detail(
    store_uuid: str,
    section_uuid: str,
    subsection_uuid: str,
    menu_item_uuid: str,
) -> dict[str, Any]:
    """Full item payload including customizations (getMenuItemV1). IDs come from get_restaurant_menu items."""
    return await api.get_menu_item_v1(
        store_uuid=store_uuid,
        section_uuid=section_uuid,
        subsection_uuid=subsection_uuid,
        menu_item_uuid=menu_item_uuid,
    )


async def list_payment_methods() -> dict[str, Any]:
    """Payment methods from checkout eligibility (requires a non-empty cart)."""
    raw = await api.get_draft_orders()
    if "error" in raw:
        return raw
    orders = raw.get("data", {}).get("draftOrders") or []
    if not orders:
        return {"error": "No draft order — add items to cart first.", "payment_methods": []}

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
    return {
        "draft_order_uuid": du,
        "payment_methods": summary.get("payment_methods", []),
        "delivery_address": summary.get("delivery_address", ""),
    }


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
) -> dict[str, Any]:
    """Set payment card/profile and sync draft order (matches web: selector + updateDraftOrder)."""
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
    return {"success": True, "payment_profile_uuid": payment_profile_uuid, "response": out}


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
    page = await manager.ensure_page(headless=False)
    await page.goto(BASE_URL, wait_until="domcontentloaded")
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


# ── API Discovery ────────────────────────────────────────────────────────────

async def start_api_discovery() -> dict[str, Any]:
    """Launch a headed browser in API discovery mode.

    The user browses Uber Eats normally while we capture every /_p/api/
    and /api/ call with full request and response bodies.
    """
    page = await manager.launch_discovery()
    await page.goto(f"{BASE_URL}/", wait_until="domcontentloaded")

    return {
        "status": "discovery_started",
        "message": (
            "Browser opened in discovery mode. Browse Uber Eats normally — "
            "I'm recording every API call.\n\n"
            "Walk through these steps:\n"
            "  1. Search for a restaurant\n"
            "  2. Open it and add items to cart\n"
            "  3. View your cart\n"
            "  4. Go to checkout\n"
            "  5. Look at payment methods\n"
            "  6. Set a tip\n"
            "  7. Do NOT place the order (unless you want to)\n\n"
            "When done, call uber_eats_stop_discovery to see captured endpoints."
        ),
        "log_file": str(Path.home() / ".ubereats-api-log.jsonl"),
    }


async def stop_api_discovery() -> dict[str, Any]:
    """Stop discovery mode, save the session, and return a summary of captured endpoints."""
    await manager.save_session()
    summary = manager.get_api_log_summary()
    await manager.close()

    return {
        "status": "discovery_complete",
        "summary": summary,
    }


def get_discovery_log() -> dict[str, Any]:
    """Return the full API discovery log (all captured calls with bodies)."""
    records = manager.get_api_log()
    if not records:
        return {"message": "No API calls captured. Run uber_eats_discover_apis first."}
    return {
        "total_calls": len(records),
        "calls": records,
    }


def get_discovery_summary() -> dict[str, Any]:
    """Return a summary of discovered endpoints."""
    return manager.get_api_log_summary()
