"""Feed cache key + merged search parsing (stores + catalog items)."""

import json

from uber_eats_mcp.api import (
    _encode_feed_cache_key,
    merge_parsed_search_results,
    parse_feed_search_results,
)


def test_encode_feed_cache_key_matches_browser_vector() -> None:
    blob = {
        "address": "Bernardo Larraín Cotapos 11822",
        "reference": "ChIJ-T4_DYDJYpYRarx3vWTyMXo",
        "referenceType": "google_places",
        "latitude": -33.3382848,
        "longitude": -70.5271393,
    }
    key = _encode_feed_cache_key(blob)
    expected_b64 = (
        "JTdCJTIyYWRkcmVzcyUyMiUzQSUyMkJlcm5hcmRvJTIwTGFycmElQzMlQURuJTIwQ290YXBvcyUyMDExODIyJTIyJTJDJTIycmVmZXJlbmNlJTIyJTNBJTIyQ2hJSi1UNF9EWURKWXBZUmFyeDN2V1R5TVhvJTIyJTJDJTIycmVmZXJlbmNlVHlwZSUyMiUzQSUyMmdvb2dsZV9wbGFjZXMlMjIlMkMlMjJsYXRpdHVkZSUyMiUzQS0zMy4zMzgyODQ4JTJDJTIybG9uZ2l0dWRlJTIyJTNBLTcwLjUyNzEzOTMlN0Q="
    )
    assert key == expected_b64 + "/DELIVERY///0/0//%5B%5D/undefined//////HOME////////"


def test_parse_feed_search_results_merges_catalog_items() -> None:
    raw = {
        "status": "success",
        "data": {
            "feedItems": [
                {
                    "uuid": "s1",
                    "type": "REGULAR_STORE",
                    "store": {
                        "title": {"text": "Mini Market"},
                        "rating": {"text": "4.9"},
                        "meta": [{"text": "10 min", "badgeType": "ETD"}],
                        "meta2": [],
                        "actionUrl": "/store/mini/x1",
                    },
                },
                {
                    "uuid": "c1",
                    "type": "CATALOG_ITEMS_CAROUSEL_PAYLOAD",
                    "payload": {
                        "header": {
                            "title": {
                                "actionUrl": (
                                    "/collection-page?collectionName=bev&storeUUID=abc-store-uuid"
                                )
                            },
                            "subtitle": {"text": "From Mini Market"},
                        },
                        "items": [
                            {
                                "catalogItem": {
                                    "uuid": "item-uuid-1",
                                    "title": "Red Bull 250ml",
                                    "price": 199000,
                                    "sectionUuid": "sec-1",
                                    "subsectionUuid": "sub-1",
                                    "imageUrl": "https://example.com/i.jpg",
                                }
                            }
                        ],
                    },
                },
            ]
        },
    }
    out = parse_feed_search_results(raw)
    assert len(out["stores"]) == 1
    assert out["stores"][0]["name"] == "Mini Market"
    assert len(out["items"]) == 1
    assert out["items"][0]["name"] == "Red Bull 250ml"
    assert out["items"][0]["store_uuid"] == "abc-store-uuid"
    assert out["items"][0]["menu_item_uuid"] == "item-uuid-1"
    assert "CATALOG_ITEMS_CAROUSEL_PAYLOAD" in out["feed_item_types"]


def test_parse_feed_search_results_clears_section_when_same_as_store() -> None:
    raw = {
        "status": "success",
        "data": {
            "feedItems": [
                {
                    "type": "CATALOG_ITEMS_CAROUSEL_PAYLOAD",
                    "payload": {
                        "header": {
                            "title": {"actionUrl": "/c?storeUUID=store-xyz"},
                            "subtitle": {"text": "Shop"},
                        },
                        "items": [
                            {
                                "catalogItem": {
                                    "uuid": "sku-1",
                                    "title": "Item",
                                    "price": 100,
                                    "sectionUuid": "store-xyz",
                                    "subsectionUuid": "sub",
                                    "imageUrl": "",
                                }
                            }
                        ],
                    },
                }
            ]
        },
    }
    out = parse_feed_search_results(raw)
    assert out["items"][0]["section_uuid"] == ""
    assert out["items"][0]["store_uuid"] == "store-xyz"


