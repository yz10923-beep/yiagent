"""The agent on OpenRCA Bank (S5 step 3): the same loop and read-only tools as on
RCAEval, a Bank prompt, a Bank alert, and a report with one entry per failure.

A Bank query gives no onset: it gives a 30-minute window, how many failures it holds
and what to find. The tools take the window's start as their "onset", so "before" is
the 30-minute baseline the cut kept and "after" is the window itself, and every time
they print as t+Ns is seconds from the window start. The occurrence time is part of the
answer, so the agent pins it from the telemetry.

Nothing case-specific enters the system prompt (the candidate components and reasons
are OpenRCA's, the same for every query, as its own agent is given them); the alert
carries only what the query itself says: the window, the number of failures, what is
asked.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from elasticsearch import ApiError, Elasticsearch, TransportError
from pydantic import ValidationError

from yi_agent.agent.loop import AgentConfig, AgentResult, InfraFailure, run_agent
from yi_agent.agent.runner import (
    LOOP_VERSION,
    _check_condition,
    _summary,
    trajectory,
)
from yi_agent.agent.tools import SUBMIT, TOOLSETS, CaseIndices, CaseTools, dispatch
from yi_agent.data import openrca
from yi_agent.data.alert import Alert
from yi_agent.data.es_load import LoadError, ensure_loaded, unload_case
from yi_agent.data.openrca import BankQuery, local_ms, local_str
from yi_agent.data.telemetry import KINDS, Kind
from yi_agent.eval import bank
from yi_agent.eval.bank import BankFailure, BankReport, BankTrial
from yi_agent.eval.harness import check_approval, write_json
from yi_agent.eval.report import Report
from yi_agent.llm.types import LLMProvider
from yi_agent.trace.tracer import JsonlSpanWriter, Tracer

PROMPTS = ("bank-v1",)
# The placebo (as on RCAEval, S3): the agent sees only the baseline, told that its last 15
# minutes are the window, and nothing from the real window's start on. It is graded
# against the real answers: well above chance means something other than the failure's
# effect points at the answer (priors over components and reasons, or a leak).
PLACEBO_WINDOW_MS = 15 * 60 * 1000
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

REASON_GUIDE = """\
How each reason shows (KPI names carry the resource: OSLinux-CPU..., OSLinux-MEM...,
OSLinux-DISK..., JVM-Memory..., JVM-Operating System...; look them up with overview and
metric_changes rather than guessing):
- high CPU usage: the component's operating-system CPU KPIs rise (for example
  OSLinux-CPU_CPU_CPUCpuUtil), and stay up.
- high JVM CPU load: the JVM's own CPU KPI rises (JVM-Operating System ... JVM_CPULoad),
  more clearly than the operating system's CPU.
- high memory usage: the operating system's memory-use KPIs rise, without the JVM heap
  being exhausted.
- JVM Out of Memory (OOM) Heap: the JVM heap used climbs to its maximum
  (JVM-Memory ... HeapMemoryUsed against HeapMemoryMax); garbage-collection log lines
  (log_name gc, "Full GC") multiply, and the JVM may restart.
- high disk I/O read usage: disk read KPIs rise (reads per second, read bytes, busy time).
- high disk space usage: file-system used-space KPIs rise.
- network latency: requests through the component get slower across the board: its
  spans' durations rise (median and tail), and callers wait for it.
- network packet loss: retransmissions make some requests through the component much
  slower or fail while most are unaffected: its tail latency rises far more than its
  median, and request counts or success rates may drop."""

_TEMPLATE = """\
You are an on-call site reliability engineer investigating failures in a banking
microservice platform, using only read-only telemetry tools over one hour: a 30-minute
baseline, then the 30-minute window in which the failures occurred.

For each failure, find:
- the root cause occurrence time: the minute the fault began, when the root cause's
  telemetry first departs from normal (not when symptoms peak or spread), written
  "YYYY-mm-dd HH:MM:SS" in UTC+8; it is graded within one minute;
- the root cause component, one of: {components};
- the root cause reason, one of: {reasons}.

The components are hosts (cmdb_id) in the data: metric names start with them
("Tomcat01_OSLinux-CPU_CPU_CPUCpuUtil"), and spans' service and logs' container are
them. Other names in the data (docker hosts, ServiceTest application KPIs) can show
symptoms but are never the answer.

In the tools the "onset" (t+0s) is the window's start: "before" is the baseline and
"after" is the window; t+Ns is seconds from the window's start, so t+480s is 8 minutes
into it. metric_series shows a KPI minute by minute: use it to find when a change began.
Every service near the root cause can look unhealthy, and a host can raise its
neighbours' metrics: separate the cause from its symptoms, and check your leading
hypothesis against a second source (metrics, logs, traces).

