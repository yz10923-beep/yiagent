"""The agent's tools and a whole run against a real Elasticsearch, on fixture cases with
the answer planted where the paper says to look. Run with `make es-test`.

The model is replaced by a scripted investigator that reads the tools' text the way a
model must (top metric row, a log line, a span id) and cites what it read: so a citation
copied from tool output is checked to pass the grader's exists/holds checks, which reads
the source Parquet, not Elasticsearch."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from elasticsearch import Elasticsearch

from tests.fakes import tool_response
from tests.rcaeval_fixture import SECRET, make_case, make_fault_case
from yi_agent.agent import runner
from yi_agent.agent.loop import run_agent
from yi_agent.agent.runner import Condition, ConditionChanged, run_split
from yi_agent.agent.tools import SUBMIT, CaseIndices, CaseTools, ToolError, dispatch
from yi_agent.data.alert import make_alert
from yi_agent.data.es_load import load_case, unload_case
from yi_agent.data.rcaeval import LABELS_PATH, SPLIT_PATH, CaseLabel, parse_case_name, read_labels
from yi_agent.data.split import read_split
from yi_agent.data.telemetry import read_window
from yi_agent.eval import harness
from yi_agent.eval.case_data import CaseData
from yi_agent.eval.grounding import check
from yi_agent.eval.report import LogCitation, MetricCitation, SpanCitation
from yi_agent.llm.types import LLMResponse, Message, TextCallback, ToolSpec

pytestmark = pytest.mark.es
OTHERS = ["alpha", "beta", "gamma"]


@pytest.fixture
def es() -> Iterator[Elasticsearch]:
    client = Elasticsearch(os.environ.get("YI_ES_URL", "http://127.0.0.1:9200"), request_timeout=30)
    if not client.ping():
        pytest.fail("Elasticsearch not reachable; run `make es-up`")
    yield client


def loaded(es: Elasticsearch, tmp: Path, name: str) -> tuple[CaseLabel, CaseTools, Path]:
    label = parse_case_name(name)
    case_dir = make_fault_case(tmp, name, [*OTHERS[:1], label.service, *OTHERS[1:]])
    load_case(es, label.id, case_dir)
    w = read_window(case_dir)
    return label, CaseTools(CaseIndices(es, label.id, w.sources), w.onset_ms), case_dir


# --- reading tool output the way a model must -----------------------------------------


def top_metric(text: str) -> str:
    return text.splitlines()[2].split()[0]  # header lines, then the strongest change


def log_citation(text: str) -> LogCitation | None:
    m = re.search(r"\| (\S+), at_ms=(\d+) \(\S+\) \| (.{10,60})", text)
    if not m:
        return None
    excerpt = m[3].split("\\n")[0]  # the tool escapes newlines; cite within one line
    return LogCitation(container=m[1], at_ms=int(m[2]), excerpt=excerpt)


def span_id(text: str) -> str | None:
    rows = [line for line in text.splitlines() if line.count(" | ") >= 5][1:]
    return rows[0].split(" | ")[0] if rows else None


@pytest.mark.parametrize(
    "name", ["re2ob_cartservice_cpu_1", "re2tt_ts-order-service_mem_1", "re2ss_carts_socket_1"]
)
def test_metric_changes_ranks_the_planted_resource_metric_first(
    es: Elasticsearch, tmp_path: Path, name: str
) -> None:
    label, tools, case_dir = loaded(es, tmp_path, name)
    try:
        text = dispatch(tools, "metric_changes", {"limit": 5})
        metric = top_metric(text)
        fam = {"cpu": "cpu", "mem": "mem", "socket": "socket"}[label.fault]
        assert metric == f"{label.service}_{fam}"
        result = check(MetricCitation(metric=metric, direction="up"), CaseData(case_dir))
        assert result.exists and result.holds
        overview = dispatch(tools, "overview", {})
        assert f"{label.service}: cpu, diskio" in overview
    finally:
        unload_case(es, label.id)


def test_delay_shows_in_trace_stats_and_the_tree_puts_the_time_on_the_root_cause(
    es: Elasticsearch, tmp_path: Path
) -> None:
    label, tools, _ = loaded(es, tmp_path, "re2ob_cartservice_delay_1")
    try:
        stats = dispatch(tools, "trace_stats", {})
        assert stats.splitlines()[2].startswith("cartservice ")  # largest p90 increase
        spans = dispatch(tools, "find_spans", {"limit": 1})
        trace_id = spans.splitlines()[2].split(" | ")[1]
        tree = dispatch(tools, "trace_tree", {"trace_id": trace_id})
        own = {
            line.split()[1]: float(line.rsplit("(", 1)[1].split(")")[0])
            for line in tree.splitlines()[1:]
        }
        assert max(own, key=lambda s: own[s]) == "cartservice"  # callers only wait
    finally:
        unload_case(es, label.id)


def test_code_faults_show_in_logs_and_error_spans_and_both_are_citable(
    es: Elasticsearch, tmp_path: Path
) -> None:
    label, tools, case_dir = loaded(es, tmp_path, "re3ob_cartservice_f1_1")
    data = CaseData(case_dir)
    try:
        assert dispatch(tools, "search_logs", {"query": "exception"}) == "no log lines match"
        text = dispatch(tools, "search_logs", {"contains": "nullpointer"})
        c = log_citation(text)
        assert c is not None and c.container == "cartservice"
        result = check(c, data)
        assert result.exists and result.holds and result.error
        sid = span_id(dispatch(tools, "find_spans", {"errors_only": True}))
        assert sid is not None
        result = check(SpanCitation(span_id=sid), data)
        assert result.exists and result.holds and result.error and result.service == "cartservice"
        # new after the onset: no line in the group was seen before it
        assert re.search(r"^1 \[0\] \| cartservice", text, re.M)
    finally:
        unload_case(es, label.id)


def test_bad_calls_are_tool_errors_and_missing_sources_say_so(
    es: Elasticsearch, tmp_path: Path
) -> None:
    name = "re2ss_carts_cpu_1"
    label = parse_case_name(name)
    case_dir = make_case(tmp_path, name, traces=False)
    load_case(es, label.id, case_dir)
    w = read_window(case_dir)
    tools = CaseTools(CaseIndices(es, label.id, w.sources), w.onset_ms)
    try:
        for tool, args, msg in [
            ("trace_stats", {}, "no traces were collected"),
            ("metric_series", {"metric": "nope_cpu"}, "no metric named"),
            ("metric_changes", {"bogus": 1}, "bad arguments"),
            ("search_logs", {"window": "later"}, "window must be one of"),
            ("drop_index", {}, "unknown tool"),
        ]:
            with pytest.raises(ToolError, match=msg):
                dispatch(tools, tool, args)
    finally:
        unload_case(es, label.id)


@dataclass
class Recorder:
    """An Elasticsearch stand-in that records which client methods the tools touch."""

    es: Elasticsearch
    used: set[str] = field(default_factory=set)

    def __getattr__(self, name: str) -> Any:
        self.used.add(name)
        return getattr(self.es, name)


def test_tools_only_search_and_their_output_leaks_no_name_path_or_secret(
    es: Elasticsearch, tmp_path: Path
) -> None:
    name = "re3tt_ts-auth-service_f2_1"
    label = parse_case_name(name)
    case_dir = make_fault_case(tmp_path, name, ["ts-ui", label.service, "ts-user-service"])
    (case_dir / "root_cause.txt").write_text(SECRET)
    load_case(es, label.id, case_dir)
    w = read_window(case_dir)
    rec = Recorder(es)
    tools = CaseTools(CaseIndices(rec, label.id, w.sources), w.onset_ms)  # type: ignore[arg-type]
    try:
        outputs = [
            dispatch(tools, n, a)
            for n, a in [
                ("overview", {}),
                ("metric_changes", {}),
                ("metric_series", {"metric": f"{label.service}_cpu"}),
                ("search_logs", {"window": "all", "limit": 50}),
                ("trace_stats", {}),
                ("find_spans", {}),
                ("trace_tree", {"trace_id": "t0001"}),
            ]
        ]
        assert rec.used == {"search"}  # read-only: no index, update, delete, bulk
        blob = "\n".join(outputs)
        for bad in (name, "re3", "f2", SECRET, str(tmp_path), "yi-c"):
            assert bad not in blob, bad
    finally:
        unload_case(es, label.id)


# --- a whole run: the loop, the tools, the runner and the grader ------------------------


@dataclass
class Investigator:
    """A scripted 'model': the strongest metric, a log line if an exception is there,
    an error span if any, then a report citing what it read."""

    requests: int = 0

    @property
    def model(self) -> str:
        return "claude-sonnet-5-5"

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        on_text: TextCallback | None = None,
    ) -> LLMResponse:
        self.requests += 1
        step = sum(1 for m in messages if m["role"] == "assistant")
        results = [
            b["content"]
            for m in messages
            if m["role"] == "user"
            for b in m["content"]
            if b.get("type") == "tool_result"
        ]
        if step == 0:
            r = tool_response(
                ("a", "metric_changes", {"limit": 3}),
                ("b", "search_logs", {"contains": "exception"}),
                ("c", "find_spans", {"errors_only": True, "limit": 1}),
            )
            return r.model_copy(update={"model": self.model})
        metric = top_metric(results[0])
        evidence: list[dict[str, Any]] = []
        if c := log_citation(results[1]):
            evidence.append(c.model_dump())
        else:  # a code fault moves no metric: cite the metric only without an exception
            evidence.append({"kind": "metric", "metric": metric, "direction": "up"})
        if results[2] != "no spans match" and (sid := span_id(results[2])):
            evidence.append({"kind": "span", "span_id": sid})
        service = c.container if c else metric.split("_")[0]
        fault = "f1" if c else {"diskio": "disk"}.get(metric.split("_", 1)[1], "cpu")
        args = {
            "root_causes": [service],
            "fault_type": fault,
            "evidence": evidence,
            "summary": "scripted",
        }
        return tool_response(("s", SUBMIT, args)).model_copy(update={"model": self.model})


async def test_a_case_run_end_to_end_cites_only_what_exists(
    es: Elasticsearch, tmp_path: Path
) -> None:
    label, tools, case_dir = loaded(es, tmp_path, "re3ob_cartservice_f5_1")
    try:
        alert = make_alert(label, read_window(case_dir))
        r = await run_agent(Investigator(), alert, partial(dispatch, tools))
        assert r.outcome == "submitted" and r.report is not None
        assert r.report.root_causes == ["cartservice"]
        data = CaseData(case_dir)
        checks = [check(c, data) for c in r.report.evidence]
        assert len(checks) == 2 and all(c.exists and c.holds for c in checks)
        # the model saw no label: not in any request, not in any tool result
        assert label.name not in json.dumps(r.messages)
    finally:
        unload_case(es, label.id)


@pytest.fixture
def dev_small(
    es: Elasticsearch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, list[CaseLabel]]]:
    """Fixture cases under the real dev_small names (the answers planted), and a scratch
    approval of the current harness, as `yi eval` tests do."""
    approval = tmp_path / "harness.sha256"
    approval.write_text(harness.harness_sha() + "\n")
    monkeypatch.setattr(harness, "APPROVAL_FILE", approval)
    by_id = {c.id: c for c in read_labels(LABELS_PATH)}
    labels = [by_id[i] for i in read_split(SPLIT_PATH).dev_small]
    data_dir = tmp_path / "data"
    for k, c in enumerate(labels):
        services = list(OTHERS)
        services.insert(k % 4, c.service)
        make_fault_case(data_dir, c.name, services)
    yield data_dir, labels
    for c in labels:
        unload_case(es, c.id)


async def test_a_split_run_scores_resumes_and_keeps_one_condition(
    es: Elasticsearch, tmp_path: Path, dev_small: tuple[Path, list[CaseLabel]]
) -> None:
    data_dir, labels = dev_small
    made: list[Investigator] = []

    def factory() -> Investigator:
        made.append(Investigator())
        return made[-1]

    cond = Condition(model="claude-sonnet-5-5", effort="high", max_steps=5)
    out_dir = tmp_path / "results"
    run = partial(run_split, "dev_small", data_dir, es, factory, variant="v", results_dir=out_dir)
    partial_out = await run(cond, limit=2)
    assert "partial run: 2 of 24" in partial_out and len(made) == 2
    out = await run(cond, parallel=4)
    assert len(made) == 24  # resumed: the first two were not run again
    assert "AC@1" in out and "no BARO scores" in out
    run_dir = out_dir / "v" / "dev_small"
    scores = json.loads((run_dir / "scores.json").read_text())
    assert len(scores["cases"]) == 24 and not scores["infra_errors"]
    assert scores["summary"]["all"]["exists"] == 1.0 and scores["summary"]["all"]["holds"] == 1.0
    assert scores["harness_sha256"] == harness.harness_sha()
    trajs = (run_dir / "trajectories.jsonl").read_text().splitlines()
    assert len(trajs) == 24 and all(json.loads(t)["cost_usd"] > 0 for t in trajs)
    assert (run_dir / "events" / f"{labels[0].id}.json").exists()
    assert (run_dir / "traces" / f"{labels[0].id}.jsonl").exists()
    assert json.loads((run_dir / "run.json").read_text())["tools_sha"] == runner.TOOLS_SHA
    await run(cond)
    assert len(made) == 24  # all done: nothing runs, the summary is printed again
    with pytest.raises(ConditionChanged, match="max_steps"):
        await run(Condition(model="claude-sonnet-5-5", effort="high", max_steps=9))


def test_real_data_oddities_null_messages_long_lines_spans_without_duration(
    es: Elasticsearch, tmp_path: Path
) -> None:
    """The VM's first Haiku run crashed on a log line with no message (2026-10-12)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from tests.rcaeval_fixture import ONSET_S

    name = "re2ob_cartservice_loss_1"
    label = parse_case_name(name)
    case_dir = make_case(tmp_path, name)
    long_line = "java.lang.IllegalStateException: boom " + "x" * 3000
    pq.write_table(
        pa.table(
            {
                "timestamp": pa.array([ONSET_S + i for i in range(4)], pa.int64()),
                "container_name": ["cartservice", None, "cartservice", "frontend"],
                "message": [None, "orphan line without a container", long_line, ""],
            }
        ),
        case_dir / "logs.parquet",
    )
    traces = pq.read_table(case_dir / "traces.parquet")
    traces = traces.set_column(
        traces.schema.get_field_index("duration"),
        "duration",
        pa.array([None, 900, None], pa.int64()),
    )
    pq.write_table(traces, case_dir / "traces.parquet")
    load_case(es, label.id, case_dir)
    w = read_window(case_dir)
    tools = CaseTools(CaseIndices(es, label.id, w.sources), w.onset_ms)
    try:
        text = dispatch(tools, "search_logs", {"window": "all"})
        assert "1 lines over 2048 chars are not grouped" in text
        c = log_citation(text.split("not grouped")[1])
        assert c is not None and c.excerpt.startswith("java.lang.IllegalStateExc")
        assert check(c, CaseData(case_dir)).exists
        assert "orphan line" in text  # a line without a container is still shown
        assert "trace " in dispatch(tools, "trace_tree", {"trace_id": "0001"})
        assert "slowest" in dispatch(tools, "find_spans", {})
    finally:
        unload_case(es, label.id)


