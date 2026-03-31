#!/usr/bin/env python3
"""
Uber Eats MCP Server
~~~~~~~~~~~~~~~~~~~~
Exposes tools for ordering food on Uber Eats.
Uses direct API calls for browsing, cart, checkout prep (fast); browser for login,
add-to-cart, item options, address picker, and place_order until submit API exists.

Run directly:  python server.py
Or via MCP:    configured in .cursor/mcp.json or .mcp.json
"""

from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

import ubereats
import preferences
import recommender
from browser import manager

mcp = FastMCP(
    "uber-eats",
    instructions="\n".join([
        "Uber Eats assistant. Order food and groceries from Uber Eats.",
        "",
        "Order flow: login → load preferences → confirm address → search → get_restaurant_menu → get_item_options (if customizable) → add_to_cart → view_cart → checkout_preview → confirm with user → place_order → track_orders",
        "Cart and checkout previews use the Uber API when logged in (fast). Browser fallback only if the API fails.",
        "",
        "PERSONALIZATION: On first interaction each session:",
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
        "  Step 8: ONLY after the user explicitly confirms, call place_order (browser until submit API is implemented).",
        "",
        "  NEVER call place_order without completing the confirmation steps above.",
        "  NEVER call place_order in the same turn as checkout_preview.",
        "  If the user says 'cancel', 'no', 'wait', or anything other than clear confirmation, do NOT place the order.",
        "",
        "IMPORTANT: If a tool returns an error about not being logged in, call uber_eats_login first.",
    ]),
)


# ── Auth ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def uber_eats_login() -> str:
    """
    Open a visible browser window so the user can log in to Uber Eats.
    Captures auth tokens and session data from network traffic.
    Session is saved for reuse — only need to login once.
    Call this before any other tool if the user is not yet logged in.
    """
    result = await ubereats.login()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_whoami() -> str:
    """
    Get current user profile info and login status.
    Shows name, email, delivery address, and session info.
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
    Get the current delivery address. Use this to verify where food will be delivered.
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
    Search Uber Eats for restaurants matching the query.
    Returns restaurants with name, URL, rating, ETA, and open/closed status.
    Use the UUID or URL in subsequent calls to get_restaurant_menu.

    Args:
        query: What to search for (e.g. 'sushi', 'pizza', 'burgers')
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
    Get the full menu for a restaurant. Pass the URL or slug from search results.
    Returns categorized menu items with names, prices, and descriptions.

    Args:
        restaurant_url: Full URL (https://www.ubereats.com/store/...) or restaurant slug
    """
    menu = await ubereats.get_restaurant_menu(restaurant_url)
    return json.dumps(menu, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_get_item_options(item_name: str, restaurant_url: str = "") -> str:
    """
    Get customization options for a menu item (sizes, extras, required choices).
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
    item_name: str,
    quantity: int = 1,
    restaurant_url: str = "",
) -> str:
    """
    Add a menu item to the cart. The browser must be on a restaurant page
    (or provide restaurant_url). If the item has required customizations,
    call uber_eats_get_item_options first.

    Args:
        item_name: Name of the menu item to add
        quantity: How many to add (default 1)
        restaurant_url: Optional restaurant URL if not already on the page
    """
    result = await ubereats.add_to_cart(
        item_name,
        quantity=quantity,
        restaurant_url=restaurant_url or None,
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
async def uber_eats_view_cart() -> str:
    """
    View the current cart contents with items, charges breakdown, and total.
    Call this before checkout to let the user review their order.
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
) -> str:
    """
    Select payment method for checkout (API). Use payment UUIDs from
    uber_eats_list_payment_methods. Optionally set use_credits to 'true' or 'false'.

    Args:
        draft_order_uuid: Draft order UUID from checkout preview
        payment_profile_uuid: Payment profile UUID to charge
        use_credits: If 'true' or 'false', toggles Uber Cash/credits; empty leaves unchanged
    """
    uc: bool | None = None
    if use_credits.strip().lower() == "true":
        uc = True
    elif use_credits.strip().lower() == "false":
        uc = False
    result = await ubereats.checkout_set_payment_profile(
        draft_order_uuid,
        payment_profile_uuid,
        use_credits=uc,
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
    Place the order. NEVER call this without explicit user confirmation.

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
    Get the user's saved preferences: default address, payment, tip,
    dietary restrictions, favorites, budget, social contexts, taste profile,
    and mood history. Call this at the start of each session to personalize.
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


# ── API Discovery ────────────────────────────────────────────────────────────

@mcp.tool()
async def uber_eats_discover_apis() -> str:
    """
    Open a headed browser in API discovery mode. The user browses
    Uber Eats normally while every /_p/api/ and /api/ call is
    captured with full request and response bodies.

    Call uber_eats_stop_discovery when done to see captured endpoints.
    """
    result = await ubereats.start_api_discovery()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_stop_discovery() -> str:
    """
    Stop API discovery mode, save the session, and return a summary
    of all captured API endpoints with call counts and sample payloads.
    """
    result = await ubereats.stop_api_discovery()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def uber_eats_discovery_log() -> str:
    """
    Return the full API discovery log — all captured calls with
    complete request and response bodies. Use after uber_eats_stop_discovery
    to inspect specific endpoints in detail.
    """
    result = ubereats.get_discovery_log()
    return json.dumps(result, indent=2, ensure_ascii=False)


def main() -> None:
    """Entry point for `uv run uber-eats-mcp` / `uber-eats-mcp` after install."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