{guide}

Finish by calling submit_report exactly once, with one entry per failure (as many as the
alert says), each with time, component and reason, whatever the query asks for. You
have a limited number of tool calls; spend them on the questions that separate your
hypotheses."""


def bank_system_prompt(version: str = "bank-v1") -> str:
    if version not in PROMPTS:
        raise ValueError(f"unknown Bank prompt {version!r}: {PROMPTS}")
    return _TEMPLATE.format(
        components=", ".join(openrca.COMPONENTS),
        reasons="; ".join(openrca.REASONS),
        guide=REASON_GUIDE,
    )


def bank_system_sha(version: str = "bank-v1") -> str:
    return hashlib.sha256(bank_system_prompt(version).encode()).hexdigest()[:12]


_ASKED = {
    "time": "the root cause occurrence time",
    "component": "the root cause component",
    "reason": "the root cause reason",
}


def bank_alert(
    q: BankQuery, baseline_start_ms: int, sources: tuple[Kind, ...], empty: tuple[Kind, ...]
) -> str:
    """What the query states (window, number of failures, what it asks), the telemetry
    on hand, and how the tools' times relate to the window. Never the answer."""
    n = len(q.failures)
    asked = ", ".join(_ASKED[a] for a in q.asked)
    lines = [
        f"Incident {q.id} in the banking platform.",
        f"Between {local_str(q.start_ms)} and {local_str(q.end_ms)} (UTC+8), "
        + ("one failure occurred." if n == 1 else f"{n} failures occurred."),
        f"The query asks for {asked}; give time, component and reason for each failure anyway.",
        f"Telemetry from {local_str(baseline_start_ms)} to {local_str(q.end_ms)} (UTC+8): "
        f"{', '.join(k for k in sources if k not in empty)}.",
    ]
    if empty:
        lines.append(f"None recorded in this hour: {', '.join(empty)}.")
    lines.append(f"In the tools, t+0s (the onset) is {local_str(q.start_ms)}, the window's start.")
    return "\n".join(lines) + "\n\nInvestigate, then call submit_report."


def _bank_submit() -> dict[str, Any]:
    rcaeval = next(t for t in TOOLSETS["v2"] if t["name"] == SUBMIT)
    evidence = copy.deepcopy(rcaeval["input_schema"]["properties"]["evidence"])
    return {
        "name": SUBMIT,
        "description": "Your final answer; call it once, when the investigation is done. "
        "It ends the investigation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "failures": {
                    "type": "array",
                    "description": "One entry per failure, in time order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "time": {
                                "type": "string",
                                "description": "YYYY-mm-dd HH:MM:SS, UTC+8: when it began",
                            },
                            "component": {"type": "string", "enum": list(openrca.COMPONENTS)},
                            "reason": {"type": "string", "enum": list(openrca.REASONS)},
                        },
                        "required": ["time", "component", "reason"],
                        "additionalProperties": False,
                    },
                },
                "evidence": evidence,
                "summary": {"type": "string", "description": "Two or three sentences: why."},
            },
            "required": ["failures", "evidence", "summary"],
            "additionalProperties": False,
        },
    }


def bank_tool_specs() -> list[dict[str, Any]]:
    """Tools v2 (the S4 final agent's), with the Bank submit_report."""
    specs = [copy.deepcopy(t) for t in TOOLSETS["v2"] if t["name"] != SUBMIT]
    for t in specs:
        t["description"] = t["description"].replace(
            "over the 20-minute window", "over the hour (baseline and window)"
        )
    return [*specs, _bank_submit()]


def parse_bank_report(q: BankQuery) -> Callable[[str, dict[str, Any]], BankReport]:
    """submit_report's input as a BankReport. The checks use only what the alert told
    the agent (the window, the number of failures), so a refusal teaches it nothing
    about the answer."""
    n = len(q.failures)

    def parse(case_id: str, args: dict[str, Any]) -> BankReport:
        raw = args.get("failures") or []
        if len(raw) != n:
            raise ValueError(f"give exactly {n} entr{'y' if n == 1 else 'ies'} in failures")
        out = []
        for f in raw:
            t = str(f.get("time", "")).strip().replace("T", " ")
            try:
                when = datetime.strptime(t[:19], TIME_FORMAT)
            except ValueError as e:
                raise ValueError(f"time {t!r} is not YYYY-mm-dd HH:MM:SS") from e
            ms = local_ms(when.strftime(TIME_FORMAT))
            if not q.start_ms <= ms < q.end_ms:
                raise ValueError(
                    f"time {t} is outside the window {local_str(q.start_ms)} to "
                    f"{local_str(q.end_ms)}"
                )
            if f.get("component") not in openrca.COMPONENTS:
                raise ValueError(f"component must be one of {list(openrca.COMPONENTS)}")
            if f.get("reason") not in openrca.REASONS:
                raise ValueError(f"reason must be one of {list(openrca.REASONS)}")
            out.append(
                BankFailure(time=local_str(ms), component=f["component"], reason=f["reason"])
            )
        try:  # the evidence is typed as on RCAEval (log line, span, metric)
            return BankReport.model_validate(
                {
                    "case_id": case_id,
                    "failures": [f.model_dump() for f in out],
                    "evidence": args.get("evidence", []),
                    "summary": str(args.get("summary", "")),
                }
            )
        except ValidationError as e:
            raise ValueError(str(e)) from e

    return parse


