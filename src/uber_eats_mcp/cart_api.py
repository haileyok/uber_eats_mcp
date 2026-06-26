"""
Build Uber Eats cart API payloads from getStoreV1 + getMenuItemV1 responses.

Uses POST /_p/api/addItemsToDraftOrderV2 (and related) — no browser.
Payload shapes follow the web client; we merge catalog rows with menu-item detail.
"""

from __future__ import annotations

import re
import uuid
from typing import Any


def normalize_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def find_catalog_item_by_name(sections: list[dict[str, Any]], item_name: str) -> dict[str, Any] | None:
    """Match a menu row from parsed restaurant_menu sections by fuzzy name."""
    target = normalize_name(item_name)
    if not target:
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for sec in sections:
        for it in sec.get("items") or []:
            name = it.get("name") or ""
            n = normalize_name(name)
            if not n:
                continue
            if target == n or target in n or n in target:
                score = 1.0 if target == n else 0.5
                if best is None or score > best[0]:
                    best = (score, it)
    return best[1] if best else None


def _new_client_uuid() -> str:
    return str(uuid.uuid4())


def _deep_find_dicts(obj: Any, predicate) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        if predicate(obj):
            out.append(obj)
        for v in obj.values():
            out.extend(_deep_find_dicts(v, predicate))
    elif isinstance(obj, list):
        for x in obj:
            out.extend(_deep_find_dicts(x, predicate))
    return out


def extract_shopping_cart_line_template(menu_item_v1: dict[str, Any]) -> dict[str, Any] | None:
    """
    Find a dict in getMenuItemV1 response that looks like a cart line (has menuItem uuid fields).
    Prefer objects that also carry customization / sku data.
    """
    if "error" in menu_item_v1:
        return None
    data = menu_item_v1.get("data") or menu_item_v1

    def looks_like_line(d: dict[str, Any]) -> bool:
        has_menu = bool(d.get("menuItemUuid") or d.get("menuItemUUID"))
        has_section = bool(d.get("sectionUuid") or d.get("catalogSectionUuid"))
        return has_menu and has_section

    candidates = _deep_find_dicts(data, looks_like_line)
    if not candidates:
        return None
    # Prefer the richest object (most keys)
    candidates.sort(key=lambda d: len(d), reverse=True)
    return dict(candidates[0])


