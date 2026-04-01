"""
User preferences for the Uber Eats assistant.

Stores defaults (address, payment, tip), dietary info, favorites,
budget, social contexts, taste profile (auto-built from orders),
and mood session history — all in ~/.ubereats-preferences.json.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

PREFS_PATH = Path.home() / ".ubereats-preferences.json"

DEFAULT_PREFERENCES: dict[str, Any] = {
    "default_address": "",
    "default_payment": "",
    "default_tip_percent": 10,
    "dietary": {
        "restrictions": [],
        "preferences": [],
        "avoid": [],
    },
    "favorites": {
        "restaurants": [],
        "items": [],
    },
    "budget": {
        "default_per_person": 0,
        "currency": "",
    },
    "language": "en",
    "social_contexts": {
        "alone": {"people": 1, "budget_multiplier": 1.0},
        "couple": {"people": 2, "budget_multiplier": 2.0},
        "family": {"people": 4, "budget_multiplier": 4.0},
        "friends": {"people": 3, "budget_multiplier": 3.0},
    },
    "taste_profile": {
        "cuisine_scores": {},
        "restaurant_frequency": {},
        "last_updated": "",
        "orders_analyzed": 0,
    },
    "mood_history": [],
}

MAX_MOOD_HISTORY = 50


# ── Load / Save ──────────────────────────────────────────────────────────────

def load_preferences() -> dict[str, Any]:
    if not PREFS_PATH.exists():
        return _deep_copy(DEFAULT_PREFERENCES)
    try:
        data = json.loads(PREFS_PATH.read_text())
        merged = _deep_copy(DEFAULT_PREFERENCES)
        _deep_merge(merged, data)
        return merged
    except Exception:
        return _deep_copy(DEFAULT_PREFERENCES)


def save_preferences(prefs: dict[str, Any]) -> None:
    PREFS_PATH.write_text(json.dumps(prefs, indent=2, ensure_ascii=False))


# ── CRUD ─────────────────────────────────────────────────────────────────────

def get_default(key: str) -> Any:
    prefs = load_preferences()
    return _get_nested(prefs, key)


def update_preference(key: str, value: Any) -> dict[str, Any]:
    """Set a preference by dotted key (e.g. 'dietary.restrictions')."""
    prefs = load_preferences()
    _set_nested(prefs, key, value)
    save_preferences(prefs)
    return prefs


def update_social_context(
    name: str,
    people: int | None = None,
    budget_multiplier: float | None = None,
) -> dict[str, Any]:
    prefs = load_preferences()
    ctx = prefs["social_contexts"].get(name, {"people": 1, "budget_multiplier": 1.0})
    if people is not None:
        ctx["people"] = people
    if budget_multiplier is not None:
        ctx["budget_multiplier"] = budget_multiplier
    prefs["social_contexts"][name] = ctx
    save_preferences(prefs)
    return prefs


# ── Favorites ────────────────────────────────────────────────────────────────

def add_favorite(
    kind: str,
    item: dict[str, str],
) -> dict[str, Any]:
    """Add a favorite restaurant or item.

    kind: "restaurants" or "items"
    item: dict with name (+ uuid/url for restaurants, or restaurant for items)
    """
    prefs = load_preferences()
    favs = prefs["favorites"].get(kind, [])
    if not any(f.get("name", "").lower() == item.get("name", "").lower() for f in favs):
        favs.append(item)
    prefs["favorites"][kind] = favs
    save_preferences(prefs)
    return prefs


def remove_favorite(kind: str, name: str) -> dict[str, Any]:
    prefs = load_preferences()
    favs = prefs["favorites"].get(kind, [])
    prefs["favorites"][kind] = [
        f for f in favs if f.get("name", "").lower() != name.lower()
    ]
    save_preferences(prefs)
    return prefs


# ── Taste Profile Builder ────────────────────────────────────────────────────

CUISINE_KEYWORDS: dict[str, list[str]] = {
    "burgers": ["burger", "whopper", "big mac", "hamburger", "cheeseburger"],
    "pizza": ["pizza", "pepperoni", "margherita", "calzone"],
    "sushi": ["sushi", "roll", "sashimi", "nigiri", "temaki", "maki"],
    "mexican": ["taco", "burrito", "quesadilla", "nacho", "enchilada"],
    "chinese": ["chow mein", "fried rice", "kung pao", "dim sum", "wonton", "lo mein"],
    "thai": ["pad thai", "curry", "tom yum", "satay", "green curry"],
    "indian": ["tikka", "naan", "biryani", "masala", "samosa", "tandoori"],
    "italian": ["pasta", "risotto", "lasagna", "ravioli", "gnocchi", "tiramisu"],
    "healthy": ["salad", "bowl", "grain", "poke", "acai", "smoothie", "quinoa"],
    "comfort": ["mac and cheese", "fried chicken", "wings", "fries", "onion rings"],
    "desserts": ["ice cream", "cake", "brownie", "cookie", "donut", "churro"],
    "drinks": ["coffee", "tea", "juice", "smoothie", "shake", "latte", "frappuccino"],
    "breakfast": ["pancake", "waffle", "eggs", "bacon", "omelette", "toast"],
    "chicken": ["chicken", "pollo", "nugget", "tender", "strip"],
    "seafood": ["fish", "shrimp", "salmon", "ceviche", "lobster", "crab"],
    "sandwiches": ["sandwich", "sub", "wrap", "panini", "baguette"],
    "asian": ["ramen", "pho", "bibimbap", "gyoza", "udon", "teriyaki"],
    "latin": ["empanada", "arepa", "lomo", "churrasco", "completo", "sopaipilla"],
    "convenience": ["red bull", "energy", "snack", "chips", "candy", "soda", "beer", "water"],
}

RESTAURANT_CUISINE_HINTS: dict[str, str] = {
    "burger king": "burgers",
    "mcdonald": "burgers",
    "subway": "sandwiches",
    "domino": "pizza",
    "papa john": "pizza",
    "pizza hut": "pizza",
    "starbucks": "drinks",
    "sushi": "sushi",
    "taco bell": "mexican",
    "kfc": "chicken",
    "popeyes": "chicken",
    "panda express": "chinese",
    "oxxo": "convenience",
    "pronto copec": "convenience",
}


def build_taste_profile(orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Analyze past orders and build/update the taste profile.

    Expects orders in the format returned by api.parse_orders().
    """
    prefs = load_preferences()
    cuisine_counts: Counter[str] = Counter()
    restaurant_counts: Counter[str] = Counter()

    for order in orders:
        restaurant = order.get("restaurant", "").lower()
        restaurant_counts[order.get("restaurant", "Unknown")] += 1

        for hint_key, cuisine in RESTAURANT_CUISINE_HINTS.items():
            if hint_key in restaurant:
                cuisine_counts[cuisine] += 1
                break

        for item in order.get("items", []):
            item_name = item.get("name", "").lower()
            for cuisine, keywords in CUISINE_KEYWORDS.items():
                if any(kw in item_name for kw in keywords):
                    cuisine_counts[cuisine] += 1

    existing_scores = prefs["taste_profile"].get("cuisine_scores", {})
    for cuisine, count in cuisine_counts.items():
        existing_scores[cuisine] = existing_scores.get(cuisine, 0) + count
    existing_freq = prefs["taste_profile"].get("restaurant_frequency", {})
    for name, count in restaurant_counts.items():
        existing_freq[name] = existing_freq.get(name, 0) + count

    prefs["taste_profile"]["cuisine_scores"] = dict(
        sorted(existing_scores.items(), key=lambda x: x[1], reverse=True)
    )
    prefs["taste_profile"]["restaurant_frequency"] = dict(
        sorted(existing_freq.items(), key=lambda x: x[1], reverse=True)
    )
    prefs["taste_profile"]["last_updated"] = date.today().isoformat()
    prefs["taste_profile"]["orders_analyzed"] = (
        prefs["taste_profile"].get("orders_analyzed", 0) + len(orders)
    )

    save_preferences(prefs)
    return prefs["taste_profile"]


# ── Mood History ─────────────────────────────────────────────────────────────

def record_mood_session(
    mood: str,
    social_context: str,
    chosen_restaurant: str = "",
    chosen_items: list[str] | None = None,
) -> None:
    prefs = load_preferences()
    entry = {
        "date": date.today().isoformat(),
        "mood": mood,
        "social_context": social_context,
        "chosen_restaurant": chosen_restaurant,
        "chosen_items": chosen_items or [],
    }
    history = prefs.get("mood_history", [])
    history.append(entry)
    prefs["mood_history"] = history[-MAX_MOOD_HISTORY:]
    save_preferences(prefs)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _deep_copy(d: dict) -> dict:
    return json.loads(json.dumps(d))


def _deep_merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _get_nested(d: dict, dotted_key: str) -> Any:
    keys = dotted_key.split(".")
    current = d
    for k in keys:
        if isinstance(current, dict) and k in current:
            current = current[k]
        else:
            return None
    return current


def _set_nested(d: dict, dotted_key: str, value: Any) -> None:
    keys = dotted_key.split(".")
    current = d
    for k in keys[:-1]:
        if k not in current or not isinstance(current[k], dict):
            current[k] = {}
        current = current[k]
    current[keys[-1]] = value
