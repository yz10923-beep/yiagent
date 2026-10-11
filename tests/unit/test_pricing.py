from __future__ import annotations

import pytest

from yi_agent.llm.pricing import cost_usd

M = 1_000_000


def test_served_ids_match_exactly_or_with_a_snapshot_date() -> None:
    usage = {"input_tokens": M}
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(4.0)  # not Opus 5's $5
    assert cost_usd("claude-opus-5", usage) == pytest.approx(5.0)
    assert cost_usd("claude-haiku-4-5-20251001", usage) == pytest.approx(1.0)


def test_unknown_models_are_none_never_a_silent_zero_or_an_older_price() -> None:
    usage = {"input_tokens": M}
    assert cost_usd("fake-model", usage) is None
    # an unlisted newer model must not inherit the price of its prefix
    assert cost_usd("claude-sonnet-5-6", usage) is None
    assert cost_usd("claude-opus-5-5-preview", usage) is None


def test_each_token_kind_has_its_own_published_rate() -> None:
    usage = {
        "input_tokens": M,
        "cache_creation_input_tokens": M,
        "cache_read_input_tokens": M,
        "output_tokens": M,
    }
    assert cost_usd("claude-opus-5", usage) == pytest.approx(5.0 + 6.25 + 0.50 + 25.0)
    # cache reads are 0.05x input on Opus 5.5 and Sonnet 5.5, 0.025x on Fable 5.1
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(4.0 + 5.0 + 0.20 + 20.0)
    assert cost_usd("claude-sonnet-5-5", usage) == pytest.approx(2.0 + 2.5 + 0.10 + 10.0)
    assert cost_usd("claude-fable-5-1", usage) == pytest.approx(10.0 + 12.5 + 0.25 + 50.0)


def test_haiku_5_5_prompts_over_100k_tokens_use_the_long_card() -> None:
    short = {"input_tokens": 100_000, "output_tokens": M}
    assert cost_usd("claude-haiku-5-5", short) == pytest.approx(0.01 + 0.50)
    # the prompt counts cache reads and writes too: 1 + 50K + 50K is over the line
    long = {
        "input_tokens": 1,
        "cache_creation_input_tokens": 50_000,
        "cache_read_input_tokens": 50_000,
        "output_tokens": M,
    }
    expected = (1 * 0.50 + 50_000 * 0.625 + 50_000 * 0.05) / M + 2.50
    assert cost_usd("claude-haiku-5-5", long) == pytest.approx(expected)