def merge_catalog_into_line_template(
    template: dict[str, Any],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    """Ensure section/subsection/menuItem uuids match the catalog row."""
    line = dict(template)
    mid = catalog.get("uuid") or line.get("menuItemUuid") or line.get("menuItemUUID")
    if mid:
        line["menuItemUuid"] = mid
    sec = catalog.get("section_uuid") or line.get("sectionUuid") or line.get("catalogSectionUuid")
    if sec:
        line["sectionUuid"] = sec
    sub = catalog.get("subsection_uuid")
    if sub is not None:
        line["subsectionUuid"] = sub
    return line


def apply_quantity(line: dict[str, Any], quantity: int) -> dict[str, Any]:
    """Attach Uber-style itemQuantity when missing (unit count)."""
    if quantity < 1:
        quantity = 1
    out = dict(line)
    if out.get("itemQuantity"):
        return out
    out["itemQuantity"] = {
        "inSellableUnit": {
            "measurementUnit": {"measurementType": "MEASUREMENT_TYPE_UNIT"},
            "value": {
                "coefficient": {"low": quantity, "high": 0, "unsigned": False},
                "exponent": 0,
            },
        }
    }
    return out


def apply_quantity_force(line: dict[str, Any], quantity: int) -> dict[str, Any]:
    """Replace quantity on a line (e.g. updateItemInDraftOrderV2), overwriting itemQuantity."""
    if quantity < 1:
        quantity = 1
    out = dict(line)
    out.pop("itemQuantity", None)
    out["quantity"] = quantity
    return apply_quantity(out, quantity)


def shopping_cart_item_for_create_draft(
    line: dict[str, Any],
    catalog_item: dict[str, Any],
    store_uuid: str,
    quantity: int,
    *,
    price: int | None = None,
) -> dict[str, Any]:
    """
    One element of createDraftOrderV2.shoppingCartItems (web multicart first-add shape).

    Uses plain int quantity and catalog price; omit nested itemQuantity used by addItems.
    """
    menu_uuid = (
        catalog_item.get("uuid")
        or line.get("menuItemUuid")
        or line.get("menuItemUUID")
        or line.get("uuid")
        or ""
    )
    # Web quick-add createDraftOrderV2 uses price: null for catalog items.
    if price is None:
        price = None
    cust = line.get("customizations")
    if not isinstance(cust, dict):
        cust = {}
    title = line.get("title") or catalog_item.get("name") or ""
    image = line.get("imageURL") or line.get("imageUrl") or catalog_item.get("image") or ""
    q = quantity if quantity >= 1 else 1
    return {
        "uuid": menu_uuid,
        "shoppingCartItemUuid": _new_client_uuid(),
        "storeUuid": store_uuid,
        "sectionUuid": catalog_item.get("section_uuid") or line.get("sectionUuid") or "",
        "subsectionUuid": catalog_item.get("subsection_uuid") or line.get("subsectionUuid") or "",
        "price": price,
        "title": title,
        "quantity": q,
        "customizations": cust,
        "imageURL": image,
        "specialInstructions": line.get("specialInstructions") or "",
        "itemId": None,
    }


def build_create_draft_order_multicart_body(
    shopping_cart_items: list[dict[str, Any]],
    *,
    currency_code: str,
    payment_profile_uuid: str = "",
    business_details: dict[str, Any] | None = None,
    is_quick_add: bool = True,
    remove_adapters: bool = True,
) -> dict[str, Any]:
    """Request body for POST /_p/api/createDraftOrderV2 matching the web client's first cart line."""
    body: dict[str, Any] = {
        "isMulticart": True,
        "shoppingCartItems": shopping_cart_items,
        "useCredits": True,
        "extraPaymentProfiles": [],
        "promotionOptions": {
            "autoApplyPromotionUUIDs": [],
            "selectedPromotionInstanceUUIDs": [],
            "skipApplyingPromotion": False,
        },
        "deliveryTime": {"asap": True},
        "deliveryType": "ASAP",
        "currencyCode": (currency_code or "USD").strip() or "USD",
        "interactionType": "door_to_door",
        "checkMultipleDraftOrdersCap": True,
        "actionMeta": {"isQuickAdd": is_quick_add, "numClicks": 1},
        "analyticsRelevantData": {"profileSource": "LAST_SELECTED_PROFILE"},
    }
    if remove_adapters:
        body["removeAdapters"] = True
    if payment_profile_uuid:
        body["paymentProfileUUID"] = payment_profile_uuid
    if business_details:
        body["businessDetails"] = business_details
    return body


def build_add_items_body(
    draft_order_uuid: str,
    store_uuid: str,
    line_items: list[dict[str, Any]],
    *,
    cart_uuid: str = "",
    is_new_cart_abstraction: bool | None = None,
    location_type: str | None = None,
    is_quick_add: bool | None = None,
    num_clicks: int | None = None,
    should_update_draft_order_metadata: bool = False,
) -> dict[str, Any]:
    """
    Request body for POST /_p/api/addItemsToDraftOrderV2.

    The web client sends draftOrderUUID, storeUuid, and a list of line payloads.
    """
    items: list[dict[str, Any]] = []
    for raw in line_items:
        item = dict(raw)
        if not item.get("uuid"):
            # Browser sends uuid = menuItemUuid (catalog SKU), not a random value.
            item["uuid"] = item.get("menuItemUuid") or item.get("menuItemUUID") or _new_client_uuid()
        if not item.get("shoppingCartItemUuid"):
            item["shoppingCartItemUuid"] = _new_client_uuid()
        # Browser always includes storeUuid on each item.
        if not item.get("storeUuid") and store_uuid:
            item["storeUuid"] = store_uuid
        items.append(item)
    body: dict[str, Any] = {
        "items": items,
        "draftOrderUUID": draft_order_uuid,
        # Web uses storeUUID + cartUUID in this mutation; keep storeUuid too for tolerance.
        "storeUUID": store_uuid,
        "storeUuid": store_uuid,
        "shouldUpdateDraftOrderMetadata": bool(should_update_draft_order_metadata),
    }
    if is_quick_add is not None:
        body["actionMeta"] = {"isQuickAdd": bool(is_quick_add)}
        if num_clicks is not None:
            body["actionMeta"]["numClicks"] = int(num_clicks)
    if is_new_cart_abstraction is not None:
        body["isNewCartAbstraction"] = bool(is_new_cart_abstraction)
    if cart_uuid:
        body["cartUUID"] = cart_uuid
    if location_type:
        body["locationType"] = location_type
    return body


def build_remove_items_body_v2(
    *,
    cart_uuid: str,
    draft_order_uuid: str,
    store_uuid: str,
    shopping_cart_item_uuids: list[str],
    location_type: str | None = None,
) -> dict[str, Any]:
    """Request body for POST /_p/api/removeItemsFromDraftOrderV2 (web cart abstraction)."""
    body: dict[str, Any] = {
        "cartUUID": cart_uuid,
        "draftOrderUUID": draft_order_uuid,
        "shoppingCartItemUUIDs": shopping_cart_item_uuids,
        "storeUUID": store_uuid,
    }
    if location_type:
        body["locationType"] = location_type
    return body


def build_remove_items_body(draft_order_uuid: str, shopping_cart_item_uuids: list[str]) -> dict[str, Any]:
    """Request body for POST /_p/api/removeItemsFromDraftOrderV2."""
    return {
        "draftOrderUUID": draft_order_uuid,
        "shoppingCartItemUUIDs": shopping_cart_item_uuids,
    }


def build_update_item_in_draft_order_v2_body(
    draft_order_uuid: str,
    store_uuid: str,
    shopping_cart_line: dict[str, Any],
    *,
    is_new_cart_abstraction: bool = True,
    location_type: str | None = None,
    is_quick_add: bool = True,
    remove_adapters: bool = True,
) -> dict[str, Any]:
    """
    Request body for POST /_p/api/updateItemInDraftOrderV2 (web grocery / multicart flow).

    location_type: e.g. \"GROCERY_STORE\" when the store is grocery; omit for typical restaurants.
    """
    # Browser capture uses key "item" (not "shoppingCartItem") for this endpoint.
    body: dict[str, Any] = {
        "draftOrderUUID": draft_order_uuid,
        "storeUuid": store_uuid,
        "isNewCartAbstraction": is_new_cart_abstraction,
        "item": shopping_cart_line,
        "actionMeta": {"isQuickAdd": is_quick_add, "numClicks": 1},
    }
    if remove_adapters:
        body["removeAdapters"] = True
    if location_type:
        body["locationType"] = location_type
    return body


def draft_order_uuid_for_store(draft_orders_response: dict[str, Any], store_uuid: str) -> str:
    """Return existing draft order uuid for this store, if any."""
    if "error" in draft_orders_response:
        return ""
    for d in draft_orders_response.get("data", {}).get("draftOrders") or []:
        su = d.get("storeUuid") or d.get("restaurantUUID") or ""
        sc = (d.get("shoppingCart") or {}).get("storeUuid") or ""
        if su == store_uuid or sc == store_uuid:
            u = d.get("uuid") or ""
            if u:
                return u
    return ""


def cart_uuid_for_store(draft_orders_response: dict[str, Any], store_uuid: str) -> str:
    """Return cart UUID for the existing draft order for this store, if any."""
    if "error" in draft_orders_response:
        return ""
    for d in draft_orders_response.get("data", {}).get("draftOrders") or []:
        su = d.get("storeUuid") or d.get("restaurantUUID") or ""
        sc_obj = d.get("shoppingCart") or {}
        sc = sc_obj.get("storeUuid") or ""
        if su == store_uuid or sc == store_uuid:
            cart_uuid = sc_obj.get("cartUuid") or sc_obj.get("uuid") or ""
            if cart_uuid:
                return cart_uuid
    return ""


def parse_create_draft_order_uuid(raw: dict[str, Any]) -> str:
    if "error" in raw:
        return ""
    d = raw.get("data") or {}
    draft = d.get("draftOrder") or d.get("draftOrderV2") or {}
    u = draft.get("uuid") or d.get("draftOrderUUID") or d.get("uuid")
    return str(u or "")


def _rich_text_title(obj: Any) -> str:
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        t = obj.get("text")
        if isinstance(t, str):
            return t.strip()
        if isinstance(t, dict):
            inner = t.get("text")
            return inner.strip() if isinstance(inner, str) else ""
        return str(obj.get("title") or "").strip()
    return ""


def catalog_item_from_explicit_uuids(
    *,
    menu_item_uuid: str,
    section_uuid: str,
    subsection_uuid: str = "",
    name_hint: str = "",
    price_cents: int = 0,
    image: str = "",
) -> dict[str, Any]:
    """Minimal catalog row for add-to-cart without loading the full store menu."""
    return {
        "uuid": menu_item_uuid.strip(),
        "section_uuid": section_uuid.strip(),
        "subsection_uuid": (subsection_uuid or "").strip(),
        "name": (name_hint or "").strip(),
        "price_cents": int(price_cents) if price_cents else 0,
        "image": (image or "").strip(),
    }


def find_catalog_item_by_menu_item_uuid(
    menu: dict[str, Any],
    menu_item_uuid: str,
) -> dict[str, Any] | None:
    """
    Find a menu row by catalog item uuid (from parse_store_menu / get_restaurant_menu shape).

    Used to fix wrong section_uuid from search (e.g. section_uuid accidentally set to store_uuid).
    """
    target = (menu_item_uuid or "").strip().lower()
    if not target:
        return None
    for sec in menu.get("sections") or []:
        for it in sec.get("items") or []:
            u = (it.get("uuid") or "").strip().lower()
            if u == target:
                return dict(it)
    return None


def catalog_item_from_search_hit(hit: dict[str, Any]) -> dict[str, Any] | None:
    """
    Map uber_eats_search items[] entry to a catalog_item + store uuid (same APIs as restaurants).

    Returns None if required ids are missing.
    """
    mu = (hit.get("menu_item_uuid") or "").strip()
    sec = (hit.get("section_uuid") or "").strip()
    su = (hit.get("store_uuid") or "").strip()
    if not (mu and su):
        return None
    if su and sec == su:
        sec = ""
    sub = (hit.get("subsection_uuid") or "").strip()
    price = hit.get("price")
    pc = int(price) if isinstance(price, int) else 0
    out = catalog_item_from_explicit_uuids(
        menu_item_uuid=mu,
        section_uuid=sec,
        subsection_uuid=sub,
        name_hint=(hit.get("name") or "").strip(),
        price_cents=pc,
        image=(hit.get("image") or "").strip(),
    )
    out["_store_uuid"] = su
    return out


def title_from_menu_item_detail(menu_item_v1: dict[str, Any]) -> str:
    if "error" in menu_item_v1:
        return ""
    data = menu_item_v1.get("data") or menu_item_v1
    t = data.get("title")
    if isinstance(t, str):
        return t.strip()
    if isinstance(t, dict):
        return _rich_text_title(t)
    return ""


def price_cents_from_menu_item_detail(menu_item_v1: dict[str, Any]) -> int:
    """Best-effort catalog price from getMenuItemV1 (often same scale as getStore catalog)."""
    if "error" in menu_item_v1:
        return 0
    data = menu_item_v1.get("data") or menu_item_v1
    p = data.get("price")
    if isinstance(p, int) and p > 0:
        return p
    if isinstance(p, dict):
        low = p.get("low")
        if isinstance(low, int):
            return low
    for key in ("purchaseInfo", "itemPurchaseInfo", "pricingInfo"):
        block = data.get(key)
        if isinstance(block, dict):
            for _subk, subv in block.items():
                if isinstance(subv, int) and subv > 100:
                    return subv
    return 0


def summarize_customizations_for_options(menu_item_v1: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Customization groups for MCP get_item_options (getMenuItemV1).

    When the API returns data.customizationsList, each group includes
    minPermitted / maxPermitted so callers can see required (pick-one) modifiers.
    Falls back to a shallow tree walk if that list is missing.
    """
    if "error" in menu_item_v1:
        return []
    data = menu_item_v1.get("data") or menu_item_v1
    raw_list = data.get("customizationsList")
    if isinstance(raw_list, list) and raw_list:
        out: list[dict[str, Any]] = []
        for g in raw_list[:30]:
            if not isinstance(g, dict):
                continue
            title = _rich_text_title(g.get("title")) or _rich_text_title(g.get("name"))
            opts_in = g.get("options") or g.get("customizationOptions") or []
            if not isinstance(opts_in, list):
                opts_in = []
            min_p = int(g.get("minPermitted") or 0)
            max_p = int(g.get("maxPermitted") or 0)
            option_rows: list[dict[str, Any]] = []
            for o in opts_in[:40]:
                if not isinstance(o, dict):
                    option_rows.append({"title": str(o), "uuid": "", "default_quantity": 0})
                    continue
                ot = o.get("title")
                if isinstance(ot, dict):
                    ot = ot.get("text", "")
                option_rows.append({
                    "title": str(ot or ""),
                    "uuid": str(o.get("uuid") or ""),
                    "default_quantity": int(o.get("defaultQuantity") or 0),
                })
            out.append({
                "group": title,
                "group_uuid": str(g.get("uuid") or ""),
                "required": min_p >= 1,
                "min_permitted": min_p,
                "max_permitted": max_p,
                "pick_one": min_p == 1 and max_p == 1,
                "options": option_rows,
            })
        return out

    groups: list[dict[str, Any]] = []

    def walk(obj: Any, depth: int = 0) -> None:
        if depth > 20:
            return
        if isinstance(obj, dict):
            title = _rich_text_title(obj.get("title")) or _rich_text_title(obj.get("name"))
            opts = obj.get("options") or obj.get("customizationOptions")
            if title and isinstance(opts, list) and opts:
                option_rows = []
                for o in opts[:40]:
                    if isinstance(o, dict):
                        ot = o.get("title")
                        if isinstance(ot, dict):
                            ot = ot.get("text", "")
                        option_rows.append({
                            "title": str(ot or ""),
                            "uuid": str(o.get("uuid") or ""),
                            "default_quantity": int(o.get("defaultQuantity") or 0),
                        })
                    else:
                        option_rows.append({"title": str(o), "uuid": "", "default_quantity": 0})
                min_p = int(obj.get("minPermitted") or 0)
                max_p = int(obj.get("maxPermitted") or 0)
                groups.append({
                    "group": title,
                    "group_uuid": str(obj.get("uuid") or ""),
                    "required": min_p >= 1,
                    "min_permitted": min_p,
                    "max_permitted": max_p,
                    "pick_one": min_p == 1 and max_p == 1,
                    "options": option_rows,
                })
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for x in obj:
                walk(x, depth + 1)

    walk(data)
    return groups[:30]


def build_customizations_from_selections(
    menu_item_v1: dict[str, Any],
    selections: dict[str, Any],
) -> dict[str, Any]:
    """Build the ``customizations`` dict for a cart line from agent-friendly selections.

    *selections* maps group UUID → option UUID (single) or list of option UUIDs (multi-select).
    The function walks ``data.customizationsList`` from the getMenuItemV1 response to find
    the matching groups and options, then builds the nested dict structure Uber expects.

    The cart body customizations format is:
    ``{ group_uuid: [ { "uuid": option_uuid, "quantity": N, "price": P } ] }``

    If a group is required (minPermitted >= 1) but not in selections, the default options
    from the API response are kept. If selections is empty, returns {} (use API defaults).
    """
    if not selections:
        return {}

    if "error" in menu_item_v1:
        return {}

    data = menu_item_v1.get("data") or menu_item_v1
    raw_list = data.get("customizationsList")
    if not isinstance(raw_list, list):
        return {}

    out: dict[str, Any] = {}

    for g in raw_list:
        if not isinstance(g, dict):
            continue
        group_uuid = str(g.get("uuid") or "")
        if not group_uuid:
            continue

        opts_in = g.get("options") or g.get("customizationOptions") or []
        if not isinstance(opts_in, list):
            opts_in = []

        # Build a lookup of option UUID → option data.
        opt_lookup: dict[str, dict[str, Any]] = {}
        for o in opts_in:
            if isinstance(o, dict):
                opt_uuid = str(o.get("uuid") or "")
                if opt_uuid:
                    opt_lookup[opt_uuid] = o

        # Determine which option UUIDs the user selected for this group.
        selected_raw = selections.get(group_uuid)
        if selected_raw is None:
            # Not in selections — check if it's required; keep defaults if so.
            min_p = int(g.get("minPermitted") or 0)
            if min_p >= 1:
                # Keep default-selected options from the API.
                default_opts: list[dict[str, Any]] = []
                for opt_uuid, o in opt_lookup.items():
                    if int(o.get("defaultQuantity") or 0) > 0:
                        default_opts.append({
                            "uuid": opt_uuid,
                            "quantity": int(o.get("defaultQuantity") or 1),
                            "price": int(o.get("price") or 0),
                        })
                if default_opts:
                    out[group_uuid] = default_opts
            continue

        # Normalize to a list of option UUIDs.
        if isinstance(selected_raw, str):
            selected_uuids = [selected_raw]
        elif isinstance(selected_raw, list):
            selected_uuids = [str(s) for s in selected_raw]
        else:
            continue

        # Build the list of option objects with uuid + quantity + price.
        selected: list[dict[str, Any]] = []
        for opt_uuid in selected_uuids:
            o = opt_lookup.get(opt_uuid)
            if o:
                selected.append({
                    "uuid": opt_uuid,
                    "quantity": 1,
                    "price": int(o.get("price") or 0),
                })

        if selected:
            out[group_uuid] = selected

    return out
