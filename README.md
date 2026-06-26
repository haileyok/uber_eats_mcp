# Claude, I'm hungry. 🍜

Order food from Uber Eats through natural language in **Cursor**, **Claude Code**, or any MCP-compatible client — with an assistant that can browse menus, prep checkout, and (when you say so) place the order.

This is a **fork** of [`matiasconcha11/uber_eats_mcp`](https://github.com/matiasconcha11/uber_eats_mcp) that routes all API calls through a **persistent Chrome instance via Chrome DevTools Protocol (CDP)**, eliminating session expiry problems and avoiding any visible browser window during normal operation.

This project is an [**MCP server**](https://modelcontextprotocol.io/) (Model Context Protocol): a small program your AI client starts over **stdio** so it can call tools like `uber_eats_search` and `uber_eats_checkout_preview`.

---

## What changed from the original

The original server authenticated by extracting cookies from a Playwright session and replaying them via `httpx`. This works but has two problems:

1. **Session expiry** — `httpx` doesn't process `set-cookie` response headers, so refreshed cookies are discarded.
2. **Anti-bot 401s** — Uber blocks some mutations (notably `createDraftOrderV2`) when sent from `httpx`, even with valid cookies.

**This fork** routes all API calls through Chrome's `page.request.post()`. Chrome maintains the cookie jar natively — `set-cookie` responses are processed automatically, CSRF tokens stay current, and anti-bot detection is satisfied because requests come from a real browser context. A keep-alive mechanism periodically navigates to `ubereats.com` to refresh sliding session cookies.

---

## Quick start

### Prerequisites

- **Python ≥ 3.12**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)**
- **Chrome/Chromium** installed (Playwright's bundled Chromium works too)
- An Uber Eats account with a saved payment method

### Setup

```bash
git clone <your-fork-url>
cd uber_eats_mcp
uv sync
uv run playwright install chromium
```

### Start Chrome with CDP

Start a persistent Chrome instance with remote debugging enabled:

**macOS:**
```bash
./scripts/start_chrome.sh
# or directly:
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir=~/.ubereats-chrome-profile \
  --headless=new --disable-gpu --no-sandbox --no-first-run
```

**Linux:**
```bash
google-chrome --remote-debugging-port=9222 \
  --user-data-dir=~/.ubereats-chrome-profile \
  --headless=new --disable-gpu --no-sandbox --no-first-run
```

> **Note:** Chrome 136+ ignores `--remote-debugging-port` for the default profile. You must use a dedicated `--user-data-dir` (the script handles this automatically).

For **login** (which requires user interaction with 2FA/captcha), start Chrome **headed** instead:
```bash
./scripts/start_chrome.sh 9222 --headed
```

### Configure your MCP client

Use the `uber-eats-mcp` entry point with `UBEREATS_CDP_PORT` and `UBEREATS_WEB_LOCALE` env vars:

```json
{
  "mcpServers": {
    "ubereats": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/uber_eats_mcp", "uber-eats-mcp"],
      "env": {
        "UBEREATS_CDP_PORT": "9222",
        "UBEREATS_WEB_LOCALE": "us-en"
      }
    }
  }
}
```

---

## Initial login

Login is the one step that requires human interaction — Uber's flow includes 2FA, SMS codes, and captcha challenges that cannot be automated. There are several paths:

### Option A: Cookie import from your laptop browser (recommended for headless)

This is the cleanest path for fully headless setups. Export cookies from your laptop, convert them, and drop them on the server — no browser interaction needed.

1. **Install the [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) extension** in your laptop's Chrome.
2. **Log into Uber Eats** in your laptop browser (www.ubereats.com).
3. **Click the extension icon** and export as **JSON** (not Netscape). You'll get a file like `ubereats.com_cookies.json`.
4. **Copy the file to the server** running Chrome + the MCP server:
   ```bash
   scp ubereats.com_cookies.json hailey.coder:~/personal/uber_eats_mcp-cdp/
   ```
5. **Run the converter** on the server:
   ```bash
   cd ~/personal/uber_eats_mcp-cdp
   python scripts/convert_cookies.py ubereats.com_cookies.json
   ```
   This writes `~/.ubereats-session.json` in Playwright's `storage_state` format.
6. **Restart the MCP server** (or call `uber_eats_login`). On startup, the server auto-loads these cookies into Chrome's cookie jar via CDP.
7. **Verify** with `uber_eats_whoami` — should show your account info.

The keep-alive mechanism refreshes sliding session cookies every 4 hours, so you won't need to re-export frequently. When the session eventually expires, just repeat steps 3-6.

### Option B: CDP headed login

1. Start Chrome **headed** (not `--headless=new`): `./scripts/start_chrome.sh 9222 --headed`
2. Run `uber_eats_login` — the MCP server navigates the CDP-connected Chrome to the Uber Eats login page.
3. Log in manually in the Chrome window (handle 2FA/captcha as needed).
4. The session persists in Chrome's cookie jar. You can restart Chrome with `--headless=new` for ongoing operation (using the same `--user-data-dir`), or keep it headed.

### Option C: Standalone headed browser (fallback, no CDP)

If `UBEREATS_CDP_PORT` is not set, the server falls back to the original behavior: launching a standalone headed Chromium for login via Playwright. Session is saved to `~/.ubereats-session.json` and API calls use `httpx`. This path has the session-expiry and anti-bot limitations described above.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `UBEREATS_CDP_PORT` | (unset) | CDP port to connect to. If unset, falls back to standalone browser launch + `httpx`. |
| `UBEREATS_WEB_LOCALE` | `us-en` | Country-language path for Uber Eats web app. |
| `UBEREATS_KEEPALIVE_INTERVAL_HOURS` | `4` | How often to auto-ping `ubereats.com` to refresh session cookies. Set to `0` to disable. |
| `UBEREATS_PLACE_ORDER_BROWSER_ONLY` | `0` | If `1`, forces the browser-click path for placing orders (emergency override). |
| `UBEREATS_LOG_API_CALLS` | `1` | Set to `0` to disable API call logging to `~/.ubereats-mcp-api-log.jsonl`. |

---

## Keep-alive

The MCP server runs an internal asyncio task that pings `ubereats.com` every `UBEREATS_KEEPALIVE_INTERVAL_HOURS` (default 4h). This is the primary session-refresh mechanism — it runs independently of any MCP client.

A secondary `uber_eats_keepalive` tool is exposed so a scheduler (e.g. Victrola) can trigger an extra ping:

```
Call the ubereats.keepalive tool to refresh the Uber Eats session.
```

---

## Typical flow

1. `uber_eats_login` (CDP or cookie import)
2. `uber_eats_get_preferences` (optional personalization)
3. `uber_eats_get_address` (confirm delivery address)
4. `uber_eats_search` or `uber_eats_nearby_restaurants`
5. `uber_eats_restaurant_menu` → decide items
6. If needed: `uber_eats_menu_item_detail` / `uber_eats_get_item_options`
7. `uber_eats_add_to_cart` (API via Chrome)
8. `uber_eats_view_cart`
9. `uber_eats_checkout_preview`
10. Optional: `uber_eats_list_payment_methods`, `uber_eats_set_checkout_payment`, `uber_eats_set_checkout_tip`, `uber_eats_apply_promo`
11. Only after explicit confirmation: `uber_eats_place_order`

---

## Run without MCP (debug)

```bash
uv run uber-eats-mcp
```

---

## What gets stored locally

Session and preferences are under your home directory (e.g. `~/.ubereats-session.json`, `~/.ubereats-config.json`, `~/.ubereats-preferences.json`). Do not commit those.

When CDP is enabled, the **source of truth for auth is Chrome's cookie jar**, not the session file. The session file may still be written for backwards compatibility, but deleting it while Chrome is running with valid cookies will not cause auth failures.

---

## Legal

This project is **unofficial** and not affiliated with, endorsed by, or sponsored by Uber Technologies, Inc. in any way. Uber Eats and the Uber logo are trademarks of Uber Technologies, Inc.

- **Reverse-engineered APIs.** This tool interacts with Uber Eats through undocumented internal APIs captured from browser network traffic. These APIs are not publicly supported and may change or break without notice.
- **Terms of Service.** Automating interactions with Uber Eats may violate their Terms of Service. You are solely responsible for ensuring your use complies with Uber's terms. The authors take no responsibility for account suspensions, bans, or any other consequences arising from use of this tool.
- **No warranty.** This software is provided as-is. Orders placed through this tool are your responsibility — always verify your order, delivery address, and payment method before confirming.
- **Use responsibly.** Do not use this tool for bulk ordering, scraping, or any activity that places undue load on Uber's infrastructure.
