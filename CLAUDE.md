# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with this repository.

## What is this

**Uber Eats MCP Server** — an MCP server that lets you order food from [Uber Eats](https://www.ubereats.com) directly from Claude Code or Cursor. Uses Playwright browser automation to interact with the Uber Eats website, capturing auth tokens from network traffic for session persistence.

This server is designed to be used **from Claude Code** or **Cursor** as the primary interface for ordering food without leaving your editor.

**Implementation note:** Cart add/remove and item customizations use the same JSON APIs as the Uber Eats website (`addItemsToDraftOrderV2`, `getMenuItemV1`, etc.). Playwright is reserved for login and placing the order until a submit API is integrated.

## Quick Start

```bash
cd uber-eats-mcp
uv sync
uv run playwright install chromium
```

Then use the MCP tools directly from Claude Code or Cursor.

## MCP Server

The server exposes **34 tools** for Claude to call natively:

### Authentication
| Tool | Description |
|------|-------------|
| `uber_eats_login` | Opens Chromium browser for manual login. Captures auth tokens from network traffic. Session saved for reuse — only login once. |
| `uber_eats_whoami` | Shows logged-in user info: name, email, address, preferences. |

### Delivery Address
| Tool | Description |
|------|-------------|
| `uber_eats_get_address` | Get current delivery address. |
| `uber_eats_set_address` | Change delivery address via address picker. Always confirm with user first. |

### Browse
| Tool | Description |
|------|-------------|
| `uber_eats_search` | Search for restaurants by keyword (e.g. "sushi", "pizza"). Returns name, rating, ETA, delivery fee. |
| `uber_eats_nearby_restaurants` | List popular/nearby restaurants from the home feed. |
| `uber_eats_restaurant_menu` | Get full categorized menu for a restaurant with prices. |
| `uber_eats_get_item_options` | View item customization options (sizes, extras, required choices). Check before adding to cart. |

### Order Flow
| Tool | Description |
|------|-------------|
| `uber_eats_add_to_cart` | Add item to cart by name. Supports quantity. |
| `uber_eats_remove_from_cart` | Remove item from cart by name. |
| `uber_eats_view_cart` | View cart: items, charges, total. |
| `uber_eats_checkout_preview` | Preview order summary with full price breakdown. Always show to user before placing order. |
| `uber_eats_place_order` | Place the order. ONLY after checkout_preview + explicit user confirmation. |
| `uber_eats_track_orders` | Track active and past orders. |

## Complete Order Example (Claude Code workflow)

Here's the full flow for ordering a pizza via Claude Code:

```
User: "I want to order a pizza"

1. Search for restaurants
   → uber_eats_search("pizza")
   → "Found 8 pizza restaurants: 1. Domino's (4.5★, 25 min)..."

2. User picks a restaurant
   → uber_eats_restaurant_menu("https://www.ubereats.com/store/dominos-pizza/...")
   → "Menu: Pizzas: Pepperoni $12.99, Margherita $11.99..."

3. Check item options if needed
   → uber_eats_get_item_options("Pepperoni Pizza")
   → "Size: Medium (required), Crust: Hand Tossed / Thin..."

4. Add to cart
   → uber_eats_add_to_cart("Pepperoni Pizza", quantity=1)
   → "Added Pepperoni Pizza x1 to cart"

5. Review cart
   → uber_eats_view_cart()
   → "Pepperoni Pizza $12.99, Delivery $2.49, Total $15.48"

6. Checkout preview
   → uber_eats_checkout_preview()
   → "Order: Pepperoni Pizza, Total: $15.48, Deliver to: 123 Main St"

7. User confirms → Place order
   → uber_eats_place_order()
   → "Order placed successfully!"

8. Track delivery
   → uber_eats_track_orders()
   → "Preparing your order... ETA 25 min"
```

## Architecture

```
uber-eats-mcp/
  server.py            → MCP server entry point (tools via FastMCP)
  api.py               → Authenticated HTTP client for /_p/api/* and related
  cart_api.py          → Cart payload helpers (add/remove line items)
  ubereats.py          → Tool implementations (API-first; Playwright for login / place_order)
  browser.py           → Browser lifecycle, session persistence, token capture
  pyproject.toml       → Dependencies (mcp, playwright)
  CLAUDE.md            → This file (agent guide)
```

## Setup for Claude Code

Add `.mcp.json` to your project root:
```json
{
  "mcpServers": {
    "uber-eats": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/uber-eats-mcp", "python", "server.py"]
    }
  }
}
```

## Setup for Cursor

Add to `.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "uber-eats": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/uber-eats-mcp", "python", "server.py"]
    }
  }
}
```

## Agent Behavior Rules

When using this as an agent (from Claude Code or Cursor), follow these rules:

- **Always confirm before placing an order.** Before calling `uber_eats_place_order`, show the user the full checkout summary (items, prices, total, address) and ask for explicit confirmation. Never place an order without the user saying "yes" or equivalent.
- **Always confirm before changing the delivery address.** Before calling `uber_eats_set_address`, tell the user which address will be set and ask for confirmation.
- **Check item options before adding to cart.** If a menu item might have customizations (sizes, extras, required choices), call `uber_eats_get_item_options` first so the user can choose.
- **Show prices clearly.** Always display prices with currency symbols.
- **Handle login gracefully.** If any tool returns a "not logged in" error, immediately suggest calling `uber_eats_login`.
- **Preview before ordering.** Always call `uber_eats_checkout_preview` before `uber_eats_place_order`.
- **Be helpful with browsing.** If the user is unsure what to eat, use `uber_eats_nearby_restaurants` to show popular options.

## Session Management

- **Browser session**: Saved to `~/.ubereats-session.json` (cookies, localStorage)
- **Config/tokens**: Saved to `~/.ubereats-config.json` (captured auth tokens, user info)
- Sessions persist across restarts — login once, reuse forever (until session expires)
- If tools return auth errors, re-run `uber_eats_login`
- **Login debug trace**: each `uber_eats_login` run (when `UBEREATS_BROWSER_DEBUG` is not `0`) overwrites `~/.ubereats-login-debug.jsonl` with JSON lines: URL, `has_auth_signals`, Uber cookie **names** (not values), sign-in link counts, `save_session` outcome. The tool response may include `debug_log` with the file path. Disable with `UBEREATS_BROWSER_DEBUG=0`.

## How It Works

1. **Login**: Opens a real Chromium window. User logs in manually. The server intercepts network traffic to capture auth tokens, cookies, CSRF tokens, and user info.
2. **Browse / cart / checkout**: Uses `httpx` against Uber’s `/_p/api/*` endpoints (same session cookies as the browser).
3. **Place order**: Still uses Playwright to click through checkout until a submit API is implemented.
4. **Session persistence**: Browser state (cookies/storage) is saved to disk so the user only logs in once.

## Important Notes

- **Browser required for login / place order**: Playwright needs Chromium installed (`uv run playwright install chromium`).
- **Headed mode**: Login, `set_address`, cart/checkout browser fallbacks, and `place_order` use the same headed Chromium + network interception stack as API discovery (`ensure_interactive_page` in `browser.py`), including the same default window size (1280×900). To use a smaller window, set `UBEREATS_BROWSER_VIEWPORT=1100x800` (or similar) in the MCP server env. Most other tools use HTTP only.
- **Session expiry**: Uber Eats sessions expire periodically. Re-run login when you get auth errors.
- **DOM changes**: Uber Eats may update their website. If selectors break, update `ubereats.py`.
- **Rate limiting**: Don't call tools too rapidly. The server includes reasonable waits between actions.
