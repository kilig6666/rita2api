"""
quota.py — Model quota/points system

Each account starts with 100 points.
Different models consume different points per request.
"""

# Model cost tiers
_COST_MAP = {
    # Tier 1 — Basic (1 point)
    "gpt-3.5-turbo": 1,
    "gemini-1.5-flash": 1,
    "gemini-2.0-flash": 1,
    "gemini-2.5-flash": 1,
    "mixtral-8x7b": 1,
    "model_25": 1,  # rita default

    # Tier 2 — Standard (2 points)
    "gpt-4o": 2,
    "gpt-4o-mini": 2,
    "chatgpt-4o-latest": 2,
    "claude-3.5-sonnet": 2,
    "claude-3.5-haiku": 2,
    "claude-4.5-haiku": 2,
    "claude-4.6": 2,
    "gemini-1.5-pro": 2,
    "gemini-2.5-pro": 2,
    "grok-2": 2,
    "deepseek-v3": 2,
    "mistral-large": 2,

    # Tier 3 — Premium (5 points)
    "gpt-4": 5,
    "gpt-4-turbo": 5,
    "claude-opus-4-6": 5,
    "reasoning": 5,
    "reasoning-preview": 5,
    "reasoning-mini": 5,
    "grok-3": 5,
    "deepseek-r1": 5,
}

DEFAULT_COST = 2


def get_cost(model: str) -> int:
    """Get point cost for a model. Returns DEFAULT_COST for unknown models."""
    model_lower = model.lower()
    if model_lower in _COST_MAP:
        return _COST_MAP[model_lower]
    # Prefix match
    for key, cost in _COST_MAP.items():
        if model_lower.startswith(key):
            return cost
    return DEFAULT_COST


def get_all_costs() -> dict:
    """Return the full cost map for display."""
    return dict(_COST_MAP)
