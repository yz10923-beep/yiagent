"""The one-call baseline (S4 addendum): a fixed summary of the case, one model call, the
same report and grader as the agent. No Elasticsearch: it reads the case's files."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import ScriptedProvider, text_response, tool_response
from tests.rcaeval_fixture import SECRET, make_fault_case
from yi_agent.agent import runner, summary
from yi_agent.agent.loop import AgentConfig, run_agent
from yi_agent.agent.prompt import one_call_prompt
from yi_agent.agent.runner import Condition
from yi_agent.agent.summary import build_summary
from yi_agent.agent.tools import SUBMIT, TOOLSETS
from yi_agent.data.alert import make_alert
from yi_agent.data.rcaeval import LABELS_PATH, SPLIT_PATH, parse_case_name, read_labels
from yi_agent.data.split import read_split
from yi_agent.data.telemetry import read_window
from yi_agent.eval import harness
from yi_agent.eval.case_data import CaseData
from yi_agent.eval.grounding import check
from yi_agent.eval.report import LogCitation, MetricCitation, SpanCitation
from yi_agent.llm.types import LLMResponse, Message, TextCallback, ToolSpec

OTHERS = ["frontend", "checkoutservice", "cartservice"]


def _case(tmp_path: Path, name: str) -> Path:
    rc = parse_case_name(name).service
    return make_fault_case(tmp_path, name, [*OTHERS[:2], rc, OTHERS[2]])


@pytest.mark.parametrize("fault", ["cpu", "mem", "disk", "socket", "delay"])
def test_the_summary_ranks_the_planted_root_cause_first(tmp_path: Path, fault: str) -> None:
    text = build_summary(_case(tmp_path, f"re2ob_paymentservice_{fault}_1"))
    first = re.search(r"Services ranked by their largest rise.*\n- ([\w-]+):", text)
    assert first and first[1] == "paymentservice"


def test_a_code_fault_shows_its_error_line_and_error_spans(tmp_path: Path) -> None:
    text = build_summary(_case(tmp_path, "re3ob_paymentservice_f5_1"))
    assert "container=paymentservice" in text and "NullPointerException" in text
    spans = text.split("## Traces", 1)[1]
    assert re.search(r"- paymentservice \S+: \d+ -> \d+; errors 0 -> [1-9]", spans)
    assert re.search(r"Error spans per service.*\n- paymentservice: 0 -> [1-9]", spans)


def test_a_delay_shows_in_the_delayed_service_s_own_operation_first(tmp_path: Path) -> None:
    """Per (service, operation), largest p90 ratio first, as the agent's trace_stats: the
    delayed service's own operation leads; its callers grow by less, relatively."""
    text = build_summary(_case(tmp_path, "re2ob_paymentservice_delay_1"))
    first = re.search(r"Per service and operation.*\n- (\S+) (\S+):", text)
    assert first and first[1] == "paymentservice"


def test_what_the_summary_shows_can_be_cited_and_is_grounded(tmp_path: Path) -> None:
    """The model may only cite what it was shown: the summary prints each citable thing
    exactly as the grader looks it up."""
    d = _case(tmp_path, "re3ob_paymentservice_f2_1")
    text = build_summary(d)
    m = re.search(r"container=(\S+) at_ms=(\d+) line='([^']+)'", text)
    s = re.search(r"span_id=(\S+)", text)
    metric = re.search(r"Largest rises.*\n- (\S+) \+", text)
    assert m and s and metric
    data = CaseData(d)
    for cite in (
        LogCitation(container=m[1], at_ms=int(m[2]), excerpt=m[3][:40]),
        SpanCitation(span_id=s[1]),
        MetricCitation(metric=metric[1], direction="up"),
    ):
        assert check(cite, data).exists, cite


def test_no_label_reaches_the_summary(tmp_path: Path) -> None:
    name = "re3ob_paymentservice_f2_1"
    d = _case(tmp_path, name)
    (d / "root_cause.txt").write_text(SECRET + "\n")
    text = build_summary(d)
    assert name not in text and str(tmp_path) not in text and SECRET not in text
    assert "f2" not in text and "root_cause" not in text


def test_the_summary_is_cut_at_its_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summary, "MAX_CHARS", 500)
    text = build_summary(_case(tmp_path, "re2ob_paymentservice_cpu_1"))
    assert len(text) < 600 and text.endswith("[summary cut at its size limit]")


def test_the_one_call_condition_is_recorded_and_the_agent_s_is_not_changed() -> None:
    agent = Condition(model="m", effort="high", max_steps=25, prompt="v2", tools="v2")
    one = Condition(
        model="m", effort="high", max_steps=25, prompt="v2", tools="v2", mode="one-call"
    )
    assert "mode" not in agent.record() and agent.config().max_steps == 25
    rec = one.record()
    assert rec["mode"] == "one-call" and rec["max_steps"] == 1
    assert rec["summary_version"] == summary.SUMMARY_VERSION
    assert rec["system_sha"] != agent.record()["system_sha"]
    assert [t["name"] for t in one.config().tools] == [SUBMIT]
    assert one.config().system == one_call_prompt("v2")
    assert "tool calls" not in one_call_prompt("v2")
    with pytest.raises(ValueError, match="placebo"):
        Condition(model="m", effort=None, max_steps=25, placebo=True, mode="one-call")


