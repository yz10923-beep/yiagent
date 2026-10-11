"""List prices, first-party API, $ per million tokens.

Each rate is stored as published, not derived from input by a multiplier: cache reads
are 0.1x input on most models but 0.05x on Opus 5.5 and Sonnet 5.5 and 0.025x on
Fable 5.1. Cache writes are priced at the 5-minute rate, the only TTL the provider sets.
Source: platform.claude.com/docs/en/about-claude/pricing, checked 2026-10-11.

Cost is always derived from the model that *served* a call and its recorded usage, never
assumed per run.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import NamedTuple


class Rates(NamedTuple):
    input: float
    cache_write: float  # 5-minute TTL
    cache_read: float
    output: float


PRICES: dict[str, Rates] = {
    "claude-fable-5-1": Rates(10.0, 12.5, 0.25, 50.0),
    "claude-fable-5": Rates(10.0, 12.5, 1.0, 50.0),
    "claude-opus-5-5": Rates(4.0, 5.0, 0.20, 20.0),
    "claude-opus-5": Rates(5.0, 6.25, 0.50, 25.0),
    "claude-opus-4-8": Rates(5.0, 6.25, 0.50, 25.0),
    "claude-sonnet-5-5": Rates(2.0, 2.5, 0.10, 10.0),
    "claude-sonnet-5": Rates(2.0, 2.5, 0.20, 10.0),
    "claude-sonnet-4-6": Rates(3.0, 3.75, 0.30, 15.0),
    "claude-haiku-5-5": Rates(0.10, 0.125, 0.01, 0.50),
    "claude-haiku-4-5": Rates(1.0, 1.25, 0.10, 5.0),
}

# Haiku 5.5 has a second rate card for a prompt over 100K tokens, where the prompt counts
# all input: uncached, cache writes and cache reads.
LONG_PROMPT_TOKENS = 100_000
LONG_PROMPT_PRICES: dict[str, Rates] = {
    "claude-haiku-5-5": Rates(0.50, 0.625, 0.05, 2.50),
}

# A served model id is the alias itself or the alias plus a snapshot date
# ("claude-haiku-4-5-20251001"). Prefix matching is not safe: "claude-sonnet-5" is a
# prefix of "claude-sonnet-5-5", so an unlisted newer model would get an old price.
_DATED = re.compile(r"(?P<alias>.+)-\d{8}")


def _alias(model: str) -> str | None:
    if model in PRICES:
        return model
    m = _DATED.fullmatch(model)
    return m["alias"] if m and m["alias"] in PRICES else None


def cost_usd(model: str, usage: Mapping[str, int]) -> float | None:
    """None if the model has no known price (better than a silent $0).

    The long-prompt card is chosen from the usage given, so pass one request's usage:
    summed over several requests, Haiku 5.5 would be priced as one long prompt.
    """
    alias = _alias(model)
    if alias is None:
        return None
    uncached = usage.get("input_tokens", 0)
    written = usage.get("cache_creation_input_tokens", 0)
    read = usage.get("cache_read_input_tokens", 0)
    rates = PRICES[alias]
    if alias in LONG_PROMPT_PRICES and uncached + written + read > LONG_PROMPT_TOKENS:
        rates = LONG_PROMPT_PRICES[alias]
    return (
        uncached * rates.input
        + written * rates.cache_write
        + read * rates.cache_read
        + usage.get("output_tokens", 0) * rates.output
    ) / 1e6
