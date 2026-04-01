"""Unit tests for cart_api helpers (no network, no Uber session)."""

from __future__ import annotations

import uber_eats_mcp.cart_api as cart_api


def test_normalize_name() -> None:
    assert cart_api.normalize_name("  Foo   Bar  ") == "foo bar"


def test_find_catalog_item_by_name_exact() -> None:
    sections = [
        {
            "section": "Burgers",
            "items": [
                {"uuid": "a", "name": "Big MIT", "section_uuid": "s1", "subsection_uuid": "sub1"},
            ],
        }
    ]
    found = cart_api.find_catalog_item_by_name(sections, "Big MIT")
    assert found is not None
    assert found["uuid"] == "a"


def test_find_catalog_item_by_name_fuzzy() -> None:
    sections = [
        {"section": "X", "items": [{"uuid": "x", "name": "Texas Ranger ** Doble", "section_uuid": "s", "subsection_uuid": ""}]}
    ]
    found = cart_api.find_catalog_item_by_name(sections, "texas ranger")
    assert found is not None
    assert found["uuid"] == "x"


def test_draft_order_uuid_for_store() -> None:
    raw = {
        "data": {
            "draftOrders": [
                {"uuid": "draft-1", "storeUuid": "store-1"},
            ]
        }
    }
    assert cart_api.draft_order_uuid_for_store(raw, "store-1") == "draft-1"
    assert cart_api.draft_order_uuid_for_store(raw, "other") == ""


def test_build_add_items_body_sets_uuids() -> None:
    body = cart_api.build_add_items_body(
        "draft-1",
        "store-1",
        [{"menuItemUuid": "m1", "sectionUuid": "sec1"}],
    )
    assert body["draftOrderUUID"] == "draft-1"
    assert body["storeUuid"] == "store-1"
    assert body["storeUUID"] == "store-1"
    assert len(body["items"]) == 1
    assert body["items"][0]["uuid"]


def test_build_remove_items_body_v2() -> None:
    b = cart_api.build_remove_items_body_v2(
        cart_uuid="c1",
        draft_order_uuid="d1",
        store_uuid="s1",
        shopping_cart_item_uuids=["u1"],
        location_type="GROCERY_STORE",
    )
    assert b["cartUUID"] == "c1"
    assert b["draftOrderUUID"] == "d1"
    assert b["storeUUID"] == "s1"
    assert b["shoppingCartItemUUIDs"] == ["u1"]
    assert b["locationType"] == "GROCERY_STORE"


def test_build_remove_items_body() -> None:
    b = cart_api.build_remove_items_body("d1", ["u1", "u2"])
    assert b["draftOrderUUID"] == "d1"
    assert b["shoppingCartItemUUIDs"] == ["u1", "u2"]


def test_parse_create_draft_order_uuid() -> None:
    assert cart_api.parse_create_draft_order_uuid({"error": "x"}) == ""
    assert cart_api.parse_create_draft_order_uuid({"data": {"draftOrder": {"uuid": "new-d"}}}) == "new-d"


def test_catalog_item_from_search_hit() -> None:
    hit = {
        "name": "Red Bull",
        "menu_item_uuid": "m1",
        "section_uuid": "s1",
        "subsection_uuid": "sub1",
        "store_uuid": "st1",
        "price": 1990,
        "image": "https://x/i.jpg",
    }
    cat = cart_api.catalog_item_from_search_hit(hit)
    assert cat is not None
    assert cat["uuid"] == "m1"
    assert cat["section_uuid"] == "s1"
    assert cat["subsection_uuid"] == "sub1"
    assert cat["_store_uuid"] == "st1"
    assert cat["price_cents"] == 1990


def test_catalog_item_from_search_hit_incomplete() -> None:
    assert cart_api.catalog_item_from_search_hit({"menu_item_uuid": "m"}) is None


def test_catalog_item_from_search_hit_no_section_ok() -> None:
    hit = {
        "name": "Red Bull",
        "menu_item_uuid": "m1",
        "store_uuid": "st1",
        "price": 1990,
    }
    cat = cart_api.catalog_item_from_search_hit(hit)
    assert cat is not None
    assert cat["section_uuid"] == ""
    assert cat["_store_uuid"] == "st1"


def test_catalog_item_from_search_hit_clears_section_when_equals_store() -> None:
    hit = {
        "menu_item_uuid": "m1",
        "section_uuid": "st1",
        "store_uuid": "st1",
    }
    cat = cart_api.catalog_item_from_search_hit(hit)
    assert cat is not None
    assert cat["section_uuid"] == ""