def test_the_placebo_ceiling_hides_everything_from_the_real_onset_on(
    es: Elasticsearch, tmp_path: Path
) -> None:
    label, _, case_dir = loaded(es, tmp_path, "re3ob_cartservice_f1_1")
    w = read_window(case_dir)
    fake = w.onset_ms - runner.PLACEBO_SHIFT_MS
    blind = CaseTools(
        CaseIndices(es, label.id, w.sources, ceiling_ms=w.onset_ms),
        fake,
        half_window_ms=runner.PLACEBO_HALF_MS,
    )
    try:
        logs = dispatch(blind, "search_logs", {"window": "all", "limit": 50})
        stamps = [int(x) for x in re.findall(r"at_ms=(\d+)", logs)]
        assert stamps and max(stamps) < w.onset_ms
        assert "NullPointerException" not in logs  # the fault's own line is after the onset
        assert "no log lines match" == dispatch(blind, "search_logs", {"contains": "nullpointer"})
        assert dispatch(blind, "find_spans", {"errors_only": True}) == "no spans match"
        with pytest.raises(ToolError, match="no trace"):
            dispatch(blind, "trace_tree", {"trace_id": "t000c"})  # starts at the real onset
        assert "(0) / " in dispatch(blind, "overview", {})
        assert "windows are 6 minutes" in dispatch(blind, "overview", {})
    finally:
        unload_case(es, label.id)