@dataclass(frozen=True)
class BankCondition:
    model: str
    effort: str | None
    max_steps: int
    prompt: str = "bank-v1"
    placebo: bool = False

    def __post_init__(self) -> None:
        if self.prompt not in PROMPTS:
            raise ValueError(f"unknown Bank prompt {self.prompt!r}")

    def config(self, q: BankQuery) -> AgentConfig:
        return AgentConfig(
            max_steps=self.max_steps,
            system=bank_system_prompt(self.prompt),
            tools=bank_tool_specs(),
            parse=parse_bank_report(q),
        )

    def record(self) -> dict[str, Any]:
        tools = json.dumps(bank_tool_specs(), sort_keys=True).encode()
        return {
            "benchmark": "openrca-bank",
            "model": self.model,
            "effort": self.effort,
            "max_steps": self.max_steps,
            "prompt": self.prompt,
            "system_sha": bank_system_sha(self.prompt),
            "tools_sha": hashlib.sha256(tools).hexdigest()[:12],
            "loop_version": LOOP_VERSION,
        } | ({"placebo": {"window_ms": PLACEBO_WINDOW_MS}} if self.placebo else {})


def shown(q: BankQuery, placebo: bool) -> BankQuery:
    """The query as the agent is told it: the real one, or under the placebo a window that
    ends where the real one starts (the failures and asked fields unchanged)."""
    if not placebo:
        return q
    return q.model_copy(update={"start_ms": q.start_ms - PLACEBO_WINDOW_MS, "end_ms": q.start_ms})


def case_tools(
    es: Elasticsearch, q: BankQuery, sources: tuple[Kind, ...], placebo: bool
) -> CaseTools:
    """The tools for one query: the window's start is their onset and the baseline their
    'before'; under the placebo nothing at or after the real window's start is visible."""
    told = shown(q, placebo)
    half = PLACEBO_WINDOW_MS if placebo else openrca.BASELINE_MS
    ceiling = q.start_ms if placebo else None
    return CaseTools(
        CaseIndices(es, q.id, sources, ceiling_ms=ceiling),
        told.start_ms,
        half_window_ms=half,
        error_counts=True,
    )


type Progress = Callable[[str], None]


def _case_sources(case_dir: Path) -> tuple[tuple[Kind, ...], tuple[Kind, ...]]:
    """(sources present, sources present but empty in this hour)."""
    present = tuple(k for k in KINDS if (case_dir / f"{k}.parquet").is_file())
    empty = tuple(
        k for k in present if pq.ParquetFile(case_dir / f"{k}.parquet").metadata.num_rows == 0
    )
    return present, empty