def test_find_catalog_item_by_menu_item_uuid() -> None:
    menu = {
        "sections": [
            {
                "items": [
                    {
                        "uuid": "item-a",
                        "name": "A",
                        "section_uuid": "real-sec",
                        "subsection_uuid": "sub-z",
                    }
                ]
            }
        ]
    }
    found = cart_api.find_catalog_item_by_menu_item_uuid(menu, "item-a")
    assert found is not None
    assert found["section_uuid"] == "real-sec"
    assert cart_api.find_catalog_item_by_menu_item_uuid(menu, "missing") is None


def test_title_from_menu_item_detail() -> None:
    raw = {"data": {"title": {"text": "Combo"}}}
    assert cart_api.title_from_menu_item_detail(raw) == "Combo"


def test_shopping_cart_item_for_create_draft() -> None:
    line = {"title": "Burger", "customizations": {"g+0": []}}
    cat = {
        "uuid": "menu-uuid",
        "name": "Burger",
        "price_cents": 990,
        "section_uuid": "sec",
        "subsection_uuid": "sub",
        "image": "https://x/img.jpg",
    }
    item = cart_api.shopping_cart_item_for_create_draft(line, cat, "store-1", 2, price=None)
    assert item["uuid"] == "menu-uuid"
    assert item["storeUuid"] == "store-1"
    assert item["sectionUuid"] == "sec"
    assert item["subsectionUuid"] == "sub"
    assert item["price"] is None
    assert item["quantity"] == 2
    assert item["shoppingCartItemUuid"]
    assert item["itemId"] is None


def test_build_create_draft_order_multicart_body() -> None:
    body = cart_api.build_create_draft_order_multicart_body(
        [{"uuid": "m1", "shoppingCartItemUuid": "c1", "storeUuid": "s1", "quantity": 1}],
        currency_code="CLP",
        payment_profile_uuid="pay-1",
        business_details={"profileType": "Personal", "profileUUID": "p1"},
    )
    assert body["isMulticart"] is True
    assert len(body["shoppingCartItems"]) == 1
    assert body["currencyCode"] == "CLP"
    assert body["paymentProfileUUID"] == "pay-1"
    assert body["businessDetails"]["profileUUID"] == "p1"
    assert body["actionMeta"]["isQuickAdd"] is True
    assert body["removeAdapters"] is True


def test_apply_quantity() -> None:
    line = {"menuItemUuid": "m"}
    out = cart_api.apply_quantity(line, 3)
    assert out["itemQuantity"]["inSellableUnit"]["value"]["coefficient"]["low"] == 3


def test_apply_quantity_force_replaces_existing() -> None:
    line = {
        "menuItemUuid": "m",
        "quantity": 1,
        "itemQuantity": {
            "inSellableUnit": {
                "value": {"coefficient": {"low": 1, "high": 0, "unsigned": False}}
            }
        },
    }
    out = cart_api.apply_quantity_force(line, 4)
    assert out["quantity"] == 4
    assert out["itemQuantity"]["inSellableUnit"]["value"]["coefficient"]["low"] == 4


def test_build_update_item_in_draft_order_v2_body() -> None:
    body = cart_api.build_update_item_in_draft_order_v2_body(
        "draft-1",
        "store-1",
        {"shoppingCartItemUuid": "sci-1", "quantity": 2},
        location_type="GROCERY_STORE",
    )
    assert body["draftOrderUUID"] == "draft-1"
    assert body["storeUuid"] == "store-1"
    assert body["item"]["shoppingCartItemUuid"] == "sci-1"
    assert body["isNewCartAbstraction"] is True
    assert body["locationType"] == "GROCERY_STORE"
    assert body["removeAdapters"] is True


def test_summarize_customizations_mandatory_from_customizations_list() -> None:
    raw = {
        "data": {
            "customizationsList": [
                {
                    "title": "Elige Tu Salsa",
                    "uuid": "3e191536-1338-5a2d-b4b8-a15e507dcfaa",
                    "minPermitted": 1,
                    "maxPermitted": 1,
                    "options": [
                        {"title": "Chik Fil Ey", "uuid": "b2131c3f-4aff-5fbb-ba3a-c46a8510ece7", "defaultQuantity": 0},
                    ],
                },
                {
                    "title": "Extras",
                    "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "minPermitted": 0,
                    "maxPermitted": 5,
                    "options": [{"title": "Bacon", "uuid": "x", "defaultQuantity": 0}],
                },
            ]
        }
    }
    groups = cart_api.summarize_customizations_for_options(raw)
    assert len(groups) == 2
    g0, g1 = groups[0], groups[1]
    assert g0["group"] == "Elige Tu Salsa"
    assert g0["required"] is True
    assert g0["pick_one"] is True
    assert g0["min_permitted"] == 1
    assert g0["max_permitted"] == 1
    assert g0["options"][0]["title"] == "Chik Fil Ey"
    assert g1["required"] is False
    assert g1["pick_one"] is False
    assert g1["min_permitted"] == 0