async def test_the_metric_top_policy_is_a_free_control_through_the_real_loop(
    es: Elasticsearch, tmp_path: Path
) -> None:
    from yi_agent.agent.policies import MetricTopPolicy

    label, tools, case_dir = loaded(es, tmp_path, "re2tt_ts-order-service_cpu_1")
    try:
        alert = make_alert(label, read_window(case_dir))
        r = await run_agent(MetricTopPolicy(), alert, partial(dispatch, tools))
        assert r.outcome == "submitted" and r.report is not None
        assert r.report.root_causes[0] == "ts-order-service" and r.report.fault_type == "cpu"
        assert r.cost_usd == 0.0 and r.models == ["policy:metric-top"]
        assert check(r.report.evidence[0], CaseData(case_dir)).holds
    finally:
        unload_case(es, label.id)


async def test_compare_puts_variants_side_by_side_from_their_scores(
    es: Elasticsearch, tmp_path: Path, dev_small: tuple[Path, list[CaseLabel]]
) -> None:
    from yi_agent.agent.policies import MetricTopPolicy
    from yi_agent.agent.runner import compare

    data_dir, _ = dev_small
    out_dir = tmp_path / "results"
    cond = Condition(model="policy:metric-top", effort="high", max_steps=5)
    await run_split(
        "dev_small", data_dir, es, MetricTopPolicy, cond, variant="top", results_dir=out_dir
    )
    table = compare("dev_small", ["top", "missing"], "all", results_dir=out_dir)
    row = next(line for line in table.splitlines() if line.startswith("top "))
    assert " 24 " in row and "0.0000" in row and "other harness" not in row
    assert "missing" in table and "no scores.json" in table
