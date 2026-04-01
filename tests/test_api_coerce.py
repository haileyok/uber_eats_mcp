"""api._coerce_uber_json_body behavior."""

import uber_eats_mcp.api as api


def test_coerce_failure_adds_error_key() -> None:
    raw = {
        "status": "failure",
        "data": {"message": "UNKNOWN_ERROR", "code": "401"},
    }
    out = api._coerce_uber_json_body(raw)
    assert "error" in out
    assert "401" in out["error"]


def test_coerce_success_unchanged() -> None:
    raw = {"status": "success", "data": {"draftOrder": {"uuid": "u1"}}}
    out = api._coerce_uber_json_body(raw)
    assert "error" not in out


def test_build_pre_checkout_actions_body() -> None:
    b = api.build_pre_checkout_actions_body(
        checkout_session_uuid="965a42c0-2e75-4763-b01b-7297251909f0",
        payment_profile_uuid="cd654f9d-9f51-5be7-ba1e-4623d9ee981b",
        order_total_e5=523900000,
        currency_code="CLP",
        use_credits=True,
        country_iso2="CL",
        region_id=148,
    )
    assert b["checkoutSessionUUID"].startswith("965a42c0")
    assert b["estimatedPaymentPlan"]["defaultPaymentProfile"]["currencyAmount"]["amountE5"] == 523900000
    assert b["orderContext"]["base"]["businessLocation"]["location"]["countryISO2"] == "CL"


def test_build_checkout_orders_request() -> None:
    draft = {
        "uuid": "6f0dc742-c9c3-4bb2-9114-7f05e3504998",
        "storeUuid": "9198a3e7-1228-5dc3-b9f8-d0c862e95434",
        "shoppingCart": {"storeUuid": "9198a3e7-1228-5dc3-b9f8-d0c862e95434"},
        "paymentProfileUUID": "cd654f9d-9f51-5be7-ba1e-4623d9ee981b",
        "deliveryAddress": {
            "address": {
                "addressComponents": {"city": "Providencia"},
            }
        },
        "orderMetadata": {"uberMerchantType": {"type": "MERCHANT_TYPE_RESTAURANT"}},
    }
    body = api.build_checkout_orders_request(
        draft,
        order_total_e5=523900000,
        currency_code="CLP",
    )
    assert body["draftOrderUUID"] == "6f0dc742-c9c3-4bb2-9114-7f05e3504998"
    ep = body["extraParams"]
    assert ep["storeUuid"] == "9198a3e7-1228-5dc3-b9f8-d0c862e95434"
    assert ep["orderTotalFare"] == 523900000
    assert ep["verticalLabel"] == "RESTAURANT"


def test_extract_checkout_session_uuid_nested() -> None:
    raw = {"data": {"nested": {"checkoutSessionUUID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}}}
    assert api.extract_checkout_session_uuid(raw) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_parse_checkout_payloads_includes_discounted_line_price() -> None:
    raw = {
        "status": "success",
        "data": {
            "checkoutPayloads": {
                "cartItems": {
                    "cartItems": [
                        {
                            "title": "Combo",
                            "quantity": {"value": {"coefficient": {"low": 1}}},
                            "originalPrice": {
                                "richTextElements": [
                                    {"type": "text", "text": {"text": "CLP\xa09,900"}},
                                ],
                            },
                            "discountedPrice": {
                                "richTextElements": [
                                    {"type": "text", "text": {"text": "CLP\xa04,990"}},
                                ],
                            },
                        }
                    ]
                },
                "subtotal": {
                    "subtotal": {
                        "value": {"amountE5": 499000000, "currencyCode": "CLP"},
                        "formattedValue": "CLP 4,990",
                    }
                },
                "total": {
                    "total": {
                        "value": {"amountE5": 600000000, "currencyCode": "CLP"},
                        "formattedValue": "CLP 6,000",
                    }
                },
            }
        },
    }
    out = api.parse_checkout_payloads(raw)
    item = out["cart_items"][0]
    assert item["line_price"] == "CLP 4,990"
    assert item["regular_price"] == "CLP 9,900"
    assert item["discounted_price"] == "CLP 4,990"
    assert item["on_offer"] is True
    assert "Was" in item["offer_summary"] and "now" in item["offer_summary"]


def test_redact_url_query_masks_payments_key() -> None:
    u = "https://payments.ubereats.com/api/foo?key=secret&ctx=abc&localeCode=en"
    out = api._redact_url_query(u)
    assert "secret" not in out
    assert "key=%2A%2A%2A" in out or "key=***" in out
    assert "localeCode=en" in out


def test_parse_active_orders_legacy_shape() -> None:
    raw = {
        "status": "success",
        "data": {
            "orders": [
                {
                    "uuid": "u1",
                    "baseEaterOrder": {"uuid": "u1", "currentState": "COMPLETED"},
                    "storeInfo": {"title": "Legacy Cafe", "uuid": "store-legacy"},
                }
            ]
        },
    }
    out = api.parse_active_orders(raw)
    assert len(out) == 1
    assert out[0]["uuid"] == "u1"
    assert out[0]["restaurant"] == "Legacy Cafe"
    assert out[0]["status"] == "COMPLETED"
    assert out[0]["store_uuid"] == "store-legacy"


def test_parse_active_orders_card_ui_shape() -> None:
    """Web getActiveOrdersV1 often omits baseEaterOrder; use orderInfo + activeOrderStatus."""
    raw = {
        "status": "success",
        "data": {
            "orders": [
                {
                    "uuid": "6f0dc742-c9c3-4bb2-9114-7f05e3504998",
                    "orderInfo": {
                        "orderPhase": "ACTIVE",
                        "storeInfo": {
                            "storeUUID": "9198a3e7-1228-5dc3-b9f8-d0c862e95434",
                            "name": "Chicken Love You",
                        },
                    },
                    "activeOrderStatus": {
                        "title": "9:43 PM",
                        "subtitle": "Estimated arrival",
                    },
                }
            ]
        },
    }
    out = api.parse_active_orders(raw)
    assert len(out) == 1
    assert out[0]["uuid"] == "6f0dc742-c9c3-4bb2-9114-7f05e3504998"
    assert out[0]["restaurant"] == "Chicken Love You"
    assert out[0]["status"] == "ACTIVE · 9:43 PM · Estimated arrival"
    assert out[0]["store_uuid"] == "9198a3e7-1228-5dc3-b9f8-d0c862e95434"


def test_find_shopping_cart_line_raw_by_uuid_and_title() -> None:
    detail = {
        "data": {
            "draftOrder": {
                "uuid": "d-1",
                "storeUuid": "s-1",
                "shoppingCart": {
                    "items": [
                        {
                            "title": {"text": "Red Bull"},
                            "quantity": 1,
                            "shoppingCartItemUUID": "aaa-bbb",
                        }
                    ]
                },
            }
        }
    }
    by_uuid = api.find_shopping_cart_line_raw(
        detail, shopping_cart_item_uuid="aaa-bbb"
    )
    assert by_uuid is not None
    assert by_uuid["draft_order_uuid"] == "d-1"
    assert by_uuid["store_uuid"] == "s-1"
    assert by_uuid["shopping_cart_item_uuid"] == "aaa-bbb"

    by_title = api.find_shopping_cart_line_raw(detail, title_query="red bull")
    assert by_title is not None
    assert by_title["shopping_cart_item_uuid"] == "aaa-bbb"