async def run_bank_split(
    split: str,
    data_dir: Path,
    es: Elasticsearch,
    make_provider: Callable[[], LLMProvider],
    cond: BankCondition,
    variant: str,
    limit: int | None = None,
    parallel: int = 1,
    progress: Progress | None = None,
    unload_after: bool = False,
) -> str:
    """Run (or resume) the agent on Bank queries; results beside the download (never in
    the repo), graded by OpenRCA's own evaluate(). Same rules as RCAEval's run_split:
    one condition per directory, infra failures retried, never scored."""
    check_approval(False)
    say = progress or (lambda _: None)
    queries = {q.id: q for q in openrca.read_labels(openrca.labels_path(data_dir))}
    ids = bank.split_ids(split)
    chosen = ids[:limit] if limit else ids
    run_dir = bank.results_dir(data_dir) / variant / split
    _check_condition(run_dir, cond)
    trials_path = run_dir / "trials.jsonl"
    done = {t.case_id: t for t in bank.read_trials(trials_path)} if trials_path.exists() else {}
    done = {k: t for k, t in done.items() if t.status == "ok"}
    todo = [(n, i) for n, i in enumerate(chosen, 1) if i not in done]
    gate = asyncio.Semaphore(max(1, parallel))

    async def one(n: int, case_id: str) -> None:
        q = queries[case_id]
        async with gate:
            say(f"[{n}/{len(chosen)}] {case_id} ...")
            trial, traj = await _run_query(q, data_dir, es, make_provider, cond, run_dir, say)
            if unload_after:
                try:
                    await asyncio.to_thread(unload_case, es, case_id)
                except (ApiError, TransportError) as e:
                    say(f"  {case_id}: could not unload ({e}); run `yi data unload {case_id}`")
        done[case_id] = trial
        with (run_dir / "trajectories.jsonl").open("a") as fh:
            fh.write(json.dumps(traj) + "\n")
        bank.write_trials([done[i] for i in ids if i in done], trials_path)
        say(_line(trial, traj, q))

    await asyncio.gather(*(one(n, i) for n, i in todo))
    lines = [_summary(run_dir, chosen)]
    if limit and len(chosen) < len(ids):
        lines.append(
            f"partial run: {len(chosen)} of {len(ids)} queries; scores need the whole split"
        )
        return "\n".join(lines)
    lines.append(bank.score_run(trials_path, split, data_dir, approve=False))
    archived = bank.results_dir(data_dir) / bank.ARCHIVE_VARIANT / split / "scores.json"
    if archived.exists():
        a = json.loads(archived.read_text())["summary"]["all"]
        lines.append(
            f"Microsoft's archived RCA-agent on the same queries: strict {a['strict']:.2f}, "
            f"mean {a['mean']:.2f}"
        )
    return "\n".join(lines)


async def _run_query(
    q: BankQuery,
    data_dir: Path,
    es: Elasticsearch,
    make_provider: Callable[[], LLMProvider],
    cond: BankCondition,
    run_dir: Path,
    say: Progress,
) -> tuple[BankTrial, dict[str, Any]]:
    case_dir = openrca.cases_dir(data_dir) / q.id
    window = openrca.read_case_window(case_dir)
    sources, empty = _case_sources(case_dir)
    told = shown(q, cond.placebo)  # the agent's view; grading uses q, the real query
    alert = Alert(
        case_id=q.id,
        system="Bank",
        onset_ms=told.start_ms,
        start_ms=window["baseline_start_ms"],
        end_ms=told.end_ms,
        sources=sources,
    )
    opening = bank_alert(told, window["baseline_start_ms"], sources, empty)
    tools = case_tools(es, q, sources, cond.placebo)
    result = AgentResult(case_id=q.id)
    config = cond.config(told)
    try:
        try:
            await asyncio.to_thread(ensure_loaded, es, q.id, case_dir, say)
        except (LoadError, ApiError, TransportError) as e:
            raise InfraFailure(f"loading the case failed: {e}") from e
        tracer = Tracer(q.id, JsonlSpanWriter(run_dir / "traces" / f"{q.id}.jsonl"))
        result = await run_agent(
            make_provider(), alert, partial(dispatch, tools), config, tracer, opening=opening
        )
    except InfraFailure as e:
        result = e.result or result
        return BankTrial(case_id=q.id, status="infra_error", error=str(e)), trajectory(
            result, str(e)
        )
    finally:
        write_json(
            run_dir / "events" / f"{q.id}.json",
            {
                "case_id": q.id,
                "system_sha": bank_system_sha(cond.prompt),
                "messages": result.messages,
            },
        )
    report = result.report or BankReport(case_id=q.id, summary=f"no report: {result.outcome}")
    assert not isinstance(report, Report)  # this runner's config parses Bank reports
    return BankTrial(case_id=q.id, report=report), trajectory(result)


def _line(trial: BankTrial, traj: dict[str, Any], q: BankQuery) -> str:
    """One query's result for the operator (the agent never sees this)."""
    cost = f"${traj['cost_usd']:.3f}" if traj["cost_usd"] is not None else "$?"
    head = f"  {q.id}: {traj['outcome']}, {traj['steps']} steps, {cost}, {traj['wall_s']:.0f}s"
    if trial.status != "ok":
        return f"{head}: {trial.error}"
    s = bank.grade_query(trial.report, q)
    truth = [(f.component, f.reason, local_str(f.time_ms)[11:16]) for f in q.failures]
    said = [
        (f.component, f.reason, f.time[11:16])
        for f in (trial.report.failures if trial.report else [])
    ]
    asks = "/".join(q.asked)
    return f"{head}; {q.task} asks {asks}; truth {truth}; said {said}; score {s.score:.2f}"
