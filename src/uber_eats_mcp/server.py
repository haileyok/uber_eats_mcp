#!/usr/bin/env python3
"""
Uber Eats MCP Server
~~~~~~~~~~~~~~~~~~~~
Exposes tools for ordering food on Uber Eats.
Uses direct API calls for browsing, cart mutations, checkout prep, and item options;
browser for login, optional address picker, and place_order fallback when API submit fails.

Run directly:  python server.py
Or via MCP:    configured in .cursor/mcp.json or .mcp.json
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from . import ubereats
from . import preferences
from . import recommender
from .browser import manager, cdp_enabled


@asynccontextmanager
async def _lifespan(app):
    """Start/stop the background keepalive task within the running event loop."""
    if cdp_enabled():
        manager.start_keepalive_task()
    try:
        yield
    finally:
        await manager.stop_keepalive_task()
        await manager.close_cdp()


mcp = FastMCP(
    "uber-eats",
    lifespan=_lifespan,
    instructions="\n".join([
        "Uber Eats assistant. Order food and groceries from Uber Eats.",
        "",
        "VOICE (critical): When you speak to the human, sound like a friend helping them order—warm, short, no jargon.",
        "Never say: API, MCP, endpoint, JSON, session, CSRF, payload, 'returned by the system', or similar.",
        "Paraphrase tool results in plain language (e.g. empty payment_methods → 'You’ll pick a card when we check out—that’s normal').",
        "If a JSON field is named assistant_hint, fix_for_assistant, or debug_*, use it for your reasoning only—do not quote it to the user.",
        "",
        "Order flow (happy path, fewest steps): probe session → confirm address → search (product name) OR pick a store → get_item_options only if the item has required choices → add_to_cart → view_cart → checkout_preview → payment + tip → explicit confirm → place_order → track_orders.",
        "Cart mutations use the Uber JSON API (add/remove/update quantity). Checkout preview surfaces real totals, fees, and promos (strikethrough prices, Uber One, banners)—that is where the user sees final pricing, not only at add time.",
        "When the user asks to change how many of something: call uber_eats_view_cart if needed, then uber_eats_update_cart_quantity with shopping_cart_item_uuid from items[] (best) or item_name.",
        "OFFERS: If search items[], menu rows, cart lines, or checkout items include on_offer, offer_summary, offer_badge, regular_price vs line_price, or strikethrough pricing—say the deal in plain language (e.g. on sale, was X, now Y, or the badge text). Do not skip mentioning a visible promotion.",
        "",
        "LOGIN LAST (critical):",
        "  Never call uber_eats_login as the first tool. It opens a browser window and often fails if another login runs in parallel.",
        "  Start with uber_eats_get_preferences, then uber_eats_get_address (and uber_eats_whoami if you need account clarity)—they use saved session files and do NOT open the login browser.",
        "  Call uber_eats_login only when a tool returns a clear not-signed-in / session / auth error, or the user explicitly asks to sign in.",
        "  uber_eats_login(force=false) skips the browser if the saved session already passes Uber’s API—use force=true only to re-authenticate or switch accounts.",
        "",
        "SEARCH BEFORE FULL MENU (critical):",
        "  If the user names a specific product (e.g. Red Bull, milk, diapers) or the store is a supermarket / grocery / Jumbo / Líder / large market: call uber_eats_search with that product query FIRST.",
        "  Do NOT call uber_eats_restaurant_menu for the entire store in that case—grocery menus can be 100k+ characters and exceed the client tool-result token limit.",
        "  Prefer: uber_eats_search → uber_eats_add_to_cart with store_uuid + menu_item_uuid (section_uuid must not equal store_uuid—if unsure, omit section and the server resolves from the menu).",
        "  After search hits, avoid re-loading the whole grocery menu unless the user wants to browse categories.",
        "  Use uber_eats_restaurant_menu mainly for smaller restaurants when a full menu browse is reasonable.",
        "",
        "If any tool result JSON contains an \"error\" key or clear failure, do NOT tell the user the action succeeded—fix session (login) or retry.",
        "",
        "PERSONALIZATION: On first interaction each session (before login unless the user asked to sign in):",
        "  1. Call uber_eats_get_preferences to load user defaults",
        "  2. If a default_address is set, switch to it automatically (no need to ask)",
        "  3. If the user seems undecided, offer: 'Want me to suggest something based on your mood?'",
        "  4. When ordering for groups, ask about the social context (alone, couple, family, friends) and adjust suggestions",
        "  5. At checkout, mention default payment and tip from preferences, but still confirm with the user",
        "",
        "IMPORTANT: Before searching for food or browsing stores, ALWAYS:",
        "  1. If no default_address in preferences, call uber_eats_get_address to check the current delivery address",
        "  2. Show the address to the user and ask them to confirm it is correct",
        "  3. If the user wants a different address, call uber_eats_saved_addresses to list options, then uber_eats_switch_address to change",
        "  4. Only proceed to search/browse AFTER the user confirms the address",
        "",
        "CHECKOUT — MANDATORY CONFIRMATION FLOW:",
        "  You MUST follow ALL of these steps IN ORDER. Do NOT skip any step.",
        "  Do NOT call place_order until the user has explicitly said 'yes', 'confirm', 'place it', or similar.",
        "",
        "  Step 1: Call checkout_preview to get the full price breakdown.",
        "  Step 2: Show the user a clear summary including:",
        "    - Restaurant name",
        "    - All items with quantities and prices",
        "    - Subtotal, delivery fee, service fee",
        "    - Total amount",
        "    - Delivery address",
        "    - Current payment method",
        "  Step 3: Ask the user which payment method to use (mention default if set in preferences).",
        "  Step 4: Ask the user how much tip to add (mention default_tip_percent if set, or suggest 10%/15%/20%/none).",
        "  Step 5: Optionally use uber_eats_set_checkout_tip / uber_eats_set_checkout_payment with draft_order_uuid from checkout_preview.",
        "  Step 6: After payment and tip are decided, show the FINAL summary with the grand total (including tip).",
        "  Step 7: Ask: 'Should I place the order?' and WAIT for explicit confirmation.",
        "  Step 8: ONLY after the user explicitly confirms, call place_order (API via Chrome CDP).",
        "",
        "  NEVER call place_order without completing the confirmation steps above.",
        "  NEVER call place_order in the same turn as checkout_preview.",
        "  If the user says 'cancel', 'no', 'wait', or anything other than clear confirmation, do NOT place the order.",
        "",
        "IMPORTANT: If a tool returns an error about not being logged in after session probe, call uber_eats_login (single login at a time).",
    ]),
)


# ── Auth ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def uber_eats_login(force: bool = False) -> str:
    """
    Open a visible browser window so the user can sign in to Uber Eats.
    By default, if ~/.ubereats-session.json already works for Uber’s API, returns success
    without opening a window (avoids wiping/restoring session by mistake).

    Args:
        force: If true, always run the full login flow (clears saved session first). Use to
            switch accounts or recover from a bad cookie file.
    """
    result = await ubereats.login(force=force)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_whoami() -> str:
    """
    Get current user profile info and login status from saved session (no login browser).
    Use with get_preferences/get_address before deciding whether uber_eats_login is needed.
    """
    result = await ubereats.whoami()
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Address ──────────────────────────────────────────────────────────────────

@mcp.tool()
async def uber_eats_saved_addresses() -> str:
    """
    List all saved delivery addresses. Shows labeled addresses (home, work, etc.)
    that the user has saved in their Uber Eats account.
    Use uber_eats_switch_address to switch between them.
    """
    result = await ubereats.list_saved_addresses()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_switch_address(label: str) -> str:
    """
    Switch to a saved delivery address by its label (e.g. 'home', 'work').
    Call uber_eats_saved_addresses first to see available addresses.

    Args:
        label: The label or name of the saved address to switch to
    """
    result = await ubereats.switch_to_address(label)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_get_address() -> str:
    """
    Current delivery address from saved session/config (no login browser).
    Prefer this (with get_preferences) before uber_eats_login when starting a chat.
    """
    result = await ubereats.get_addresses()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_set_address(address: str) -> str:
    """
    Change the delivery address. Always confirm with the user before calling this.
    Opens a headed browser to set the new address via the address picker.

    Args:
        address: New delivery address (street address or landmark)
    """
    result = await ubereats.set_address(address)
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Browse ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def uber_eats_search(query: str) -> str:
    """
    Global Uber Eats search (same flow as the web search bar: getSearchFeedV1, merged
    with getFeedV1 for extra catalog rows). Response JSON has:
    - stores: list of merchants (name, uuid, url, eta, …)
    - items: catalog hits (menu_item_uuid, section_uuid, subsection_uuid, store_uuid)
      for product-style queries like energy drinks or grocery SKUs
    - feed_item_types: raw feed module types (debugging)

    Use items[] for quick add via menu_item_detail / add_to_cart; use stores[] for browsing.

    Args:
        query: Search text (cuisine, store, or product, e.g. 'sushi', 'red bull')
    """
    results = await ubereats.search_restaurants(query)
    return json.dumps(results, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_nearby_restaurants(limit: int = 15) -> str:
    """
    List nearby/popular restaurants from the Uber Eats home feed.
    Good for browsing when the user doesn't have a specific craving.
    Returns open/closed status for each restaurant.

    Args:
        limit: Max restaurants to return (default 15)
    """
    results = await ubereats.list_nearby_restaurants(limit)
    return json.dumps(results, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_restaurant_menu(restaurant_url: str) -> str:
    """
    Full menu for one store. OK for small restaurants; for supermarkets/groceries
    the payload can be enormous (100k+ chars) and exceed the client's tool limit—
    prefer uber_eats_search with the product name first, then item-level tools.

    Args:
        restaurant_url: Full URL (https://www.ubereats.com/store/...) or restaurant slug
    """
    menu = await ubereats.get_restaurant_menu(restaurant_url)
    return json.dumps(menu, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_get_item_options(item_name: str, restaurant_url: str = "") -> str:
    """
    Get customization options for a menu item (sizes, extras, required choices).
    Each group includes required, min_permitted, max_permitted, pick_one (when API provides them).
    Call this before add_to_cart if the item has options/modifiers.

    Args:
        item_name: Name of the menu item to inspect
        restaurant_url: Optional restaurant URL if not already on the page
    """
    result = await ubereats.get_item_options(item_name, restaurant_url)
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Cart ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def uber_eats_add_to_cart(
    item_name: str = "",
    quantity: int = 1,
    restaurant_url: str = "",
    store_uuid: str = "",
    section_uuid: str = "",
    subsection_uuid: str = "",
    menu_item_uuid: str = "",
) -> str:
    """
    Add a line via addItemsToDraftOrderV2 + getMenuItemV1 (same stack for restaurants and grocery).

    Best: pass store_uuid + menu_item_uuid (required). The server loads getStoreV1 and resolves the
    real section_uuid / subsection_uuid from the menu—so wrong search data (e.g. section_uuid =
    store_uuid) is fixed automatically when the SKU exists in the menu.

    Optional: section_uuid, subsection_uuid, item_name from uber_eats_search items[] or restaurant_menu.

    Fallback: restaurant_url + item_name (fuzzy match on full menu; more error-prone).

    If the item has required customizations, call uber_eats_get_item_options first.

    Args:
        item_name: Display / fuzzy name (optional if UUID path fills title from getMenuItemV1)
        quantity: How many to add (default 1)
        restaurant_url: Store URL or slug (legacy path)
        store_uuid: Merchant UUID (from search or menu payload)
        section_uuid: Catalog section UUID (never the same as store_uuid; optional if resolvable from menu)
        subsection_uuid: Submenu UUID (often empty; server retries with menu_item_uuid if needed)
        menu_item_uuid: SKU / catalog item UUID
    """
    result = await ubereats.add_to_cart(
        item_name=item_name,
        quantity=quantity,
        restaurant_url=restaurant_url or None,
        store_uuid=store_uuid,
        section_uuid=section_uuid,
        subsection_uuid=subsection_uuid,
        menu_item_uuid=menu_item_uuid,
    )
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_remove_from_cart(item_name: str) -> str:
    """
    Remove an item from the cart by name.

    Args:
        item_name: Name of the item to remove
    """
    result = await ubereats.remove_from_cart(item_name)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_update_cart_quantity(
    quantity: int,
    item_name: str = "",
    shopping_cart_item_uuid: str = "",
    grocery_store: bool = False,
) -> str:
    """
    Change the quantity of a line already in the cart (same API the site uses for line updates).

    Best: pass shopping_cart_item_uuid from uber_eats_view_cart items[].shopping_cart_item_uuid.
    Otherwise match by item_name (partial match ok).

    For large markets / grocery carts, set grocery_store=true if quantity updates fail without it.

    Args:
        quantity: New quantity (1 or more)
        item_name: Line title to match if UUID not provided
        shopping_cart_item_uuid: From view_cart items[] (preferred)
        grocery_store: Hint for grocery / convenience store drafts
    """
    result = await ubereats.update_cart_line_quantity(
        quantity,
        item_name=item_name,
        shopping_cart_item_uuid=shopping_cart_item_uuid,
        grocery_store=grocery_store,
    )
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_view_cart() -> str:
    """
    View the current cart: line titles, quantities, shopping_cart_item_uuid per line (for updates),
    and draft_order_uuid. Call before checkout so the user can review; use UUIDs when changing qty.
    """
    cart = await ubereats.view_cart()
    return json.dumps(cart, indent=2, ensure_ascii=False)


# ── Checkout & Order ─────────────────────────────────────────────────────────

@mcp.tool()
async def uber_eats_list_payment_methods() -> str:
    """
    List eligible payment methods for the current cart (checkout API).
    Requires items in the cart. Use after add_to_cart or when checkout fails
    to show card / wallet options.
    """
    result = await ubereats.list_payment_methods()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_search_history() -> str:
    """
    Recent search queries from the user's Uber Eats search history (API).
    """
    result = await ubereats.search_history()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_apply_promo(code: str) -> str:
    """
    Apply a promo code (e.g. from email or offers). May succeed or return an error
    if the code is invalid or expired.

    Args:
        code: Promotion code string
    """
    result = await ubereats.apply_promo(code)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_list_profiles() -> str:
    """
    List Uber account profiles (Personal, Business, Family, etc.) and which is selected.
    Use uber_eats_switch_profile to change the active profile before ordering.
    """
    result = await ubereats.list_uber_profiles()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_switch_profile(profile_uuid: str) -> str:
    """
    Switch the active Uber profile by UUID from uber_eats_list_profiles.

    Args:
        profile_uuid: Profile UUID to select
    """
    result = await ubereats.switch_uber_profile(profile_uuid)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_set_checkout_tip(draft_order_uuid: str, tip_percent: int) -> str:
    """
    Set the delivery tip on the current draft order (API). Use tip values that match
    Uber's buttons: typically 0, 5, 10, 15, or 20. Call uber_eats_checkout_preview
    first and pass draft_order_uuid from the response.

    Args:
        draft_order_uuid: Draft order UUID from checkout preview
        tip_percent: Tip percentage (e.g. 10 for 10%)
    """
    result = await ubereats.checkout_set_tip(draft_order_uuid, tip_percent)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_set_checkout_payment(
    draft_order_uuid: str,
    payment_profile_uuid: str,
    use_credits: str = "",
    set_as_default: str = "",
) -> str:
    """
    Select payment method for checkout (API). Use payment UUIDs from
    uber_eats_list_payment_methods. Optionally set use_credits to 'true' or 'false'.
    Set set_as_default to 'true' to also PATCH the account default card (payments profilePatch).

    Args:
        draft_order_uuid: Draft order UUID from checkout preview
        payment_profile_uuid: Payment profile UUID to charge
        use_credits: If 'true' or 'false', toggles Uber Cash/credits; empty leaves unchanged
        set_as_default: If 'true', persist as default payment on the selected Uber profile
    """
    uc: bool | None = None
    if use_credits.strip().lower() == "true":
        uc = True
    elif use_credits.strip().lower() == "false":
        uc = False
    sad = set_as_default.strip().lower() in ("1", "true", "yes")
    result = await ubereats.checkout_set_payment_profile(
        draft_order_uuid,
        payment_profile_uuid,
        use_credits=uc,
        set_as_default=sad,
    )
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_checkout_savings() -> str:
    """
    Fetch checkout savings / promo carousel data for the current draft order (API).
    Requires a non-empty cart.
    """
    result = await ubereats.checkout_savings()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_checkout_preview() -> str:
    """
    Preview the order with full price breakdown. Always call this before
    place_order and show the summary to the user for confirmation.
    Does NOT place the order.

    After showing the preview, you MUST:
    1. Show a clear summary to the user (items, fees, total, address, payment)
    2. Ask which payment method to use
    3. Ask how much tip to add
    4. Show the final total WITH tip
    5. Ask for EXPLICIT confirmation before proceeding

    Do NOT call place_order until the user explicitly confirms.
    """
    result = await ubereats.checkout_preview()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_place_order() -> str:
    """
    Place the order via API (checkoutOrdersByDraftOrdersV1) through Chrome CDP.
    NEVER call this without explicit user confirmation.

    REQUIRED before calling — ALL of these must be true:
    1. checkout_preview was called and summary was shown to the user
    2. User chose a payment method
    3. User specified tip amount (or explicitly said no tip)
    4. User saw the final total (including tip)
    5. User explicitly said 'yes', 'confirm', 'place it', or similar

    If ANY of these are missing, do NOT call this tool.
    If the user said 'cancel', 'no', 'wait', or is uncertain, do NOT call this tool.
    """
    result = await ubereats.place_order()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_track_orders() -> str:
    """
    Track active and past orders. Shows order status, restaurant, items, and totals.
    Uses direct API for fast results.
    """
    result = await ubereats.track_orders()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_track_active_orders() -> str:
    """
    In-progress / active orders only (delivery, preparing, etc.). Lighter than track_orders.
    """
    result = await ubereats.track_active_orders()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_reorder(order_uuid: str) -> str:
    """
    Look up a past order by UUID and return store UUID + previous line items so the user
    can open the menu and add items again. Cart mutations still use the browser until
    add-to-cart API is captured.

    Args:
        order_uuid: Past order UUID from uber_eats_track_orders
    """
    result = await ubereats.reorder_from_past_order(order_uuid)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_menu_item_detail(
    store_uuid: str,
    section_uuid: str,
    subsection_uuid: str,
    menu_item_uuid: str,
) -> str:
    """
    Load one menu item with customization options (API). UUIDs come from uber_eats_restaurant_menu
    item entries (section_uuid, subsection_uuid, uuid).

    Args:
        store_uuid: Restaurant store UUID
        section_uuid: Section UUID from the menu item
        subsection_uuid: Subsection UUID from the menu item
        menu_item_uuid: Item UUID (catalog item)
    """
    result = await ubereats.get_menu_item_detail(
        store_uuid,
        section_uuid,
        subsection_uuid,
        menu_item_uuid,
    )
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Preferences ──────────────────────────────────────────────────────────────

@mcp.tool()
async def uber_eats_get_preferences() -> str:
    """
    Local preferences file (default address label, favorites, etc.). No browser.
    Call first each session; then get_address—avoid opening login until an API
    tool proves the Uber session is missing or expired.
    """
    prefs = preferences.load_preferences()
    return json.dumps(prefs, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_set_preference(key: str, value: str) -> str:
    """
    Update a user preference by dotted key path.

    Examples:
      key="default_address", value="home"
      key="default_tip_percent", value="15"
      key="dietary.restrictions", value='["no peanuts", "lactose intolerant"]'
      key="budget.default_per_person", value="15000"
      key="budget.currency", value="CLP"
      key="language", value="es"

    Args:
        key: Dotted path to the preference (e.g. 'dietary.avoid')
        value: The value to set (JSON string for lists/objects, plain string for scalars)
    """
    try:
        parsed_value = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed_value = value

    prefs = preferences.update_preference(key, parsed_value)
    return json.dumps({"success": True, "key": key, "value": parsed_value}, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_add_favorite(
    kind: str,
    name: str,
    uuid: str = "",
    url: str = "",
    restaurant: str = "",
) -> str:
    """
    Save a restaurant or menu item as a favorite.

    Args:
        kind: 'restaurants' or 'items'
        name: Name of the restaurant or item
        uuid: Restaurant UUID (for restaurants)
        url: Restaurant URL (for restaurants)
        restaurant: Which restaurant this item is from (for items)
    """
    if kind == "restaurants":
        item = {"name": name, "uuid": uuid, "url": url}
    else:
        item = {"name": name, "restaurant": restaurant}

    prefs = preferences.add_favorite(kind, item)
    return json.dumps(
        {"success": True, "added": item, "total_favorites": len(prefs["favorites"].get(kind, []))},
        indent=2, ensure_ascii=False,
    )


@mcp.tool()
async def uber_eats_build_taste_profile() -> str:
    """
    Analyze past order history and build/update the user's taste profile.
    Identifies cuisine preferences, favorite restaurants, and ordering patterns.
    Call this periodically or when the user wants updated recommendations.
    """
    result = await ubereats.analyze_order_history()
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Recommendations ──────────────────────────────────────────────────────────

@mcp.tool()
async def uber_eats_recommend(
    mood: str = "",
    social_context: str = "alone",
    craving: str = "",
) -> str:
    """
    Get personalized food recommendations based on mood and social context.

    If mood is empty, returns the available mood options for the user to pick from.

    Mood options: tired, healthy, celebrating, stressed, adventurous, quick, craving
    Social context options: alone, couple, family, friends (or custom contexts from preferences)

    Args:
        mood: How the user is feeling (e.g. 'tired', 'celebrating', 'adventurous')
        social_context: Who they're eating with (e.g. 'alone', 'family', 'friends')
        craving: Free-text craving if mood is 'craving' (e.g. 'sushi', 'burgers')
    """
    if not mood:
        return json.dumps({
            "mood_options": recommender.get_mood_options(),
            "social_context_options": recommender.get_social_context_options(),
            "message": "Ask the user: How are you feeling? Who are you eating with?",
        }, indent=2, ensure_ascii=False)

    nearby = await ubereats.list_nearby_restaurants(limit=30)
    if isinstance(nearby, list) and nearby and "error" in nearby[0]:
        nearby = []

    result = recommender.recommend(
        mood=mood,
        social_context=social_context,
        nearby_stores=nearby,
        craving_text=craving,
    )
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_suggest_cart(
    restaurant_url: str,
    social_context: str = "alone",
) -> str:
    """
    Suggest what to order from a specific restaurant based on group size
    and preferences. Analyzes the menu and recommends mains, sides, and drinks.

    Args:
        restaurant_url: Restaurant URL or slug
        social_context: Who you're eating with (alone, couple, family, friends)
    """
    menu = await ubereats.get_restaurant_menu(restaurant_url)
    if "error" in menu:
        return json.dumps(menu, indent=2, ensure_ascii=False)

    sections = menu.get("sections", [])
    result = recommender.build_cart_suggestion(social_context, sections)
    result["restaurant"] = menu.get("restaurant", "")
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_keepalive() -> str:
    """
    Refresh the Uber Eats session by navigating to ubereats.com through the
    persistent Chrome instance. This refreshes sliding session cookies so the
    session doesn't expire during long idle periods.

    The server also runs an internal keepalive task automatically (every
    UBEREATS_KEEPALIVE_INTERVAL_HOURS, default 4h). This tool is a secondary
    mechanism — call it manually or via a scheduler if you want an extra ping.
    """
    result = await manager.keepalive()
    return json.dumps({"result": result}, indent=2, ensure_ascii=False)


def main() -> None:
    """Entry point for `uv run uber-eats-mcp` / `uber-eats-mcp` after install."""
    # Keepalive task lifecycle is handled by the FastMCP lifespan context.
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
