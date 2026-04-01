"""
Mood-based food recommendation engine.

Cross-references the user's mood, social context, taste profile,
dietary restrictions, budget, and nearby restaurant availability
to produce personalized suggestions.
"""

from __future__ import annotations

import re
from typing import Any

from . import preferences

# ── Mood → Cuisine Mapping ───────────────────────────────────────────────────

MOOD_CUISINE_MAP: dict[str, dict[str, Any]] = {
    "tired": {
        "label": "Tired / Lazy",
        "description": "You want something cozy and easy — no complicated decisions.",
        "cuisines": ["burgers", "pizza", "comfort", "asian", "chicken", "latin"],
        "keywords": ["comfort", "classic", "delivery", "easy"],
        "prefer_fast_eta": True,
    },
    "healthy": {
        "label": "Energetic / Healthy",
        "description": "Feeling good and want to keep the streak — light and nutritious.",
        "cuisines": ["healthy", "sushi", "seafood", "thai"],
        "keywords": ["salad", "bowl", "poke", "grain", "fresh", "light"],
        "prefer_fast_eta": False,
    },
    "celebrating": {
        "label": "Celebrating / Treat Yourself",
        "description": "Special occasion or just feeling great — go fancy.",
        "cuisines": ["sushi", "italian", "seafood", "desserts", "indian"],
        "keywords": ["premium", "special", "feast", "gourmet"],
        "prefer_fast_eta": False,
    },
    "stressed": {
        "label": "Stressed / Comfort",
        "description": "Need food that feels like a hug.",
        "cuisines": ["comfort", "burgers", "pizza", "desserts", "chicken", "latin"],
        "keywords": ["familiar", "comfort", "warm", "fried", "cheesy"],
        "prefer_fast_eta": True,
    },
    "adventurous": {
        "label": "Adventurous / Try Something New",
        "description": "Bored of the usual — surprise me with something different.",
        "cuisines": ["thai", "indian", "mexican", "chinese", "asian", "seafood"],
        "keywords": ["new", "different", "exotic", "spicy"],
        "prefer_new_restaurants": True,
        "prefer_fast_eta": False,
    },
    "quick": {
        "label": "Quick / Efficient",
        "description": "Not picky, just hungry — get me food fast.",
        "cuisines": ["burgers", "chicken", "sandwiches", "pizza", "convenience"],
        "keywords": ["fast", "quick", "nearby", "ready"],
        "prefer_fast_eta": True,
    },
    "craving": {
        "label": "Specific Craving",
        "description": "You know exactly what you want.",
        "cuisines": [],
        "keywords": [],
        "prefer_fast_eta": False,
    },
}

SOCIAL_CONTEXTS: dict[str, dict[str, Any]] = {
    "alone": {
        "label": "Eating alone",
        "typical_mains": 1,
        "typical_sides": 0,
        "typical_drinks": 1,
        "suggestion": "A single main + a drink is usually perfect.",
    },
    "couple": {
        "label": "Date / with partner",
        "typical_mains": 2,
        "typical_sides": 1,
        "typical_drinks": 2,
        "suggestion": "Two mains, maybe a shared appetizer or side, and drinks.",
    },
    "family": {
        "label": "Family dinner",
        "typical_mains": 4,
        "typical_sides": 2,
        "typical_drinks": 4,
        "suggestion": "A main for each person, a couple of sides to share, and drinks for everyone.",
    },
    "friends": {
        "label": "Hanging out with friends",
        "typical_mains": 3,
        "typical_sides": 3,
        "typical_drinks": 3,
        "suggestion": "Mains for each + shareable sides and snacks. Consider variety so everyone can try different things.",
    },
}


# ── Public API ───────────────────────────────────────────────────────────────

def get_mood_options() -> list[dict[str, str]]:
    """Return the available mood categories for the user to pick from."""
    return [
        {"id": mood_id, "label": info["label"], "description": info["description"]}
        for mood_id, info in MOOD_CUISINE_MAP.items()
    ]


def get_social_context_options() -> list[dict[str, str]]:
    """Return the available social context options."""
    prefs = preferences.load_preferences()
    custom_contexts = prefs.get("social_contexts", {})

    options = []
    for ctx_id, info in SOCIAL_CONTEXTS.items():
        custom = custom_contexts.get(ctx_id, {})
        people = custom.get("people", info.get("typical_mains", 1))
        options.append({
            "id": ctx_id,
            "label": info["label"],
            "people": str(people),
            "suggestion": info["suggestion"],
        })

    for ctx_id, custom in custom_contexts.items():
        if ctx_id not in SOCIAL_CONTEXTS:
            options.append({
                "id": ctx_id,
                "label": ctx_id.title(),
                "people": str(custom.get("people", 1)),
                "suggestion": f"Order for {custom.get('people', 1)} people.",
            })

    return options