def test_parse_feed_search_results_catalog_item_promotion() -> None:
    raw = {
        "status": "success",
        "data": {
            "feedItems": [
                {
                    "uuid": "c1",
                    "type": "CATALOG_ITEMS_CAROUSEL_PAYLOAD",
                    "payload": {
                        "header": {
                            "title": {
                                "actionUrl": "/collection-page?storeUUID=store-x",
                            },
                            "subtitle": {"text": "Market"},
                        },
                        "items": [
                            {
                                "catalogItem": {
                                    "uuid": "item-2",
                                    "title": "Snack Pack",
                                    "price": 990000,
                                    "sectionUuid": "s",
                                    "subsectionUuid": "ss",
                                    "imageUrl": "",
                                    "itemPromotion": {
                                        "originalPrice": {"text": "CLP 1,500"},
                                        "discountedPrice": {"text": "CLP 990"},
                                    },
                                }
                            }
                        ],
                    },
                }
            ]
        },
    }
    out = parse_feed_search_results(raw)
    assert len(out["items"]) == 1
    it = out["items"][0]
    assert it.get("on_offer") is True
    assert it.get("offer_summary")
    assert "1,500" in it["regular_price"] or "1500" in it["regular_price"].replace(",", "")


def test_parse_feed_search_results_carousel_store() -> None:
    raw = {
        "status": "success",
        "data": {
            "feedItems": [
                {
                    "type": "REGULAR_CAROUSEL",
                    "carousel": {
                        "stores": [
                            {
                                "storeUuid": "carousel-uu",
                                "title": {"text": "Oxxo"},
                                "meta": [{"text": "15 min", "badgeType": "ETD"}],
                                "rating": {"text": "4.2"},
                                "actionUrl": "/store/oxxo/slug",
                            }
                        ]
                    },
                }
            ]
        },
    }
    out = parse_feed_search_results(raw)
    assert len(out["stores"]) == 1
    assert out["stores"][0]["uuid"] == "carousel-uu"
    assert out["stores"][0]["name"] == "Oxxo"


def test_parse_feed_search_results_mini_store_with_items() -> None:
    raw = {
        "status": "success",
        "data": {
            "feedItems": [
                {
                    "uuid": "feed-1",
                    "type": "MINI_STORE_WITH_ITEMS",
                    "miniStoreWithItems": {
                        "store": {
                            "title": {"text": "Corner Store"},
                            "rating": {"text": "4.8"},
                            "meta": [{"text": "12 min", "badgeType": "ETD"}],
                            "meta2": [],
                            "actionUrl": "/store/corner/x?storeUUID=store-mini-1",
                        },
                        "items": [
                            {
                                "uuid": "item-a",
                                "title": {"text": "Energy Drink 250ml"},
                                "sectionUuid": "sec-a",
                                "subsectionUuid": "sub-a",
                                "price": 1500,
                                "imageUrl": "https://example.com/e.jpg",
                            }
                        ],
                    },
                }
            ]
        },
    }
    out = parse_feed_search_results(raw)
    assert len(out["stores"]) == 1
    assert out["stores"][0]["uuid"] == "store-mini-1"
    assert out["stores"][0]["name"] == "Corner Store"
    assert len(out["items"]) == 1
    assert out["items"][0]["name"] == "Energy Drink 250ml"
    assert out["items"][0]["menu_item_uuid"] == "item-a"
    assert out["items"][0]["store_uuid"] == "store-mini-1"
    assert "MINI_STORE_WITH_ITEMS" in out["feed_item_types"]


def test_merge_parsed_search_results_dedupes() -> None:
    a = parse_feed_search_results(
        {
            "status": "success",
            "data": {
                "feedItems": [
                    {
                        "uuid": "s1",
                        "type": "REGULAR_STORE",
                        "store": {
                            "title": {"text": "A"},
                            "rating": {"text": ""},
                            "meta": [],
                            "meta2": [],
                            "actionUrl": "/store/a?storeUUID=uu1",
                        },
                    }
                ]
            },
        }
    )
    b = parse_feed_search_results(
        {
            "status": "success",
            "data": {
                "feedItems": [
                    {
                        "uuid": "s1",
                        "type": "REGULAR_STORE",
                        "store": {
                            "title": {"text": "A"},
                            "rating": {"text": ""},
                            "meta": [],
                            "meta2": [],
                            "actionUrl": "/store/a?storeUUID=uu1",
                        },
                    },
                    {
                        "uuid": "c1",
                        "type": "CATALOG_ITEMS_CAROUSEL_PAYLOAD",
                        "payload": {
                            "header": {
                                "title": {
                                    "actionUrl": "/collection?storeUUID=uu1",
                                },
                                "subtitle": {"text": "From A"},
                            },
                            "items": [
                                {
                                    "catalogItem": {
                                        "uuid": "cat-1",
                                        "title": "Snack",
                                        "price": 100,
                                        "sectionUuid": "s",
                                        "subsectionUuid": "ss",
                                        "imageUrl": "",
                                    }
                                }
                            ],
                        },
                    },
                ]
            },
        }
    )
    m = merge_parsed_search_results(a, b)
    assert len(m["stores"]) == 1
    assert len(m["items"]) == 1
