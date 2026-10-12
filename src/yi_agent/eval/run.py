"""Running baselines and scoring runs: everything between a split's name and its
scores.json. It lives in the hashed eval package because it decides which cases are
graded; the CLI only parses arguments."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path

import pyarrow.parquet as pq

from yi_agent.data.rcaeval import LABELS_PATH, SPLIT_PATH, CaseLabel, read_labels
from yi_agent.data.split import read_split
from yi_agent.data.telemetry import read_window
from yi_agent.eval import baro, baselines
from yi_agent.eval.case_data import CaseData
from yi_agent.eval.grade import Graded, grade
from yi_agent.eval.grounding import oracle_evidence
from yi_agent.eval.harness import RESULTS_DIR, check_approval, provenance, write_json
from yi_agent.eval.report import read_trials, write_trials
from yi_agent.eval.scoring import avg_at, normalize_service, service_of_metric
from yi_agent.eval.tables import breakdown, render

SPLITS = ("dev", "dev_small", "test", "all")
# RCAEval README, `main.py --method baro --dataset re2-tt --length 20`, all 90 cases.
PUBLISHED_BARO_AVG5 = {
    ("re2", "tt"): {
        "cpu": 0.72,
        "mem": 0.99,
        "disk": 1.0,
        "socket": 0.83,
        "delay": 0.63,
        "loss": 0.64,
    }
}

type Progress = Callable[[str], None]


def labels_by_id() -> dict[str, CaseLabel]:
    return {c.id: c for c in read_labels(LABELS_PATH)}


def split_ids(split: str, labels: dict[str, CaseLabel]) -> list[str]:
    if split == "all":
        return sorted(labels)
    if split not in SPLITS:
        raise ValueError(f"unknown split {split!r}")
    ids: list[str] = getattr(read_split(SPLIT_PATH), split)
    return ids


def score_run(trials_path: Path, split: str, data_dir: Path, approve: bool) -> tuple[str, Graded]:
    """Grade a trials file over a whole split; writes scores.json next to it."""
    sha = check_approval(approve)
    labels = labels_by_id()
    ids = split_ids(split, labels)
    graded = grade(read_trials(trials_path), labels, ids, data_dir)
    rows = breakdown(graded.scores)
    write_json(
        trials_path.parent / "scores.json",
        {
            **provenance(sha),
            "split": split,
            "trials": trials_path.name,
            "summary": rows,
            "infra_errors": [t.model_dump() for t in graded.infra_errors],
            "cases": [s.model_dump() for s in graded.scores],
        },
    )
    return render(rows, len(graded.infra_errors)), graded


def run_baseline(
    name: str,
    split: str,
    data_dir: Path,
    approve: bool,
    variant: str | None = None,
    seed: int = 0,
    progress: Progress | None = None,
) -> tuple[Path, str]:
    check_approval(approve)  # fail before the run, not after it
    labels = labels_by_id()
    chosen = [labels[i] for i in split_ids(split, labels)]
    baseline = baselines.make(name, list(labels.values()), data_dir, seed=seed)
    trials = []
    for n, label in enumerate(chosen, 1):
        trials += baselines.run_baseline(baseline, [label], data_dir)
        if progress and (n % 10 == 0 or n == len(chosen)):
            progress(f"  {name}: {n}/{len(chosen)} cases")
    path = RESULTS_DIR / (variant or name) / split / "trials.jsonl"
    write_trials(trials, path)
    table, _ = score_run(path, split, data_dir, approve=False)
    return path, table


def reproduce_baro(
    data_dir: Path,
    systems: Sequence[str],
    progress: Progress | None = None,
    exclude: tuple[str, ...] = (),
    show_misses: bool = False,
) -> str:
    """BARO on every RE2 case of the systems (the exception for untuned baselines,
    docs/ROADMAP.md), Avg@5 per fault against the published numbers. `exclude` and
    `show_misses` are for diagnosing a gap: which metrics outrank the root cause."""
    labels = [c for c in labels_by_id().values() if c.suite == "re2" and c.system in systems]
    per: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    misses: list[str] = []
    for n, c in enumerate(sorted(labels, key=lambda c: c.name), 1):
        d = data_dir / c.name
        window = baro.prepare(d / "metrics.parquet", read_window(d).onset_ms // 1000, exclude)
        metrics = baro.rank_metrics(window)
        services = baro.rank_services(metrics)
        per[(c.suite, c.system, c.fault)].append(avg_at(services, c.service))
        rc = normalize_service(c.service)
        if show_misses and (not services or services[0] != rc):
            pos = services.index(rc) + 1 if rc in services else None
            first_rc = next((m for m, _ in metrics if service_of_metric(m) == rc), None)
            top = ", ".join(f"{m} {z:.0f}" for m, z in metrics[:3])
            misses.append(f"  {c.name}: rc at {pos}, its best {first_rc}; top: {top}")
        if progress and n % 30 == 0:
            progress(f"  baro: {n}/{len(labels)} cases")
    lines = [f"{'dataset':<8}{'fault':<8}{'n':>4}{'ours':>7}{'published':>11}{'diff':>7}"]
    for (suite, system, fault), xs in sorted(per.items()):
        ours = sum(xs) / len(xs)
        pub = PUBLISHED_BARO_AVG5.get((suite, system), {}).get(fault)
        diff = f"{ours - pub:+.2f}" if pub is not None else "-"
        pub_s = f"{pub:.2f}" if pub is not None else "-"
        lines.append(f"{suite}-{system:<4}{fault:<8}{len(xs):>4}{ours:>7.2f}{pub_s:>11}{diff:>7}")
    if exclude:
        lines.append(f"(metric families excluded: {', '.join(exclude)})")
    if misses:
        lines.append(f"cases where BARO's first service is not the root cause ({len(misses)}):")
        lines += misses
    return "\n".join(lines)


def evidence_coverage(data_dir: Path, split: str, progress: Progress | None = None) -> str:
    """For how many cases does the paper's root-cause indicator exist and hold in the
    data? Cases without it are not graded on expected evidence; this says how many."""
    labels = labels_by_id()
    found: dict[str, Counter[str]] = defaultdict(Counter)
    missing: list[str] = []
    ids = split_ids(split, labels)
    for n, i in enumerate(ids, 1):
        c = labels[i]
        ev = oracle_evidence(CaseData(data_dir / c.name), c.service, c.fault)
        key = f"{c.suite} {c.fault}"
        found[key]["cases"] += 1
        found[key]["covered"] += bool(ev)
        for e in ev:
            found[key][e.kind] += 1
        if not ev:
            missing.append(f"{i} ({c.suite}-{c.system} {c.fault})")
        if progress and n % 20 == 0:
            progress(f"  coverage: {n}/{len(ids)} cases")
    lines = [f"{'fault':<12}{'cases':>6}{'covered':>8}{'metric':>7}{'span':>6}{'log':>5}"]
    for key, cnt in sorted(found.items()):
        lines.append(
            f"{key:<12}{cnt['cases']:>6}{cnt['covered']:>8}{cnt['metric']:>7}"
            f"{cnt['span']:>6}{cnt['log']:>5}"
        )
    if missing:
        lines.append(f"no indicator found in {len(missing)} cases: " + ", ".join(missing[:12]))
    return "\n".join(lines)


def metric_kinds(data_dir: Path, split: str) -> str:
    """Which metric families each system exports (the part of a column after the first
    `_`), to check the indicator rules against the real column names."""
    labels = labels_by_id()
    kinds: dict[str, Counter[str]] = defaultdict(Counter)
    cases: Counter[str] = Counter()
    for i in split_ids(split, labels):
        c = labels[i]
        names = pq.ParquetFile(data_dir / c.name / "metrics.parquet").schema_arrow.names
        sysname = f"{c.suite}-{c.system}"
        cases[sysname] += 1
        for fam in {n.split("_", 1)[1] for n in names if "_" in n}:
            kinds[sysname][fam] += 1
    out = []
    for sysname, cnt in sorted(kinds.items()):
        fams = ", ".join(f"{k} ({v}/{cases[sysname]})" for k, v in sorted(cnt.items()))
        out.append(f"{sysname}: {fams}")
    return "\n".join(out)
