"""Non-LLM reports for the same cases, in the same format the agent returns.

- oracle, empty, wrong: the grader's selftest anchors (1.0, 0, 0). Oracle and wrong read
  the labels, so they test the graders, never the task.
- random: a uniformly random ranking of the candidate services and a random fault; the
  floor every method must clear.
- baro: RCAEval's BARO (eval/baro.py), with a fault guessed from its top metric's family
  (cpu, mem, diskio, socket, latency -> delay) so the fault score has a non-LLM floor too.
- shape-columns, shape-time: leak detectors, not baselines. They predict a case's root
  cause from *other injections'* labels, by the most similar metric column set or the
  nearest injection time. Scoring above chance means the data's shape or its recording
  order carries the label, which matters to anything that remembers across cases.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable, Sequence
from pathlib import Path

import pyarrow.parquet as pq

from yi_agent.data.rcaeval import FAULTS, CaseLabel
from yi_agent.data.telemetry import read_window
from yi_agent.eval import baro
from yi_agent.eval.case_data import CaseData
from yi_agent.eval.grounding import oracle_evidence
from yi_agent.eval.report import MetricCitation, Report, Trial
from yi_agent.eval.scoring import TOP_K, normalize_service

FAULT_OF_METRIC_FAMILY = {
    "cpu": "cpu",
    "mem": "mem",
    "diskio": "disk",
    "socket": "socket",
    "latency": "delay",
    "latency-90": "delay",
    "latency-50": "delay",
}

type Baseline = Callable[[CaseLabel, CaseData], Report]


def _rng(case_id: str, seed: int) -> random.Random:
    # Not hash(): Python salts str hashes per process, and runs must be reproducible.
    return random.Random(int(hashlib.sha256(f"{seed}:{case_id}".encode()).hexdigest()[:16], 16))


def oracle(label: CaseLabel, data: CaseData) -> Report:
    return Report(
        case_id=label.id,
        root_causes=[label.service],
        fault_type=label.fault,
        evidence=oracle_evidence(data, label.service, label.fault),
    )


def empty(label: CaseLabel, data: CaseData) -> Report:
    return Report(case_id=label.id)


def wrong(label: CaseLabel, data: CaseData) -> Report:
    others = [s for s in baro.candidates(data.window) if s != normalize_service(label.service)]
    fault = next(f for f in FAULTS[label.suite] if f != label.fault)
    return Report(case_id=label.id, root_causes=others[:TOP_K], fault_type=fault)


def make_random(seed: int) -> Baseline:
    def run(label: CaseLabel, data: CaseData) -> Report:
        rng = _rng(label.id, seed)
        ranking = baro.candidates(data.window)
        rng.shuffle(ranking)
        return Report(
            case_id=label.id,
            root_causes=ranking[:TOP_K],
            fault_type=rng.choice(FAULTS[label.suite]),
        )

    return run


def baro_report(label: CaseLabel, data: CaseData) -> Report:
    metrics = baro.rank_metrics(data.window)
    services = baro.rank_services(metrics)
    fault = None
    if metrics and label.suite == "re2":  # RE3's code-level faults have no metric family
        top = metrics[0][0]
        fault = FAULT_OF_METRIC_FAMILY.get(top.split("_", 1)[1] if "_" in top else "")
    evidence = [MetricCitation(metric=metrics[0][0], direction="up")] if metrics else []
    return Report(
        case_id=label.id,
        root_causes=services[:TOP_K],
        fault_type=fault,
        evidence=list(evidence),
        summary="BARO (RCAEval): metrics ranked by max robust z after the onset",
    )


def _shape(all_labels: Sequence[CaseLabel], data_dir: Path, how: str) -> Baseline:
    columns: dict[str, frozenset[str]] = {}
    onsets: dict[str, int] = {}

    def features(c: CaseLabel) -> tuple[frozenset[str], int]:
        if c.id not in columns:
            d = data_dir / c.name
            columns[c.id] = frozenset(pq.ParquetFile(d / "metrics.parquet").schema_arrow.names)
            onsets[c.id] = read_window(d).onset_ms
        return columns[c.id], onsets[c.id]

    def run(label: CaseLabel, data: CaseData) -> Report:
        cols, onset = features(label)
        pool = [
            c
            for c in all_labels
            if c.suite == label.suite and c.system == label.system and c.group != label.group
        ]

        def distance(c: CaseLabel) -> float:
            other_cols, other_onset = features(c)
            if how == "columns":
                return 1 - len(cols & other_cols) / max(len(cols | other_cols), 1)
            return abs(other_onset - onset)

        ranked = sorted(pool, key=lambda c: (distance(c), c.name))
        services: list[str] = []
        for c in ranked:
            s = normalize_service(c.service)
            if s not in services:
                services.append(s)
        return Report(case_id=label.id, root_causes=services[:TOP_K])

    return run


def make(name: str, all_labels: Sequence[CaseLabel], data_dir: Path, seed: int = 0) -> Baseline:
    if name == "random":
        return make_random(seed)
    if name in ("shape-columns", "shape-time"):
        return _shape(all_labels, data_dir, name.split("-")[1])
    fixed: dict[str, Baseline] = {
        "oracle": oracle,
        "empty": empty,
        "wrong": wrong,
        "baro": baro_report,
    }
    if name not in fixed:
        raise ValueError(f"unknown baseline {name!r}")
    return fixed[name]


NAMES = ("oracle", "empty", "wrong", "random", "baro", "shape-columns", "shape-time")


def run_baseline(baseline: Baseline, labels: Sequence[CaseLabel], data_dir: Path) -> list[Trial]:
    """A case the method crashes on becomes an error trial: kept, counted and shown next
    to every score, never silently dropped (RCAEval's main.py skips such cases)."""
    trials = []
    for label in labels:
        try:
            report = baseline(label, CaseData(data_dir / label.name))
            trials.append(Trial(case_id=label.id, report=report))
        except Exception as e:  # one bad case must not end the run
            trials.append(
                Trial(case_id=label.id, status="infra_error", error=f"{type(e).__name__}: {e}")
            )
    return trials
