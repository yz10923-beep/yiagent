"""Context: results cut at the cap on whole lines, the output store, and the meter
(vendored from kama S6; the loop-level tests stay in kama)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from yi_agent.context import FRESH_PRIOR, RATIO_RANGE, ContextMeter, estimate_tokens
from yi_agent.llm.anthropic_provider import AnthropicProvider
from yi_agent.llm.types import Message, ToolSpec, Usage
from yi_agent.outputs import OutputStore, cut

LINES = "".join(f"line {i:05d} " + "x" * 40 + "\n" for i in range(1, 2001))  # ~100K chars


# ---------------------------------------------------------------- the cut and the store


def test_cut_keeps_whole_lines_and_names_whats_missing() -> None:
    shown, info = cut(LINES, 30_000, "t1")
    head, tail = shown.split("[... ", 1)[0], shown.rsplit("...]\n", 1)[1]
    assert head.endswith("x" * 40 + "\n") and tail.startswith("line ")  # whole lines
    assert all(line.startswith("line ") for line in (head + "\n" + tail).split("\n") if line)
    assert info.original_chars == len(LINES) and info.total_lines == 2000
    assert info.kept_chars == len(head) + len(tail) <= 30_000
    first_missing = head.count("\n") + 1
    assert f"(lines {first_missing}-" in shown and "of 2000)" in shown
    assert 'read_output(id="t1", offset=<line>, limit=<lines>)' in shown


def test_cut_of_one_long_line_says_so() -> None:
    shown, info = cut("y" * 50_000, 10_000, "t2")
    assert "(part of line 1)" in shown and info.total_lines == 1


def test_store_finds_this_run_then_earlier_runs_and_refuses_paths(tmp_path: Path) -> None:
    old = OutputStore(tmp_path / "r1")
    old.save("toolu_old", "from run 1")
    store = OutputStore(tmp_path / "r2", [tmp_path / "r1"])
    store.save("toolu_new", "from run 2")
    assert store.find("toolu_new") == tmp_path / "r2" / "toolu_new.txt"
    assert store.find("toolu_old") == tmp_path / "r1" / "toolu_old.txt"
    assert store.find("../r1/toolu_old") is None and store.find("") is None
    with pytest.raises(ValueError):
        store.save("../escape", "x")


# ---------------------------------------------------------------- the registry


# ---------------------------------------------------------------- the meter


class Counter:
    def __init__(self, n: int | Exception) -> None:
        self.n, self.calls = n, 0

    async def count_tokens(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> int:
        self.calls += 1
        if isinstance(self.n, Exception):
            raise self.n
        return self.n


async def test_meter_is_exact_up_to_the_last_request_and_estimates_the_rest() -> None:
    m = ContextMeter(budget=10_000)
    msgs: list[Message] = [{"role": "user", "content": "go"}]
    first = m.estimate("sys", [], msgs)
    raw = estimate_tokens("sys") + estimate_tokens([]) + estimate_tokens(msgs)
    assert first == math.ceil(raw * FRESH_PRIOR)
    m.observe(Usage(input_tokens=5, cache_read_input_tokens=995, output_tokens=40), len(msgs))
    msgs += [{"role": "assistant", "content": "reply"}, {"role": "user", "content": "x" * 300}]
    tail = estimate_tokens(msgs[2:])
    assert m.estimate("sys", [], msgs) == 1000 + 40 + math.ceil(tail * m.ratio)
    assert (m.peak, m.mean) == (1000, 1000)


async def test_meter_calibrates_on_requests_estimated_from_scratch() -> None:
    """Found in the S6 curves: chars/3 ran 11-21% low on whole requests (tool schemas
    and JSON are dense), and a low estimate near the budget skips the exact count. A
    from-scratch request measures the ratio; later estimates are scaled by it."""
    m = ContextMeter(budget=10_000)
    msgs: list[Message] = [{"role": "user", "content": "w" * 3000}]
    raw = estimate_tokens("s") + estimate_tokens([]) + estimate_tokens(msgs)
    assert m.estimate("s", [], msgs) == math.ceil(raw * FRESH_PRIOR)  # uncalibrated: high
    m.observe(Usage(input_tokens=round(raw * 1.2)), len(msgs))
    assert m.ratio == pytest.approx(1.2, abs=0.01)
    msgs += [{"role": "assistant", "content": "r"}, {"role": "user", "content": "x" * 900}]
    last = round(raw * 1.2)
    assert m.estimate("s", [], msgs) == last + math.ceil(estimate_tokens(msgs[2:]) * m.ratio)
    m.observe(Usage(input_tokens=5_000), len(msgs))  # an incremental request: no change
    assert m.ratio == pytest.approx(1.2, abs=0.01)
    m.restart()  # a compaction: the next request is estimated from scratch again
    assert m.estimate("s", [], msgs[:1]) == math.ceil(raw * m.ratio)
    m.observe(Usage(input_tokens=raw * 5), 1)
    assert m.ratio == RATIO_RANGE[1]  # clamped: one odd request can't blow it up
    m.restart()
    m.estimate("s", [], msgs[:1])
    m.observe(Usage(input_tokens=1), 1)
    assert m.ratio == RATIO_RANGE[0]  # never below chars/3


async def test_meter_counts_exactly_only_near_the_budget() -> None:
    counter = Counter(9_321)
    m = ContextMeter(budget=10_000, counter=counter)
    small: list[Message] = [{"role": "user", "content": "hi"}]
    assert await m.measure("s", [], small) == (m.estimate("s", [], small), "estimate")
    big: list[Message] = [{"role": "user", "content": "w" * 27_000}]  # ~9000 tokens estimated
    assert await m.measure("s", [], big) == (9_321, "count") and counter.calls == 1
    broken = ContextMeter(budget=10_000, counter=Counter(RuntimeError("down")))
    assert await broken.measure("s", [], big) == (broken.estimate("s", [], big), "estimate")


async def test_provider_counts_tokens_over_http() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/messages/count_tokens"
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json={"input_tokens": 4242})

    client = anthropic.AsyncAnthropic(
        api_key="k",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    p = AnthropicProvider(model="claude-opus-5", max_tokens=10, client=client)
    n = await p.count_tokens(system="s", messages=[{"role": "user", "content": "hi"}], tools=[])
    assert n == 4242 and seen[0]["model"] == "claude-opus-5" and seen[0]["system"] == "s"
