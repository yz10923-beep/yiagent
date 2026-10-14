"""The agent on OpenRCA Bank (S5 step 3): its prompt, alert, report and condition. The
whole run, through `yi agent bank-run` against Elasticsearch, is in
tests/integration/test_bank_es.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.openrca_fixture import make_bank
from tests.unit.test_openrca import _labels
from yi_agent.agent.bank import (
    BankCondition,
    bank_alert,
    bank_system_prompt,
    bank_tool_specs,
    parse_bank_report,
)
from yi_agent.agent.prompt import system_prompt
from yi_agent.agent.tools import SUBMIT
from yi_agent.data import openrca
from yi_agent.data.openrca import BankQuery, local_str


@pytest.fixture
def queries(tmp_path: Path) -> list[BankQuery]:
    return _labels(make_bank(tmp_path))


def test_the_prompt_is_the_same_for_every_query_and_lists_openrca_s_candidates() -> None:
    p = bank_system_prompt()
    assert all(c in p for c in openrca.COMPONENTS) and all(r in p for r in openrca.REASONS)
    assert "UTC+8" in p and "t+0s" in p and "within one minute" in p
    assert p != system_prompt("v2")
    with pytest.raises(ValueError):
        bank_system_prompt("bank-v9")


def test_the_alert_states_the_query_and_never_its_answer(queries: list[BankQuery]) -> None:
    for q in queries:
        text = bank_alert(q, q.start_ms - openrca.BASELINE_MS, ("logs", "traces", "metrics"), ())
        assert local_str(q.start_ms) in text and local_str(q.end_ms) in text
        assert ("one failure" in text) == (len(q.failures) == 1)
        for f in q.failures:
            assert f.component not in text and f.reason not in text
            assert local_str(f.time_ms) not in text
        assert all(c not in text for c in openrca.COMPONENTS)


def test_the_alert_says_when_a_source_has_nothing_in_the_hour(queries: list[BankQuery]) -> None:
    q = queries[0]
    text = bank_alert(q, q.start_ms - openrca.BASELINE_MS, ("logs", "traces", "metrics"), ("logs",))
    assert "None recorded in this hour: logs." in text
    assert "Telemetry from" in text and ": traces, metrics." in text


def test_the_tools_are_the_final_agents_with_the_bank_report() -> None:
    specs = {t["name"]: t for t in bank_tool_specs()}
    assert set(specs) == {
        "overview",
        "metric_changes",
        "metric_series",
        "search_logs",
        "trace_stats",
        "find_spans",
        "trace_tree",
        SUBMIT,
    }
    item = specs[SUBMIT]["input_schema"]["properties"]["failures"]["items"]["properties"]
    assert item["component"]["enum"] == list(openrca.COMPONENTS)
    assert item["reason"]["enum"] == list(openrca.REASONS)
    assert "20-minute" not in specs["metric_series"]["description"]
    assert "errors_only" in specs["search_logs"]["input_schema"]["properties"]


def _one(q: BankQuery, **kw: str) -> dict[str, object]:
    f = {
        "time": local_str(q.start_ms + 120_000),
        "component": "Tomcat01",
        "reason": "high CPU usage",
    }
    return {**f, **kw}


def test_parsing_a_report_checks_only_what_the_alert_told(queries: list[BankQuery]) -> None:
    one = next(q for q in queries if len(q.failures) == 1)
    two = next(q for q in queries if len(q.failures) == 2)
    parse = parse_bank_report(one)
    r = parse(one.id, {"failures": [_one(one)], "evidence": [], "summary": "s"})
    assert r.failures[0].time == local_str(one.start_ms + 120_000)
    iso = local_str(one.start_ms + 60_000).replace(" ", "T")
    assert parse(one.id, {"failures": [_one(one, time=iso)], "evidence": []}).failures[0].time
    for bad, msg in [
        ({"failures": [_one(one), _one(one)]}, "exactly 1 entry"),
        ({"failures": [_one(one, time="14:57")]}, "not YYYY-mm-dd"),
        ({"failures": [_one(one, time=local_str(one.end_ms))]}, "outside the window"),
        ({"failures": [_one(one, component="tomcat01")]}, "component must be"),
        ({"failures": [_one(one, reason="cpu")]}, "reason must be"),
        ({"failures": [_one(one)], "evidence": [{"kind": "log", "container": "x"}]}, "excerpt"),
    ]:
        with pytest.raises(ValueError, match=msg):
            parse(one.id, {"evidence": [], **bad})
    with pytest.raises(ValueError, match="exactly 2 entries"):
        parse_bank_report(two)(two.id, {"failures": [_one(two)], "evidence": []})


def test_the_condition_is_recorded_as_a_bank_run() -> None:
    rec = BankCondition(model="m", effort="high", max_steps=25).record()
    assert rec["benchmark"] == "openrca-bank" and rec["prompt"] == "bank-v1"
    assert len(rec["system_sha"]) == 12 and len(rec["tools_sha"]) == 12
    with pytest.raises(ValueError):
        BankCondition(model="m", effort=None, max_steps=25, prompt="v2")