def _alert_and_config(tmp_path: Path) -> tuple[Any, AgentConfig]:
    name = "re2ob_paymentservice_cpu_1"
    d = _case(tmp_path, name)
    alert = make_alert(parse_case_name(name), read_window(d))
    one = Condition(model="m", effort=None, max_steps=25, prompt="v2", mode="one-call")
    return alert, one.config()


async def test_one_call_is_exactly_one_model_call(tmp_path: Path) -> None:
    alert, config = _alert_and_config(tmp_path)
    report = {"root_causes": ["paymentservice"], "fault_type": "cpu", "evidence": []}
    p = ScriptedProvider([tool_response(("t1", SUBMIT, report))])
    r = await run_agent(p, alert, lambda n, a: "", config, opening="SUMMARY")
    assert r.outcome == "submitted" and r.steps == 1 and len(p.requests) == 1
    assert p.requests[0].messages[0]["content"][0]["text"] == "SUMMARY"
    silent = ScriptedProvider([text_response("I think it is cpu."), text_response("x")])
    r = await run_agent(silent, alert, lambda n, a: "", config, opening="SUMMARY")
    assert r.outcome == "budget" and r.report is None and len(silent.requests) == 1  # no nudge


class Reader:
    """A stand-in model that reads the summary it is sent and answers with its top
    service and top metric: a scripted reply that depends on the case, through the CLI."""

    def __init__(self, **_: Any) -> None:
        self.model = "fake-reader"

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        on_text: TextCallback | None = None,
    ) -> LLMResponse:
        assert [t["name"] for t in tools] == [SUBMIT] and len(messages) == 1
        text = messages[0]["content"][0]["text"]
        svc = re.search(r"Services ranked by their largest rise.*\n- ([\w-]+):", text)
        metric = re.search(r"Largest rises.*\n- (\S+) \+", text)
        assert svc and metric
        report = {
            "root_causes": [svc[1]],
            "fault_type": "cpu",
            "evidence": [{"kind": "metric", "metric": metric[1], "direction": "up"}],
        }
        resp = tool_response(("t1", SUBMIT, report))
        return resp.model_copy(update={"model": self.model})


def test_the_one_call_run_goes_end_to_end_through_the_cli_without_elasticsearch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from yi_agent import cli

    approval = tmp_path / "harness.sha256"
    approval.write_text(harness.harness_sha() + "\n")
    monkeypatch.setattr(harness, "APPROVAL_FILE", approval)
    by_id = {c.id: c for c in read_labels(LABELS_PATH)}
    data_dir = tmp_path / "data"
    for k, i in enumerate(read_split(SPLIT_PATH).dev_small):
        services = list(OTHERS)
        services.insert(k % 4, by_id[i].service)
        make_fault_case(data_dir, by_id[i].name, services)
    monkeypatch.setenv("YI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("YI_ES_URL", "http://127.0.0.1:9")  # nothing listens: none is needed
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(cli, "AnthropicProvider", Reader)
    argv = ["agent", "run", "--split", "dev_small", "--mode", "one-call", "--prompt", "v2"]
    assert cli.main([*argv, "--variant", "one-call", "--limit", "2"]) == 0
    assert "partial run: 2 of 24" in capsys.readouterr().out
    assert cli.main([*argv, "--variant", "one-call"]) == 0  # resumes, then scores
    out = capsys.readouterr().out
    assert "AC@1" in out and "steps 1.0, tool calls 1.0" in out
    row = next(line for line in out.splitlines() if line.startswith("all "))
    assert row.split()[1] == "24"
    # RE2 faults are planted in a metric, so a top-metric reader finds every one; the
    # fixture's code faults move no metric (they show in logs and spans instead)
    re2 = [line for line in out.splitlines() if re.match(r"re2 (cpu|mem|disk|socket) ", line)]
    assert re2 and all(line.split()[3] == "1.00" for line in re2)
    run_dir = tmp_path / "results" / "one-call" / "dev_small"
    run = json.loads((run_dir / "run.json").read_text())
    assert run["mode"] == "one-call" and run["max_steps"] == 1
    events = json.loads(next((run_dir / "events").iterdir()).read_text())
    assert "Telemetry summary:" in events["messages"][0]["content"][0]["text"]
    # a different condition in the same directory is refused, as for the agent
    assert cli.main([*argv[:-2], "--variant", "one-call"]) == 2
    assert TOOLSETS  # the agent's tool sets are untouched by the one-call mode
