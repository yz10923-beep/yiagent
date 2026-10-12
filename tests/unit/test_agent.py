"""The agent loop (S3) with a scripted model and fake tools: how a run ends, what the
model is sent, how failures are classified, and that nothing case-specific is in the
prompt or the tools."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from tests.fakes import ScriptedProvider, text_response, tool_response
from yi_agent.agent import prompt
from yi_agent.agent.loop import AgentConfig, InfraFailure, parse_report, run_agent
from yi_agent.agent.tools import SUBMIT, TOOL_SPECS, ToolError, ToolInfraError
from yi_agent.data.alert import Alert
from yi_agent.data.rcaeval import LABELS_PATH, read_labels
from yi_agent.eval.report import LogCitation, MetricCitation
from yi_agent.llm.retry import NO_RETRY, RetryPolicy
from yi_agent.llm.types import LLMError, LLMResponse, Usage

ALERT = Alert(
    case_id="c0123456789",
    system="Online Boutique",
    onset_ms=1_705_935_125_000,
    start_ms=1_705_934_405_000,
    end_ms=1_705_935_845_000,
    sources=("logs", "traces", "metrics"),
)
GOOD = {
    "root_causes": ["cartservice", "frontend"],
    "fault_type": "cpu",
    "evidence": [
        {"kind": "metric", "metric": "cartservice_cpu", "direction": "up"},
        {
            "kind": "log",
            "container": "cartservice",
            "at_ms": 1_705_935_130_000,
            "excerpt": "NullPointerException: price",
        },
    ],
    "summary": "cartservice cpu jumped at the onset.",
}


class FakeTools:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    def __call__(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        if self.fail is not None:
            raise self.fail
        return f"result of {name}"


def submit(args: dict[str, Any], i: str = "s1") -> LLMResponse:
    return tool_response((i, SUBMIT, args))


async def test_a_run_investigates_then_submits_a_report() -> None:
    p = ScriptedProvider(
        [
            tool_response(("t1", "overview", {}), ("t2", "metric_changes", {"limit": 5})),
            submit(GOOD),
        ],
        model="claude-sonnet-5-5",
    )
    tools = FakeTools()
    r = await run_agent(p, ALERT, tools)
    assert r.outcome == "submitted" and r.steps == 2 and r.tool_calls == 3
    assert tools.calls == [("overview", {}), ("metric_changes", {"limit": 5})]
    assert r.report is not None and r.report.case_id == ALERT.case_id
    assert r.report.root_causes == ["cartservice", "frontend"] and r.report.fault_type == "cpu"
    assert r.report.evidence[0] == MetricCitation(metric="cartservice_cpu", direction="up")
    assert isinstance(r.report.evidence[1], LogCitation)
    # both tool results go back in one user message, in call order
    second = p.requests[1].messages[-1]["content"]
    assert [b["tool_use_id"] for b in second] == ["t1", "t2"]
    assert second[0]["content"] == "result of overview"
    assert r.usage == Usage(input_tokens=20, output_tokens=10)
    assert r.cost_usd is not None and r.cost_usd > 0
    assert r.models == ["claude-sonnet-5-5"]


async def test_the_history_is_append_only_so_every_request_extends_the_cached_prefix() -> None:
    p = ScriptedProvider(
        [tool_response(("t1", "overview", {})), tool_response(("t2", "overview", {})), submit(GOOD)]
    )
    await run_agent(p, ALERT, FakeTools())
    for a, b in zip(p.requests, p.requests[1:], strict=False):
        assert b.messages[: len(a.messages)] == a.messages
        assert b.system == a.system and b.tools == a.tools
    assert p.requests[0].system == prompt.SYSTEM


async def test_the_alert_is_the_first_message() -> None:
    p = ScriptedProvider([submit(GOOD)])
    await run_agent(p, ALERT, FakeTools())
    first = p.requests[0].messages[0]["content"][0]["text"]
    assert first.startswith(ALERT.render()) and "submit_report" in first


async def test_an_invalid_report_is_sent_back_to_be_fixed() -> None:
    bad = {**GOOD, "fault_type": "slow"}
    short = {**GOOD, "evidence": [{"kind": "log", "container": "x", "at_ms": 1, "excerpt": "e"}]}
    p = ScriptedProvider([submit(bad, "s1"), submit(short, "s2"), submit(GOOD, "s3")])
    r = await run_agent(p, ALERT, FakeTools())
    assert r.outcome == "submitted" and r.tool_errors == 2 and r.steps == 3
    errors = [p.requests[i].messages[-1]["content"][0] for i in (1, 2)]
    assert all(e["is_error"] for e in errors)
    assert "fault_type must be one of" in errors[0]["content"]
    assert "excerpt" in errors[1]["content"]


async def test_a_model_that_stops_talking_is_nudged_once_then_ends_without_a_report() -> None:
    p = ScriptedProvider([text_response("I think it is cartservice."), text_response("Done.")])
    r = await run_agent(p, ALERT, FakeTools())
    assert r.outcome == "no_report" and r.report is None and r.steps == 2
    assert p.requests[1].messages[-1]["content"][0]["text"] == prompt.NUDGE


async def test_a_nudge_that_works_ends_with_the_report() -> None:
    p = ScriptedProvider([text_response("Thinking..."), submit(GOOD)])
    r = await run_agent(p, ALERT, FakeTools())
    assert r.outcome == "submitted"


async def test_the_step_budget_ends_the_run_and_the_model_is_warned_first() -> None:
    p = ScriptedProvider([tool_response((f"t{i}", "overview", {})) for i in range(4)])
    r = await run_agent(p, ALERT, FakeTools(), AgentConfig(max_steps=4))
    assert r.outcome == "budget" and r.steps == 4 and r.report is None
    notices = [
        b["text"]
        for req in p.requests
        for b in req.messages[-1]["content"]
        if b.get("type") == "text" and "tool round" in b.get("text", "")
    ]
    assert notices == [prompt.budget_notice(2), prompt.budget_notice(1)]


async def test_a_bad_tool_call_is_the_models_to_fix() -> None:
    p = ScriptedProvider([tool_response(("t1", "metric_series", {})), submit(GOOD)])
    r = await run_agent(p, ALERT, FakeTools(fail=ToolError("no metric named 'x'")))
    assert r.outcome == "submitted" and r.tool_errors == 1
    block = p.requests[1].messages[-1]["content"][0]
    assert block["is_error"] and "no metric named" in block["content"]


async def test_elasticsearch_failing_is_an_infra_failure_not_a_score() -> None:
    p = ScriptedProvider([tool_response(("t1", "overview", {}))], model="claude-sonnet-5-5")
    with pytest.raises(InfraFailure, match="unreachable") as e:
        await run_agent(p, ALERT, FakeTools(fail=ToolInfraError("Elasticsearch unreachable")))
    assert e.value.result is not None and e.value.result.steps == 1  # what it spent
    assert e.value.result.cost_usd


async def test_api_failures_are_retried_then_become_infra_failures() -> None:
    fast = RetryPolicy(max_retries=2, base_s=0.001, max_delay_s=0.001)
    busy = LLMError("overloaded", retryable=True, kind="overloaded")
    p = ScriptedProvider([busy, busy, submit(GOOD)])
    r = await run_agent(p, ALERT, FakeTools(), AgentConfig(retry=fast))
    assert r.outcome == "submitted" and len(p.requests) == 3
    p = ScriptedProvider([busy, busy, busy])
    with pytest.raises(InfraFailure, match="overloaded"):
        await run_agent(p, ALERT, FakeTools(), AgentConfig(retry=fast))
    bad = LLMError("400 bad", retryable=False, kind="invalid_request")
    p = ScriptedProvider([bad])
    with pytest.raises(InfraFailure, match="invalid_request"):
        await run_agent(p, ALERT, FakeTools(), AgentConfig(retry=NO_RETRY))
    assert len(p.requests) == 1


async def test_a_refusal_ends_the_run_without_a_report() -> None:
    p = ScriptedProvider([text_response("", stop="refusal")])
    r = await run_agent(p, ALERT, FakeTools())
    assert r.outcome == "refusal" and r.report is None


async def test_an_unpriced_model_has_unknown_cost_not_zero() -> None:
    p = ScriptedProvider([submit(GOOD)], model="some-future-model")
    r = await run_agent(p, ALERT, FakeTools())
    assert r.cost_usd is None


def test_parse_report_requires_a_service_and_a_known_fault() -> None:
    with pytest.raises(ValueError, match="root_causes"):
        parse_report("c1", {**GOOD, "root_causes": []})
    with pytest.raises(ValueError, match="fault_type"):
        parse_report("c1", {**GOOD, "fault_type": None})
    assert parse_report("c1", {**GOOD, "case_id": "c999"}).case_id == "c1"  # never the model's


def test_the_prompt_and_tools_name_no_service_and_no_case() -> None:
    """The same text goes to every case; a service name in it would point at an answer.
    Fault names are the answer vocabulary, so they are allowed."""
    labels = read_labels(LABELS_PATH)
    blob = prompt.SYSTEM + json.dumps(TOOL_SPECS) + prompt.NUDGE + prompt.budget_notice(2)
    found = {
        token
        for c in labels
        for token in (c.service, c.name, c.id)
        if re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", blob, re.I)
    }
    assert found == set()
    assert "re2" not in blob.lower() and "re3" not in blob.lower()  # not even the suite
