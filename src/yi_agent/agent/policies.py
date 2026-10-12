"""Scripted "models" for controls: they drive the real loop, tools and grader without an
API call, so a control differs from the agent only in who reads the tool output.

- policy:metric-top answers with the services of `metric_changes`' rows, in order, and
  the first row's family as the fault. It is BARO's statistic as the tool computes it
  (from Elasticsearch: no zero-filled gaps, ranked by the larger of the up and down
  moves), with no reasoning on top. If it scores like the agent, the agent's gain over
  BARO comes from the tool, not from the investigation.
"""

from __future__ import annotations

from typing import Any

from yi_agent.agent.tools import SUBMIT
from yi_agent.llm.types import LLMResponse, Message, TextCallback, ToolSpec, Usage

POLICY_PREFIX = "policy:"  # a model name with this prefix is free: no API call is made
FAULT_OF_FAMILY = {
    "cpu": "cpu",
    "mem": "mem",
    "diskio": "disk",
    "socket": "socket",
    "latency-50": "delay",
    "latency-90": "delay",
}


def _call(i: str, name: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[{"type": "tool_use", "id": i, "name": name, "input": args}],
        usage=Usage(),
        model=MetricTopPolicy.model,
    )


def _rows(table: str) -> list[tuple[str, float, float]]:
    """(metric, z_up, z_down) for each row of metric_changes' table."""
    out = []
    for line in table.splitlines()[2:]:
        parts = line.split()
        if len(parts) < 7 or line.startswith("..."):
            continue
        try:
            out.append((parts[0], float(parts[5]), float(parts[6])))
        except ValueError:
            continue
    return out


class MetricTopPolicy:
    model = POLICY_PREFIX + "metric-top"

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        on_text: TextCallback | None = None,
    ) -> LLMResponse:
        results = [
            b
            for m in messages
            if m["role"] == "user"
            for b in m["content"]
            if b.get("type") == "tool_result"
        ]
        if not results:
            return _call("p1", "metric_changes", {"limit": 200})
        rows = _rows(str(results[0]["content"])) if not results[0].get("is_error") else []
        services: list[str] = []
        for metric, _, _ in rows:
            svc = metric.split("_")[0]
            if svc not in services:
                services.append(svc)
        first, up, down = rows[0] if rows else ("", 0.0, 0.0)
        family = first.split("_", 1)[1] if "_" in first else ""
        evidence = (
            [{"kind": "metric", "metric": first, "direction": "up" if up >= -down else "down"}]
            if first
            else []
        )
        return _call(
            "p2",
            SUBMIT,
            {
                "root_causes": services[:5] or ["unknown"],
                "fault_type": FAULT_OF_FAMILY.get(family, "delay"),
                "evidence": evidence,
                "summary": "policy: the services of metric_changes' rows, in order",
            },
        )


POLICIES = {MetricTopPolicy.model: MetricTopPolicy}
