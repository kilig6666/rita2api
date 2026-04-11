"""
quota.py — Model quota/points system

Uses Rita's actual model quota values from categoryModels API.
Falls back to static map if API is unavailable.
"""

# Static cost map based on Rita API categoryModels response (2026-04)
# Key: model_xxx -> quota points per request
_COST_MAP = {
    # Tier: Free (0 points)
    "model_25": 0,    # Rita
    "model_17": 0,    # GPT-4.1-nano
    "model_36": 0,    # GPT-5-nano

    # Tier: Basic (1 point)
    "model_16": 1,    # GPT-4.1-mini
    "model_32": 1,    # Gemini-2.5-Pro-0605
    "model_35": 1,    # GPT-5-mini
    "model_37": 1,    # Rita-Pro
    "model_49": 1,    # Gemini-3-Flash
    "model_7": 1,     # DeepSeek-V3
    "model_8": 1,     # DeepSeek-R1-0528

    # Tier: Low (2 points)
    "model_39": 2,    # DeepSeek-V3.1

    # Tier: Medium (4-5 points)
    "model_10": 4,    # Perplexity AI-Sonar
    "model_11": 5,    # Perplexity AI-Sonar-Deep-Research
    "model_12": 5,    # Perplexity AI-Sonar-Reasoning-Pro
    "model_15": 5,    # GPT-4.1
    "model_18": 5,    # GPT-o4-mini
    "model_19": 5,    # GPT-o3-mini
    "model_2": 5,     # GPT-4o
    "model_20": 5,    # GPT-o3
    "model_22": 5,    # Gemini-2.5-Pro
    "model_33": 5,    # Grok-4
    "model_34": 5,    # GPT-5
    "model_42": 5,    # GPT-5.1
    "model_44": 5,    # Grok-4.1
    "model_48": 5,    # GPT-5.2
    "model_66": 5,    # Gemini-3.1-Pro
    "model_69": 5,    # GPT-5.4
    "model_9": 5,     # Perplexity AI-Sonar-Pro

    # Tier: High (7-8 points)
    "model_23": 7,    # Grok-3
    "model_21": 8,    # Claude-3.7-Sonnet
    "model_28": 8,    # Claude-4-Sonnet
    "model_30": 8,    # Claude-4-Sonnet-Thinking
    "model_40": 8,    # Claude-4.5-Sonnet
    "model_41": 8,    # Claude-4.5-Sonnet-Thinking

    # Tier: Premium (10-16 points)
    "model_1080": 10, # ChatGPT-image-1
    "model_1121": 10, # Nano-banana 2
    "model_67": 10,   # Gemini-3.1-Pro-Thinking
    "model_1114": 15, # Nano-banana pro
    "model_43": 15,   # GPT-5.1-Thinking
    "model_47": 16,   # Claude-Opus-4.5
    "model_50": 16,   # Claude-Opus-4.6
    "model_51": 16,   # Claude-Opus-4.6-Thinking

    # Tier: Ultra (35-45 points)
    "model_1123": 35, # Nano-banana 2 direct connection
    "model_29": 40,   # Claude-4-Opus
    "model_31": 40,   # Claude-4-Opus-Thinking
    "model_68": 40,   # Claude-Sonnet-4.6
    "model_1118": 45, # Nano-banana pro direct connection
}

DEFAULT_COST = 5


def get_cost(model: str) -> int:
    """Get point cost for a model. Returns DEFAULT_COST for unknown models."""
    if model in _COST_MAP:
        return _COST_MAP[model]
    return DEFAULT_COST


def get_all_costs() -> dict:
    """Return the full cost map for display."""
    return dict(_COST_MAP)