def recommend(
    mood: str,
    social_context: str = "alone",
    nearby_stores: list[dict[str, Any]] | None = None,
    craving_text: str = "",
) -> dict[str, Any]:
    """Generate food recommendations based on mood, context, and preferences.

    Returns a structured suggestion with ranked restaurants and reasoning.
    """
    prefs = preferences.load_preferences()
    mood_info = MOOD_CUISINE_MAP.get(mood, MOOD_CUISINE_MAP["tired"])
    ctx_info = SOCIAL_CONTEXTS.get(social_context, SOCIAL_CONTEXTS["alone"])
    custom_ctx = prefs.get("social_contexts", {}).get(social_context, {})
    people_count = custom_ctx.get("people", ctx_info.get("typical_mains", 1))

    budget_per_person = prefs.get("budget", {}).get("default_per_person", 0)
    budget_mult = custom_ctx.get("budget_multiplier", float(people_count))
    total_budget = int(budget_per_person * budget_mult) if budget_per_person else 0
    currency = prefs.get("budget", {}).get("currency", "")

    target_cuisines = mood_info["cuisines"]
    dietary_restrictions = prefs.get("dietary", {}).get("restrictions", [])
    dietary_avoid = prefs.get("dietary", {}).get("avoid", [])
    dietary_prefs = prefs.get("dietary", {}).get("preferences", [])

    taste = prefs.get("taste_profile", {})
    cuisine_scores = taste.get("cuisine_scores", {})
    restaurant_freq = taste.get("restaurant_frequency", {})

    ranked_stores: list[dict[str, Any]] = []
    if nearby_stores:
        for store in nearby_stores:
            if not store.get("is_open", True):
                continue

            score = 0.0
            name_lower = store.get("name", "").lower()
            reasons: list[str] = []

            for cuisine in target_cuisines:
                for kw in preferences.CUISINE_KEYWORDS.get(cuisine, []):
                    if kw in name_lower:
                        score += 3
                        reasons.append(f"matches your {mood} mood ({cuisine})")
                        break
                for hint_key, hint_cuisine in preferences.RESTAURANT_CUISINE_HINTS.items():
                    if hint_key in name_lower and hint_cuisine in target_cuisines:
                        score += 3
                        reasons.append(f"known for {hint_cuisine}")
                        break

            if craving_text:
                for word in craving_text.lower().split():
                    if word in name_lower:
                        score += 5
                        reasons.append(f"matches your craving for '{craving_text}'")
                        break

            for pref_cuisine in dietary_prefs:
                if pref_cuisine.lower() in name_lower:
                    score += 1
                    reasons.append(f"fits your preference for {pref_cuisine}")

            skip = False
            for avoid in dietary_avoid:
                if avoid.lower() in name_lower:
                    skip = True
                    break
            if skip:
                continue

            if store.get("name", "") in restaurant_freq:
                freq = restaurant_freq[store["name"]]
                if mood_info.get("prefer_new_restaurants"):
                    score -= freq * 0.5
                    if freq > 2:
                        reasons.append("you order here often (trying new things!)")
                else:
                    score += min(freq, 3)
                    if freq >= 2:
                        reasons.append("one of your regulars")

            eta_str = store.get("eta", "")
            eta_match = re.search(r"(\d+)", eta_str)
            if eta_match and mood_info.get("prefer_fast_eta"):
                eta_min = int(eta_match.group(1))
                if eta_min <= 20:
                    score += 2
                    reasons.append(f"fast delivery ({eta_str})")

            rating_str = store.get("rating", "")
            try:
                rating = float(rating_str)
                if rating >= 4.5:
                    score += 1.5
                elif rating >= 4.0:
                    score += 0.5
            except (ValueError, TypeError):
                pass

            ranked_stores.append({
                **store,
                "_score": score,
                "reasons": list(dict.fromkeys(reasons))[:3],
            })

        ranked_stores.sort(key=lambda s: s["_score"], reverse=True)
        for s in ranked_stores:
            del s["_score"]

    cart_suggestion = _build_cart_suggestion(social_context, people_count)

    budget_note = ""
    if total_budget:
        budget_note = f"Budget: ~{currency} {total_budget:,} total ({currency} {budget_per_person:,}/person x {people_count})"

    return {
        "mood": mood_info["label"],
        "mood_description": mood_info["description"],
        "social_context": ctx_info["label"],
        "people": people_count,
        "suggested_cuisines": target_cuisines,
        "dietary_notes": {
            "restrictions": dietary_restrictions,
            "avoid": dietary_avoid,
            "preferences": dietary_prefs,
        } if any([dietary_restrictions, dietary_avoid, dietary_prefs]) else None,
        "budget": budget_note or None,
        "top_picks": ranked_stores[:5],
        "cart_suggestion": cart_suggestion,
        "all_open_matches": len(ranked_stores),
    }


