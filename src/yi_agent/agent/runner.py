"""Running the agent over a split: cases in, trials.jsonl and scores.json out, the same
files a baseline writes, graded by the same harness.

A run directory holds one condition (model, effort, step budget, prompt and tools): it
is written to run.json first, and a run that finds a different one there stops instead
of mixing conditions. A run that stops early resumes where it left off: graded trials
are kept, infra failures are retried (they were never scores).

Per case it also writes, beside the trials: trajectories.jsonl (steps, tokens, dollars,
time), events/<case>.json (the full message history) and traces/<case>.jsonl (spans).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from elasticsearch import ApiError, Elasticsearch, TransportError

from yi_agent.agent.loop import AgentConfig, AgentResult, InfraFailure, run_agent
from yi_agent.agent.prompt import SYSTEM_SHA
from yi_agent.agent.tools import TOOL_SPECS, CaseIndices, CaseTools, dispatch
from yi_agent.data.alert import make_alert
from yi_agent.data.es_load import LoadError, ensure_loaded
from yi_agent.data.rcaeval import CaseLabel
from yi_agent.data.telemetry import read_window
from yi_agent.eval import run as eval_run
from yi_agent.eval.case_data import CaseData
from yi_agent.eval.grade import score_case
from yi_agent.eval.harness import RESULTS_DIR, check_approval, write_json
from yi_agent.eval.report import Report, Trial, read_trials, write_trials
from yi_agent.llm.types import LLMProvider
from yi_agent.trace.tracer import JsonlSpanWriter, Tracer

LOOP_VERSION = 1  # bump when the loop's behaviour changes: it is part of the condition
# The placebo control: the agent is told the anomaly began 6 minutes before the real
# injection and can see nothing from the injection on (6 minutes on each side of the
# false onset, all of it before the fault). If it still finds the root cause above
# chance, something other than the fault's effect points at it.
PLACEBO_SHIFT_MS = 6 * 60 * 1000
PLACEBO_HALF_MS = 6 * 60 * 1000
TOOLS_SHA = hashlib.sha256(json.dumps(TOOL_SPECS, sort_keys=True).encode()).hexdigest()[:12]

type Progress = Callable[[str], None]


@dataclass(frozen=True)
class Condition:
    model: str
    effort: str | None
    max_steps: int
    placebo: bool = False

    def record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "model": self.model,
            "effort": self.effort,
            "max_steps": self.max_steps,
            "system_sha": SYSTEM_SHA,
            "tools_sha": TOOLS_SHA,
            "loop_version": LOOP_VERSION,
        }
        if self.placebo:  # only when set, so earlier run.json files still match
            rec["placebo"] = {"shift_ms": PLACEBO_SHIFT_MS, "half_window_ms": PLACEBO_HALF_MS}
        return rec


class ConditionChanged(Exception):
    pass


def _check_condition(run_dir: Path, cond: Condition) -> None:
    path = run_dir / "run.json"
    want = cond.record()
    if path.exists():
        have = json.loads(path.read_text())
        if have != want:
            diff = {k: (have.get(k), v) for k, v in want.items() if have.get(k) != v}
            raise ConditionChanged(
                f"{run_dir} holds a run of another condition {diff} (was, now): a variant is "
                "one condition; pass a new --variant"
            )
    else:
        write_json(path, want)


def trajectory(result: AgentResult, error: str | None = None) -> dict[str, Any]:
    return {
        "case_id": result.case_id,
        "outcome": "infra_error" if error else result.outcome,
        "error": error,
        "steps": result.steps,
        "tool_calls": result.tool_calls,
        "tool_errors": result.tool_errors,
        "usage": result.usage.model_dump(),
        "cost_usd": result.cost_usd,
        "peak_request_tokens": result.peak_request_tokens,
        "models": result.models,
        "wall_s": round(result.wall_s, 1),
    }


async def run_split(
    split: str,
    data_dir: Path,
    es: Elasticsearch,
    make_provider: Callable[[], LLMProvider],
    cond: Condition,
    variant: str,
    limit: int | None = None,
    parallel: int = 1,
    progress: Progress | None = None,
    results_dir: Path = RESULTS_DIR,
) -> str:
    """Run (or resume) the agent on a split and return the printed summary. With
    `limit`, only the first cases run (a smoke test); scoring needs the whole split.
    `parallel` cases run at once; each is still one sequential investigation."""
    check_approval(False)  # refuse before spending anything on a run that can't be scored
    say = progress or (lambda _: None)
    labels = eval_run.labels_by_id()
    ids = eval_run.split_ids(split, labels)
    chosen = ids[:limit] if limit else ids
    run_dir = results_dir / variant / split
    _check_condition(run_dir, cond)
    trials_path = run_dir / "trials.jsonl"
    done = {t.case_id: t for t in read_trials(trials_path)} if trials_path.exists() else {}
    done = {k: t for k, t in done.items() if t.status == "ok"}  # retry infra failures
    config = AgentConfig(max_steps=cond.max_steps)
    todo = [(n, i) for n, i in enumerate(chosen, 1) if i not in done]
    gate = asyncio.Semaphore(max(1, parallel))

    async def one(n: int, case_id: str) -> None:
        label = labels[case_id]
        async with gate:
            say(f"[{n}/{len(chosen)}] {case_id} ...")
            trial, traj = await _run_case(
                label, data_dir, es, make_provider, config, run_dir, say, cond.placebo
            )
        # One event loop: these writes never interleave, and each leaves a valid file.
        done[case_id] = trial
        with (run_dir / "trajectories.jsonl").open("a") as fh:
            fh.write(json.dumps(traj) + "\n")
        write_trials([done[i] for i in ids if i in done], trials_path)
        say(_case_line(trial, traj, label, data_dir))

    await asyncio.gather(*(one(n, i) for n, i in todo))
    lines = [_summary(run_dir, chosen)]
    if limit and len(chosen) < len(ids):
        lines.append(f"partial run: {len(chosen)} of {len(ids)} cases; scores need the whole split")
        return "\n".join(lines)
    table, _ = eval_run.score_run(trials_path, split, data_dir, approve=False)
    lines += [table, _versus_baro(run_dir, results_dir / "baro" / split / "scores.json")]
    return "\n".join(line for line in lines if line)


async def _run_case(
    label: CaseLabel,
    data_dir: Path,
    es: Elasticsearch,
    make_provider: Callable[[], LLMProvider],
    config: AgentConfig,
    run_dir: Path,
    say: Progress,
    placebo: bool = False,
) -> tuple[Trial, dict[str, Any]]:
    case_dir = data_dir / label.name
    window = read_window(case_dir)
    alert = make_alert(label, window)  # the case id, the system and the times; no label
    indices = CaseIndices(es, label.id, window.sources)
    tools = CaseTools(indices, window.onset_ms)
    if placebo:
        fake = window.onset_ms - PLACEBO_SHIFT_MS
        alert = alert.model_copy(update={"onset_ms": fake, "end_ms": window.onset_ms})
        indices = CaseIndices(es, label.id, window.sources, ceiling_ms=window.onset_ms)
        tools = CaseTools(indices, fake, half_window_ms=PLACEBO_HALF_MS)
    result = AgentResult(case_id=label.id)
    try:
        try:
            await asyncio.to_thread(ensure_loaded, es, label.id, case_dir, say)
        except (LoadError, ApiError, TransportError) as e:
            raise InfraFailure(f"loading the case failed: {e}") from e
        tracer = Tracer(label.id, JsonlSpanWriter(run_dir / "traces" / f"{label.id}.jsonl"))
        result = await run_agent(make_provider(), alert, partial(dispatch, tools), config, tracer)
    except InfraFailure as e:
        result = e.result or result
        return Trial(case_id=label.id, status="infra_error", error=str(e)), trajectory(
            result, str(e)
        )
    finally:
        write_json(
            run_dir / "events" / f"{label.id}.json",
            {"case_id": label.id, "system_sha": SYSTEM_SHA, "messages": result.messages},
        )
    # No report is the agent's failure and scores nothing; a trial without one would be
    # read as an infra failure and dropped from the scores.
    report = result.report or Report(case_id=label.id, summary=f"no report: {result.outcome}")
    return Trial(case_id=label.id, report=report), trajectory(result)


def _case_line(trial: Trial, traj: dict[str, Any], label: CaseLabel, data_dir: Path) -> str:
    """One case's result for the operator (the agent never sees this)."""
    cost = f"${traj['cost_usd']:.3f}" if traj["cost_usd"] is not None else "$?"
    head = (
        f"  {label.id}: {traj['outcome']}, {traj['steps']} steps, {traj['tool_calls']} tool "
        f"calls, {cost}, {traj['wall_s']:.0f}s"
    )
    if trial.status != "ok":
        return f"{head}: {trial.error}"
    assert trial.report is not None
    s = score_case(trial.report, label, CaseData(data_dir / label.name))
    on = f", evidence on target {s.on_target:.2f}" if s.on_target is not None else ""
    return (
        f"{head}; truth {label.service} {label.fault}; said {trial.report.root_causes[:3]} "
        f"{trial.report.fault_type}; AC@1 {s.ac1:.0f} Avg@5 {s.avg5:.2f}{on}"
    )


