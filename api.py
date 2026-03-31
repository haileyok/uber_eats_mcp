"""
Direct HTTP client for the Uber Eats internal API.

Uses session cookies captured during browser login. No browser needed
for API calls — just httpx with the right cookies and headers.

Endpoint reference (reverse-engineered):
  POST /_p/api/getFeedV1        — home feed + search
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
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

SESSION_PATH = Path.home() / ".ubereats-session.json"
CONFIG_PATH = Path.home() / ".ubereats-config.json"
BASE_URL = "https://www.ubereats.com"
PAYMENTS_BASE_URL = "https://payments.ubereats.com"
# Web client key from network captures; override with UBEREATS_PAYMENTS_API_KEY if needed.
DEFAULT_PAYMENTS_API_KEY = "production_u2bkf0z5pn0e552g"

DEFAULT_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "x-csrf-token": "x",
    "origin": BASE_URL,
    "referer": f"{BASE_URL}/",
}


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


async def _post(path: str, body: dict | None = None) -> dict[str, Any]:
    """Make an authenticated POST to the Uber Eats API."""
    cookies = _build_cookies()
    if not cookies:
        return {"error": "No session found. Use uber_eats_login first."}

    async with httpx.AsyncClient(
        base_url=BASE_URL,
        headers=DEFAULT_HEADERS,
        cookies=cookies,
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        resp = await client.post(path, json=body or {})

    if resp.status_code == 403:
        return {"error": "Session expired or invalid. Use uber_eats_login to re-authenticate."}
    if resp.status_code != 200:
        return {"error": f"API returned {resp.status_code}: {resp.text[:200]}"}

    try:
        return resp.json()
    except Exception:
        return {"error": f"Failed to parse response: {resp.text[:200]}"}


async def _post_absolute_url(url: str, body: dict | None = None) -> dict[str, Any]:
    """POST to an absolute URL (e.g. payments.ubereats.com) with the same session cookies."""
    cookies = _build_cookies()
    if not cookies:
        return {"error": "No session found. Use uber_eats_login first."}

    headers = {
        **DEFAULT_HEADERS,
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/",
    }
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.post(url, json=body or {}, headers=headers, cookies=cookies)

    if resp.status_code == 403:
        return {"error": "Session expired or invalid. Use uber_eats_login to re-authenticate."}
    if resp.status_code != 200:
        return {"error": f"API returned {resp.status_code}: {resp.text[:200]}"}

    try:
        return resp.json()
    except Exception:
        return {"error": f"Failed to parse response: {resp.text[:200]}"}


# ── Feed / Search ────────────────────────────────────────────────────────────

async def get_feed(
    query: str = "",
    offset: int = 0,
    page_size: int = 80,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "pageInfo": {"offset": offset, "pageSize": page_size},
    }
    if query:
        body["userQuery"] = query
    return await _post("/_p/api/getFeedV1", body)


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
                    "section_uuid": section_uuid_key,
                    "subsection_uuid": subsection_uuid,
                }
                if cat_item.get("itemPromotion"):
                    promo = cat_item["itemPromotion"]
                    item_data["original_price"] = promo.get("originalPrice", "")
                items.append(item_data)

            if items:
                sections.append({
                    "section": section_title,
                    "items": items,
                })

    return {
        "restaurant": title,
        "uuid": uuid,
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
        title = store.get("title", "")
        if isinstance(title, dict):
            title = title.get("text", "")
        out.append({
            "uuid": o.get("uuid") or base.get("uuid", ""),
            "restaurant": title or "Unknown",
            "status": base.get("currentState", "") or o.get("state", ""),
            "store_uuid": store.get("uuid") or store.get("storeUuid") or "",
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


# ── Locale + _p/api helpers (discovery uses ?localeCode=cl-en) ───────────────


def _locale_query() -> str:
    """Query suffix for Uber web API calls (e.g. cl-en, en-us)."""
    try:
        import preferences

        loc = preferences.load_preferences().get("locale_code")
        if isinstance(loc, str) and loc.strip():
            return f"?localeCode={loc.strip()}"
    except Exception:
        pass
    return "?localeCode=cl-en"


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
    dining_mode: str | None = None,
) -> dict[str, Any]:
    """Single menu item detail + customization options (captured from discovery)."""
    body: dict[str, Any] = {
        "itemRequestType": item_request_type,
        "storeUuid": store_uuid,
        "sectionUuid": section_uuid,
        "subsectionUuid": subsection_uuid,
        "menuItemUuid": menu_item_uuid,
        "diningMode": dining_mode,
    }
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


async def get_pre_checkout_actions(
    body: dict[str, Any],
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    locale_code: str = "en",
) -> dict[str, Any]:
    """Payments API — requires checkoutSessionUUID and plan payload from checkout flow."""
    loc = _load_location()
    lat = latitude if latitude is not None else (loc or {}).get("latitude")
    lng = longitude if longitude is not None else (loc or {}).get("longitude")
    if lat is None or lng is None:
        return {"error": "Missing coordinates; set delivery location first."}

    key = os.environ.get("UBEREATS_PAYMENTS_API_KEY", DEFAULT_PAYMENTS_API_KEY)
    ctx = quote(json.dumps({"latitude": lat, "longitude": lng}))
    url = f"{PAYMENTS_BASE_URL}/api/getPreCheckoutActions?key={key}&ctx={ctx}&localeCode={locale_code}"
    return await _post_absolute_url(url, body)


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
        title = it.get("title", "")
        qty = it.get("quantity", 1)
        price_raw = it.get("price")
        price_txt = ""
        if isinstance(price_raw, int):
            if price_raw >= 100_000:
                price_txt = _format_clp_e5(price_raw)
            elif price_raw > 0:
                price_txt = str(price_raw)
        items_out.append({
            "title": title,
            "quantity": qty,
            "price": price_txt,
            "shopping_cart_item_uuid": it.get("shoppingCartItemUUID") or it.get("shoppingCartItemUuid", ""),
        })

    return {
        "restaurant_uuid": store_uuid,
        "draft_order_uuid": draft_uuid,
        "items": items_out,
    }


def parse_checkout_payloads(raw: dict[str, Any]) -> dict[str, Any]:
    """Structured checkout summary from getCheckoutPresentationV1."""
    if "error" in raw:
        return raw

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
        price_plain = _rich_text_to_plain(ci.get("originalPrice"))
        cart_items.append({
            "name": name,
            "quantity": q,
            "line_price": price_plain,
        })

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

    return {
        "subtotal": sub_fmt or _format_clp_e5(sub_val.get("amountE5")),
        "total": tot_fmt or _format_clp_e5(tot_val.get("amountE5")),
        "currency": sub_val.get("currencyCode") or tot_val.get("currencyCode", ""),
        "fare_breakdown": charges,
        "cart_items": cart_items,
        "tip_options": tip_options,
        "delivery_address_title": addr_title,
        "delivery_address": addr_sub or addr_title,
        "payment_methods": payment_methods,
    }