def build_cart_suggestion(
    social_context: str,
    menu_sections: list[dict[str, Any]],
) -> dict[str, Any]:
    """Given a restaurant menu and social context, suggest what to order."""
    prefs = preferences.load_preferences()
    custom_ctx = prefs.get("social_contexts", {}).get(social_context, {})
    ctx_info = SOCIAL_CONTEXTS.get(social_context, SOCIAL_CONTEXTS["alone"])
    people = custom_ctx.get("people", ctx_info.get("typical_mains", 1))

    budget_per_person = prefs.get("budget", {}).get("default_per_person", 0)
    currency = prefs.get("budget", {}).get("currency", "")
    dietary_avoid = prefs.get("dietary", {}).get("avoid", [])

    suggestions: list[dict[str, Any]] = []
    mains: list[dict[str, Any]] = []
    sides: list[dict[str, Any]] = []
    drinks: list[dict[str, Any]] = []

    main_kw = ["combo", "meal", "plate", "plato", "burger", "pizza", "sushi", "bowl", "sandwich", "wrap", "main"]
    side_kw = ["side", "fries", "onion rings", "salad", "appetizer", "entrada", "snack"]
    drink_kw = ["drink", "soda", "juice", "water", "beer", "cola", "sprite", "fanta", "red bull", "coffee"]

    for section in menu_sections:
        section_name = section.get("section", "").lower()
        for item in section.get("items", []):
            name_lower = item.get("name", "").lower()

            if any(a.lower() in name_lower for a in dietary_avoid):
                continue

            entry = {
                "name": item.get("name", ""),
                "price": item.get("price", ""),
                "price_cents": item.get("price_cents", 0),
            }

            if any(kw in name_lower or kw in section_name for kw in drink_kw):
                drinks.append(entry)
            elif any(kw in name_lower or kw in section_name for kw in side_kw):
                sides.append(entry)
            else:
                mains.append(entry)

    typical_mains = ctx_info.get("typical_mains", people)
    typical_sides = ctx_info.get("typical_sides", 0)
    typical_drinks = ctx_info.get("typical_drinks", people)

    def _pick_best(items: list[dict], count: int) -> list[dict]:
        items.sort(key=lambda x: x.get("price_cents", 0), reverse=True)
        return items[:count]

    suggested_mains = _pick_best(mains, typical_mains)
    suggested_sides = _pick_best(sides, typical_sides)
    suggested_drinks = _pick_best(drinks, typical_drinks)

    total_cents = sum(
        i.get("price_cents", 0)
        for i in suggested_mains + suggested_sides + suggested_drinks
    )

    result: dict[str, Any] = {
        "for_people": people,
        "context": ctx_info["label"],
        "suggestion_note": ctx_info["suggestion"],
        "suggested_mains": suggested_mains,
        "suggested_sides": suggested_sides,
        "suggested_drinks": suggested_drinks,
        "estimated_total_cents": total_cents,
    }

    if budget_per_person and currency:
        total_budget = budget_per_person * people
        result["budget_note"] = f"{currency} {total_budget:,} budget vs ~{currency} {total_cents // 100:,} estimated"

    return result


# ── Internal Helpers ─────────────────────────────────────────────────────────

def _build_cart_suggestion(
    social_context: str,
    people_count: int,
) -> dict[str, str]:
    ctx_info = SOCIAL_CONTEXTS.get(social_context, SOCIAL_CONTEXTS["alone"])
    return {
        "people": str(people_count),
        "mains": str(ctx_info.get("typical_mains", people_count)),
        "sides": str(ctx_info.get("typical_sides", 0)),
        "drinks": str(ctx_info.get("typical_drinks", people_count)),
        "tip": ctx_info["suggestion"],
    }