def _summary(run_dir: Path, ids: Sequence[str]) -> str:
    path = run_dir / "trajectories.jsonl"
    if not path.exists():
        return ""
    last: dict[str, dict[str, Any]] = {}
    spent: float | None = 0.0  # every attempt, including infra failures that were retried
    for line in path.read_text().splitlines():
        t = json.loads(line)
        last[t["case_id"]] = t
        spent = None if spent is None or t["cost_usd"] is None else spent + t["cost_usd"]
    rows = [last[i] for i in ids if i in last]
    if not rows:
        return ""
    n = len(rows)
    costs = [r["cost_usd"] for r in rows]
    total = None if any(c is None for c in costs) else sum(costs)
    outcomes = Counter(r["outcome"] for r in rows)

    def mean(k: str) -> float:
        return sum(float(r[k]) for r in rows) / n

    cost_s = (
        f"${total / n:.3f} per case (${spent:.2f} spent in all, retries included)"
        if total is not None and spent is not None
        else "$? (unpriced model)"
    )
    return (
        f"trajectories ({n} cases): outcomes {dict(outcomes)}; steps {mean('steps'):.1f}, "
        f"tool calls {mean('tool_calls'):.1f}, tool errors {mean('tool_errors'):.2f} per case; "
        f"{cost_s}; {mean('wall_s'):.0f}s per case; peak request "
        f"{max(r['peak_request_tokens'] for r in rows):,} tokens"
    )


def _versus_baro(run_dir: Path, baro_scores: Path) -> str:
    """The agent beside BARO on the same split, per group, when BARO has been scored."""
    if not baro_scores.exists():
        return f"(no BARO scores at {baro_scores}: run `yi eval baseline baro --split ...`)"
    ours = json.loads((run_dir / "scores.json").read_text())["summary"]
    baro = json.loads(baro_scores.read_text())["summary"]
    out = [f"{'group':<14}{'n':>4}  {'AC@1 agent/BARO':>16}  {'Avg@5 agent/BARO':>17}"]
    for group, r in ours.items():
        b = baro.get(group)
        if not r.get("cases") or not b:
            continue
        out.append(
            f"{group:<14}{r['cases']:>4}  {r['ac1']:>7.2f} / {b['ac1']:<6.2f}  "
            f"{r['avg5']:>8.2f} / {b['avg5']:<6.2f}"
        )
    return "\n".join(out)
