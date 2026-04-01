# Claude, I’m hungry. 🍜

Order food from Uber Eats through natural language in **Cursor** or **Claude Code** — with an assistant that can browse menus, prep checkout, and (when you say so) place the order.

This project is an [**MCP server**](https://modelcontextprotocol.io/) (Model Context Protocol): a small program your AI client starts over **stdio** so it can call tools like `uber_eats_search` and `uber_eats_checkout_preview`.  
It is **not** a Cursor extension / VS Code plugin.

Under the hood: **Uber web JSON APIs** for search, menus, cart (add/remove via `addItemsToDraftOrderV2` / `removeItemsFromDraftOrderV2`), checkout, and orders. **Playwright** is used for **login**, optional **address picker** UI, and **place order fallback** when API submit is unavailable or fails.

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
  - “Reorder helper” that resolves the store + previous items (then add with `uber_eats_add_to_cart` API)
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

### Optional: “I’m hungry” Cursor rule

This repo includes **`.cursor/rules/uber-eats-hungry.mdc`**, which nudges the agent to **call** the Uber Eats tools when you sound hungry or want to order.

- It is **not** part of the **`pip` / wheel install** — `pyproject.toml` only packages the Python server (`server.py`, `api.py`, …). Cloning the repo (or copying the file) is what brings the rule in.
- For Cursor to load it, open **`uber-eats-mcp`** as the **project root**, or copy `uber-eats-hungry.mdc` into your own app’s `.cursor/rules/`.

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
7. `uber_eats_add_to_cart` (API — requires `restaurant_url`)
8. `uber_eats_view_cart`
9. `uber_eats_checkout_preview`
10. Optional: `uber_eats_list_payment_methods`, `uber_eats_set_checkout_payment`, `uber_eats_set_checkout_tip`, `uber_eats_apply_promo`
11. Only after explicit confirmation: `uber_eats_place_order` (API submit first, browser fallback if needed)

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

## Environment (optional)

| Variable | Meaning |
|----------|---------|
| `UBEREATS_WEB_LOCALE` | Country-language path for the **website** (login, address UI, discovery). Default **`cl-en`** (Chile, English), matching `https://www.ubereats.com/cl-en`. Set to **`us-en`**, **`mx-en`**, etc. for other regions, or **empty** to open `https://www.ubereats.com/` only. |
| `UBEREATS_QUIET` | If `1` / `true`, hides the MCP startup banner on stderr. |
| `UBEREATS_LOGIN_TRACE` | Default **on**: each `uber_eats_login` clears then appends JSON lines to **`~/.ubereats-mcp-login-trace.jsonl`** (no cookie values). In another terminal: `tail -f ~/.ubereats-mcp-login-trace.jsonl`. Set to **`0`** to disable. After a successful save, look for **`storage_audit_after_save`**: `session_file_exists`, positive `session_cookie_entries`, `session_includes_sid_cookie`, `config_file_exists`, `config_has_sid_field` — that pattern means disk storage looks healthy. |
| `UBEREATS_DISCOVERY_LOG` | Path to the **browser discovery** JSONL (full API request/response bodies). Default **`~/.ubereats-api-log.jsonl`**. Set to e.g. **`…/uber-eats-mcp/discovery-api-log.jsonl`** so logs stay in this repo; `scripts/run_discovery_interactive.py` sets that automatically. **Cursor MCP:** add the same path under `env` for `uber_eats_discover_apis`. |

**Headed browser (login, address, discovery):** only one flow runs at a time. If the assistant triggers **`uber_eats_login` twice in parallel**, or login runs while another tool opens the same browser, Playwright can error with *page or browser has been closed*—run **one** login and wait for it to finish.

---

## What gets stored locally

Session and preferences are under your home directory (e.g. `~/.ubereats-session.json`, `~/.ubereats-preferences.json`). Do not commit those.

If login fails mid-way, you may see **`~/.ubereats-session.json.prelogin.bak`** (and a matching **`.ubereats-config.json.prelogin.bak`**). A successful retry restores them automatically; if **`*.prelogin.bak`** exists but the main files do not, you can recover manually: copy each `*.prelogin.bak` over the non-`.bak` filename (then remove the `.bak` files if you like).

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

This project is **unofficial** and not affiliated with, endorsed by, or sponsored by Uber Technologies, Inc. in any way. Uber Eats and the Uber logo are trademarks of Uber Technologies, Inc.

- **Reverse-engineered APIs.** This tool interacts with Uber Eats through undocumented internal APIs captured from browser network traffic. These APIs are not publicly supported and may change or break without notice.
- **Terms of Service.** Automating interactions with Uber Eats may violate their Terms of Service. You are solely responsible for ensuring your use complies with Uber's terms. The authors take no responsibility for account suspensions, bans, or any other consequences arising from use of this tool.
- **No warranty.** This software is provided as-is. Orders placed through this tool are your responsibility — always verify your order, delivery address, and payment method before confirming.
- **Use responsibly.** Do not use this tool for bulk ordering, scraping, or any activity that places undue load on Uber's infrastructure.
