"""The agent loop: alert in, tool calls, one report out.

    messages = [alert]
    repeat up to max_steps times:
        ask the model (retrying API failures; giving up is an infra failure)
        no tool call  -> nudge once to submit, then give up (no report)
        tool calls    -> run each; submit_report with a valid report ends the run
        2 steps left  -> tell the model, so it submits instead of running out

The history is the API's own message format, appended to and never edited, so the
cached prefix grows by one exchange per step (each step pays only for its new tail) and
thinking blocks are echoed back exactly as they came.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from yi_agent.agent.prompt import NUDGE, SYSTEM, budget_notice, first_message
from yi_agent.agent.tools import ALL_FAULTS, SUBMIT, TOOL_SPECS, ToolError, ToolInfraError
from yi_agent.context import request_tokens
from yi_agent.data.alert import Alert
from yi_agent.eval.report import Report
from yi_agent.llm.pricing import cost_usd
from yi_agent.llm.retry import RetryPolicy
from yi_agent.llm.types import LLMError, LLMProvider, LLMResponse, Message, ToolCall, Usage
from yi_agent.trace.tracer import Tracer

Outcome = Literal["submitted", "no_report", "budget", "refusal"]
# A telemetry tool: (name, input) -> text. Raises ToolError (the model sees it) or
# ToolInfraError (the run stops as an infra failure).
type RunTool = Callable[[str, dict[str, Any]], str]


class InfraFailure(Exception):
    """The run could not finish for reasons that are not the agent's: the API failed
    past its retries, or Elasticsearch did. Never scored. `result` is the run so far
    (its steps and cost were spent all the same)."""

    result: AgentResult | None = None


@dataclass(frozen=True)
class AgentConfig:
    max_steps: int = 25  # model calls per case
    retry: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass
class AgentResult:
    case_id: str
    outcome: Outcome = "no_report"
    report: Report | None = None
    steps: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = 0.0  # None once any call's model has no known price
    peak_request_tokens: int = 0
    models: list[str] = field(default_factory=list)  # every model that served a call
    wall_s: float = 0.0
    messages: list[Message] = field(default_factory=list)

    def account(self, resp: LLMResponse) -> None:
        self.usage = self.usage + resp.usage
        cost = cost_usd(resp.model, resp.usage.model_dump())
        self.cost_usd = None if cost is None or self.cost_usd is None else self.cost_usd + cost
        self.peak_request_tokens = max(self.peak_request_tokens, request_tokens(resp.usage))
        if resp.model not in self.models:
            self.models.append(resp.model)


def parse_report(case_id: str, args: dict[str, Any]) -> Report:
    """The model's submit_report input as a Report; ValueError says what to fix."""
    if args.get("fault_type") not in ALL_FAULTS:
        raise ValueError(f"fault_type must be one of {list(ALL_FAULTS)}")
    if not args.get("root_causes"):
        raise ValueError("root_causes must name at least one service")
    try:
        return Report.model_validate({**args, "case_id": case_id})
    except ValidationError as e:
        raise ValueError(str(e)) from e


async def run_agent(
    provider: LLMProvider,
    alert: Alert,
    run_tool: RunTool,
    config: AgentConfig | None = None,
    tracer: Tracer | None = None,
) -> AgentResult:
    """Investigate one case. Raises InfraFailure; every other ending is an AgentResult
    (a run without a valid report has report=None and scores nothing)."""
    config = config or AgentConfig()
    tracer = tracer or Tracer.noop()
    result = AgentResult(case_id=alert.case_id)
    t0 = time.monotonic()
    messages: list[Message] = [
        {"role": "user", "content": [{"type": "text", "text": first_message(alert)}]}
    ]
    result.messages = messages
    nudged = False
    with tracer.span("agent.run", "agent", case_id=alert.case_id) as run_span:
        try:
            for step in range(1, config.max_steps + 1):
                resp = await _call_model(provider, messages, config.retry, tracer, step)
                result.steps = step
                result.account(resp)
                messages.append({"role": "assistant", "content": resp.content})
                if resp.stop_reason == "refusal":
                    result.outcome = "refusal"
                    break
                calls = resp.tool_calls
                if not calls:
                    if nudged:
                        result.outcome = "no_report"
                        break
                    nudged = True
                    messages.append({"role": "user", "content": [{"type": "text", "text": NUDGE}]})
                    continue
                blocks = [
                    await _run_call(c, alert.case_id, run_tool, result, tracer) for c in calls
                ]
                if result.report is not None:
                    result.outcome = "submitted"
                    break
                if (left := config.max_steps - step) <= 2:
                    blocks.append({"type": "text", "text": budget_notice(left)})
                messages.append({"role": "user", "content": blocks})
            else:
                result.outcome = "budget"
        except InfraFailure as e:
            e.result = result
            raise
        finally:
            result.wall_s = time.monotonic() - t0
            run_span.set(
                outcome=result.outcome,
                steps=result.steps,
                tool_calls=result.tool_calls,
                cost_usd=result.cost_usd,
                usage=result.usage.model_dump(),
            )
    return result


async def _call_model(
    provider: LLMProvider,
    messages: list[Message],
    policy: RetryPolicy,
    tracer: Tracer,
    step: int,
) -> LLMResponse:
    """One model call. API failures are retried with backoff (a model call has no side
    effects); past the policy's limits they become an InfraFailure."""
    waited, retry = 0.0, 0
    while True:
        with tracer.span("llm.call", "llm", step=step, attempt=retry + 1) as span:
            try:
                resp = await provider.complete(system=SYSTEM, messages=messages, tools=TOOL_SPECS)
            except LLMError as e:
                span.fail(f"{e.kind}: {e}")
                retry += 1
                delay = policy.delay(e, retry)
                if not policy.should_retry(e, retry, waited, delay):
                    raise InfraFailure(f"model call failed ({e.kind}): {e}") from e
            else:
                span.set(
                    model=resp.model,
                    stop_reason=resp.stop_reason,
                    request_tokens=request_tokens(resp.usage),
                    usage=resp.usage.model_dump(),
                    ttft_ms=resp.ttft_ms,
                )
                return resp
        await asyncio.sleep(delay)
        waited += delay


async def _run_call(
    call: ToolCall, case_id: str, run_tool: RunTool, result: AgentResult, tracer: Tracer
) -> dict[str, Any]:
    """One tool call as a tool_result block. A bad call is the model's to fix (it sees
    the error); Elasticsearch failing ends the run as an infra failure."""
    result.tool_calls += 1
    with tracer.span(f"tool.{call.name}", "tool", input=json.dumps(call.input)) as span:
        try:
            if call.name == SUBMIT:
                result.report = parse_report(case_id, call.input)
                text = "Report received. The investigation is over."
            else:
                text = await asyncio.to_thread(run_tool, call.name, call.input)
        except (ToolError, ValueError) as e:
            result.tool_errors += 1
            span.fail(str(e))
            return {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": str(e),
                "is_error": True,
            }
        except ToolInfraError as e:
            raise InfraFailure(str(e)) from e
        span.set(chars=len(text))
    return {"type": "tool_result", "tool_use_id": call.id, "content": text}
