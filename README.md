# Claude, I’m hungry. 🍜

Order food from Uber Eats through natural language in **Cursor** or **Claude Code** — with an assistant that can browse menus, prep checkout, and (when you say so) place the order.

This project is an [**MCP server**](https://modelcontextprotocol.io/) (Model Context Protocol): a small program your AI client starts over **stdio** so it can call tools like `uber_eats_search` and `uber_eats_checkout_preview`.  
It is **not** a Cursor extension / VS Code plugin.

Under the hood: **Uber web APIs where possible**, and **Playwright** for the parts that still need the browser (login, item customization UI, add/remove cart, address picker, and the final “Place order” click until we capture a submit API).

---

## What you can do

- **Browse & decide fast**
  - Search and browse nearby stores
  - Pull full menus with categories and item prices
  - Get item detail payloads (including customization metadata where available)
- **Cart & checkout prep**
  - View cart via API (draft order + carts view)
  - Checkout preview with totals, fees, tip options, and delivery address
  - List eligible payment methods (when cart is non-empty)
  - Set checkout tip / select payment / apply promo / view savings
- **Orders**
  - Track **active** orders (and past orders)
  - “Reorder helper” that resolves the store + previous items (adding items is still browser-based today)
- **Reverse-engineering mode**
  - Run an API discovery browser that logs full request/response bodies to `~/.ubereats-api-log.jsonl`

---

## Quick start (after `git clone`)

From inside **`uber-eats-mcp/`** (this folder):

**macOS / Linux**

```bash
chmod +x scripts/setup.sh
./scripts/setup.sh
```

**Windows (PowerShell)**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

**Manual (same as the scripts)**

```bash
uv sync
uv run playwright install chromium
```

The setup script prints a **ready-to-paste JSON** block with the correct path to this clone.

---

## Connect Cursor

1. Open **Cursor → Settings → MCP** (or edit the MCP config file your Cursor version uses).
2. **Merge** the `uber-eats` entry into your existing `mcpServers` object (do not delete other servers).
3. Restart Cursor or reload MCP.

If you prefer a **project-local** config, add `.cursor/mcp.json` in a project and paste the same `mcpServers` snippet.

Example shape (use the **exact output** from `./scripts/setup.sh` so paths match your machine):

```json
{
  "mcpServers": {
    "uber-eats": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/uber-eats-mcp", "uber-eats-mcp"]
    }
  }
}
```

Requires **`uv`** on your `PATH`: [install uv](https://docs.astral.sh/uv/getting-started/installation/).

---

## Connect Claude Code

Add an **`.mcp.json`** at the root of the project you open in Claude Code (or use the global location your version documents). Use the same `mcpServers` JSON as above.

---

## Typical flow (what the assistant should do)

1. `uber_eats_login`
2. `uber_eats_get_preferences` (optional personalization)
3. `uber_eats_get_address` (confirm delivery address)
4. `uber_eats_search` or `uber_eats_nearby_restaurants`
5. `uber_eats_restaurant_menu` → decide items
6. If needed: `uber_eats_menu_item_detail` / `uber_eats_get_item_options`
7. `uber_eats_add_to_cart` (browser today)
8. `uber_eats_view_cart`
9. `uber_eats_checkout_preview`
10. Optional: `uber_eats_list_payment_methods`, `uber_eats_set_checkout_payment`, `uber_eats_set_checkout_tip`, `uber_eats_apply_promo`
11. Only after explicit confirmation: `uber_eats_place_order` (browser until submit API is captured)

---

## Run without MCP (debug)

```bash
uv run uber-eats-mcp
```

or

```bash
uv run python server.py
```

---

## What gets stored locally

Session and preferences are under your home directory (e.g. `~/.ubereats-session.json`, `~/.ubereats-preferences.json`). Do not commit those.

---

## Prerequisites

- **Python ≥ 3.12**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** (recommended) or another way to install from `pyproject.toml`
- **Chromium** via Playwright (`playwright install chromium` — included in the setup scripts)

---

## More tools

See the tool list in Cursor/Claude Code after connecting. The implementation lives in `server.py`.

---

## Legal

Uber Eats is a third-party service. Use automation responsibly and in line with their terms. This project is unofficial.
