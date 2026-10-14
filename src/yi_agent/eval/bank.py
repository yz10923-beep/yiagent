"""Grading OpenRCA Bank queries (S5), as OpenRCA grades them.

A report names one entry per failure (time, component, reason). It is rendered into
OpenRCA's prediction string and scored by OpenRCA's own `evaluate()` (vendored,
`openrca_evaluate.py`) against the query's verbatim scoring points: the share of points
matched, best pairing over the failures, and 0 unless the report names as many failures
as the query has. A query is strictly right when it scores 1.0; OpenRCA reports strict
accuracy and the mean score, by difficulty (tasks 1-3 easy, 4-6 middle, 7 hard).

Diagnostics on top, never mixed into those numbers: per failure, was the component, the
reason, the time (within a minute) right, whether the query asked for it or not.

Results go under <data_dir>/openrca/results, beside the download: they carry
label-derived detail, and Bank's data stays out of the repo (S5 decisions).
"""

from __future__ import annotations

import csv
import hashlib
import io
import itertools
import json
import random
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from yi_agent.data import openrca
from yi_agent.data.openrca import BankQuery, local_str
from yi_agent.eval.harness import check_approval, provenance, write_json
from yi_agent.eval.openrca_evaluate import evaluate
from yi_agent.eval.report import Citation

SPLITS = ("dev", "test", "all")
# Microsoft's archived RCA-agent run on Bank (rca/archive/agent-Bank.csv in their repo,
# MIT): their predictions with the score their grader gave each. Fetched by `yi data bank
# fetch`, pinned here; it is the published bar.
ARCHIVE_URL = "https://raw.githubusercontent.com/microsoft/OpenRCA/main/rca/archive/agent-Bank.csv"
ARCHIVE_SHA256 = "6737585beba0feba3145220f11d81f55fb09b7cb257a9620b9849e64c85358cd"
ARCHIVE_VARIANT = "openrca-rca-agent"

type Progress = Callable[[str], None]


class RunInvalid(Exception):
    pass


class BankFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    time: str = ""  # "YYYY-mm-dd HH:MM:SS", UTC+8, as OpenRCA asks; "" = not given
    component: str = ""
    reason: str = ""


class BankReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    failures: list[BankFailure] = Field(default_factory=list)  # one per failure
    evidence: list[Citation] = Field(default_factory=list)
    summary: str = ""


class BankTrial(BaseModel):
    case_id: str
    status: Literal["ok", "infra_error"] = "ok"
    error: str | None = None
    report: BankReport | None = None


def prediction_json(report: BankReport | None) -> str:
    """OpenRCA's prediction format: {"1": {datetime, component, reason}, ...}."""
    failures = report.failures if report else []
    return json.dumps(
        {
            str(n): {
                "root cause occurrence datetime": f.time,
                "root cause component": f.component,
                "root cause reason": f.reason,
            }
            for n, f in enumerate(failures, 1)
        },
        ensure_ascii=False,
    )


def difficulty(task: str) -> str:
    n = int(task.split("_")[1])
    return "easy" if n <= 3 else "middle" if n <= 6 else "hard"


class QueryScore(BaseModel):
    case_id: str
    task: str
    score: float  # OpenRCA's, 0..1 in steps of the query's points
    named: int  # failures the report named (it must equal the query's)
    # diagnostics against the full truth, best pairing, whatever the query asked
    component_right: int = 0
    reason_right: int = 0
    time_right: int = 0
    failures: int = 0


def _diagnose(report: BankReport | None, q: BankQuery) -> tuple[int, int, int]:
    said = report.failures if report else []
    best = (0, 0, 0)
    if not said:
        return best
    for perm in itertools.permutations(said, min(len(said), len(q.failures))):
        c = r = t = 0
        for f, truth in zip(perm, q.failures, strict=False):
            c += f.component == truth.component
            r += f.reason == truth.reason
            try:
                t += abs(openrca.local_ms(f.time) - truth.time_ms) <= 60_000
            except ValueError:
                pass
        best = max(best, (c, r, t), key=sum)
    return best


def grade_query(report: BankReport | None, q: BankQuery) -> QueryScore:
    _, _, score = evaluate(prediction_json(report), q.scoring_points)
    c, r, t = _diagnose(report, q)
    return QueryScore(
        case_id=q.id,
        task=q.task,
        score=score,
        named=len(report.failures) if report else 0,
        component_right=c,
        reason_right=r,
        time_right=t,
        failures=len(q.failures),
    )


def grade_run(
    trials: list[BankTrial], queries: dict[str, BankQuery], ids: list[str]
) -> tuple[list[QueryScore], list[BankTrial]]:
    """Scores over exactly the split (a missing or extra case invalidates the run); infra
    failures are counted, never scored."""
    seen: dict[str, BankTrial] = {}
    for t in trials:
        if t.case_id in seen:
            raise RunInvalid(f"two trials for case {t.case_id}: a variant is one condition")
        seen[t.case_id] = t
    if extra := sorted(set(seen) - set(ids)):
        raise RunInvalid(f"{len(extra)} trials for cases outside the split: {extra[:3]}")
    if missing := sorted(set(ids) - set(seen)):
        raise RunInvalid(f"{len(missing)} cases of the split have no trial: {missing[:3]}")
    scores, infra = [], []
    for i in ids:
        t = seen[i]
        if t.status == "infra_error":
            infra.append(t)
            continue
        if t.report is not None and t.report.case_id != i:
            raise RunInvalid(f"trial for {i} carries a report for {t.report.case_id}")
        scores.append(grade_query(t.report, queries[i]))
    return scores, infra


def summarize(
    scores: list[QueryScore], queries: dict[str, BankQuery]
) -> dict[str, dict[str, float]]:
    """Per group: queries, strict accuracy (score 1.0), mean score, and the diagnostics."""
    groups: dict[str, list[QueryScore]] = defaultdict(list)
    for s in scores:
        q = queries[s.case_id]
        groups["all"].append(s)
        groups[difficulty(s.task)].append(s)
        groups[s.task].append(s)
        for reason in {f.reason for f in q.failures}:
            groups[f"reason: {reason}"].append(s)
    out: dict[str, dict[str, float]] = {}
    for g, ss in groups.items():
        f = sum(s.failures for s in ss) or 1
        out[g] = {
            "queries": len(ss),
            "strict": sum(s.score == 1.0 for s in ss) / len(ss),
            "mean": sum(s.score for s in ss) / len(ss),
            "component": sum(s.component_right for s in ss) / f,
            "reason": sum(s.reason_right for s in ss) / f,
            "time": sum(s.time_right for s in ss) / f,
        }
    return out


_ORDER = ["all", "easy", "middle", "hard"]


def render(summary: dict[str, dict[str, float]], infra: int = 0) -> str:
    keys = [k for k in _ORDER if k in summary]
    keys += sorted(k for k in summary if k.startswith("task_"))
    keys += sorted(k for k in summary if k.startswith("reason: "))
    head = (
        f"{'group':<38}{'n':>4}{'strict':>8}{'mean':>7}   {'component':>9}{'reason':>7}{'time':>6}"
    )
    lines = [head, "-" * len(head)]
    for k in keys:
        r = summary[k]
        lines.append(
            f"{k:<38}{int(r['queries']):>4}{r['strict']:>8.2f}{r['mean']:>7.2f}   "
            f"{r['component']:>9.2f}{r['reason']:>7.2f}{r['time']:>6.2f}"
        )
    lines.append(
        "strict = share of queries with every scoring point (OpenRCA's accuracy); mean = "
        "mean score; component / reason / time = share of failures right against the full "
        "truth, asked or not (diagnostic)"
    )
    if infra:
        lines.append(f"{infra} infra failures, not scored")
    return "\n".join(lines)


# ---- runs and baselines ----------------------------------------------------------------


def results_dir(data_dir: Path) -> Path:
    return openrca.raw_dir(data_dir) / "results"


def split_ids(split: str) -> list[str]:
    s = openrca.read_split()
    if split == "all":
        return sorted(s.dev + s.test)
    if split not in SPLITS:
        raise ValueError(f"unknown split {split!r}")
    ids: list[str] = getattr(s, split)
    return ids


def write_trials(trials: list[BankTrial], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(t.model_dump_json() + "\n" for t in trials))


def read_trials(path: Path) -> list[BankTrial]:
    return [BankTrial.model_validate_json(x) for x in path.read_text().splitlines() if x]


def score_run(trials_path: Path, split: str, data_dir: Path, approve: bool) -> str:
    sha = check_approval(approve)
    queries = {q.id: q for q in openrca.read_labels(openrca.labels_path(data_dir))}
    scores, infra = grade_run(read_trials(trials_path), queries, split_ids(split))
    summary = summarize(scores, queries)
    write_json(
        trials_path.parent / "scores.json",
        {
            **provenance(sha),
            "benchmark": "openrca-bank",
            "split": split,
            "summary": summary,
            "infra_errors": [t.model_dump() for t in infra],
            "cases": [s.model_dump() for s in scores],
        },
    )
    return render(summary, len(infra))


def _oracle(q: BankQuery, rng: random.Random) -> list[BankFailure]:
    return [
        BankFailure(time=local_str(f.time_ms), component=f.component, reason=f.reason)
        for f in q.failures
    ]


def _empty(q: BankQuery, rng: random.Random) -> list[BankFailure]:
    return []


def _wrong(q: BankQuery, rng: random.Random) -> list[BankFailure]:
    """The right number of failures, every field wrong: a component and a reason that are
    none of the window's, and a time ten minutes off every true one."""
    comps = [c for c in openrca.COMPONENTS if c not in {f.component for f in q.failures}]
    reasons = [r for r in openrca.REASONS if r not in {f.reason for f in q.failures}]
    times = [f.time_ms for f in q.failures]
    out = []
    for n, f in enumerate(q.failures):
        t = f.time_ms + 10 * 60_000
        while any(abs(t - x) <= 60_000 for x in times):
            t += 2 * 60_000
        out.append(BankFailure(time=local_str(t), component=comps[n], reason=reasons[n]))
    return out


def _random(q: BankQuery, rng: random.Random) -> list[BankFailure]:
    """Chance: the right number of failures (the query states it), each a random
    component, reason and minute of the window."""
    return [
        BankFailure(
            time=local_str(q.start_ms + 60_000 * rng.randrange(30)),
            component=rng.choice(openrca.COMPONENTS),
            reason=rng.choice(openrca.REASONS),
        )
        for _ in q.failures
    ]


BASELINES: dict[str, Callable[[BankQuery, random.Random], list[BankFailure]]] = {
    "oracle": _oracle,
    "empty": _empty,
    "wrong": _wrong,
    "random": _random,
}


def run_baseline(
    name: str, split: str, data_dir: Path, approve: bool, seed: int = 0
) -> tuple[Path, str]:
    check_approval(approve)  # fail before the run, not after it
    queries = {q.id: q for q in openrca.read_labels(openrca.labels_path(data_dir))}
    rng = random.Random(seed)
    make = BASELINES[name]
    trials = [
        BankTrial(case_id=i, report=BankReport(case_id=i, failures=make(queries[i], rng)))
        for i in split_ids(split)
    ]
    path = results_dir(data_dir) / f"bank-{name}" / split / "trials.jsonl"
    write_trials(trials, path)
    return path, score_run(path, split, data_dir, approve=False)


# ---- Microsoft's archived run ----------------------------------------------------------


def archive_path(data_dir: Path) -> Path:
    return openrca.raw_dir(data_dir) / "archive" / "agent-Bank.csv"


def fetch_archive(data_dir: Path) -> Path:
    import urllib.request

    path = archive_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as r:  # noqa: S310 (fixed https URL)
        body = r.read()
    if hashlib.sha256(body).hexdigest() != ARCHIVE_SHA256:
        raise ValueError(f"{ARCHIVE_URL} differs from the pinned file; not saved")
    path.write_bytes(body)
    return path


def replay_archive(
    data_dir: Path, approve: bool, archive_sha: str | None = None
) -> tuple[int, int, list[str], dict[str, str]]:
    """Re-grade Microsoft's archived predictions with the vendored grader and our labels:
    (rows whose recorded score we reproduce, rows, mismatches, table per split). The
    recorded scores are unrounded, `evaluate()` rounds to two decimals: equal within 0.005.
    """
    check_approval(approve)
    path = archive_path(data_dir)
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != (archive_sha or ARCHIVE_SHA256):
        raise ValueError(f"{path}: differs from the pinned archive; rerun `yi data bank fetch`")
    csv.field_size_limit(1 << 30)
    rows = list(csv.DictReader(io.StringIO(body.decode())))
    queries = openrca.read_labels(openrca.labels_path(data_dir))
    by_text = {_instruction(data_dir, q.row): q for q in queries}
    same, mismatches, trials = 0, [], []
    for row in rows:
        q = by_text.get(row["instruction"])
        if q is None:
            mismatches.append(f"archived row {row['row_id']}: instruction not in query.csv")
            continue
        _, _, ours = evaluate(row["prediction"], q.scoring_points)
        report = _archived_report(q.id, row["prediction"])
        piped = grade_query(report, q).score  # through BankReport and prediction_json
        if abs(ours - float(row["score"])) <= 0.005 and piped == ours:
            same += 1
        else:
            mismatches.append(
                f"{q.id}: ours {ours}, through a report {piped}, archived {float(row['score']):.4f}"
            )
        trials.append(BankTrial(case_id=q.id, report=report))
    tables: dict[str, str] = {}
    for split in SPLITS:
        ids = set(split_ids(split))
        p = results_dir(data_dir) / ARCHIVE_VARIANT / split / "trials.jsonl"
        write_trials([t for t in trials if t.case_id in ids], p)
        tables[split] = score_run(p, split, data_dir, approve=False)
    return same, len(rows), mismatches, tables


_QUERY_TEXT: dict[Path, list[str]] = {}


def _instruction(data_dir: Path, row: int) -> str:
    path = openrca.raw_dir(data_dir) / openrca.SYSTEM / "query.csv"
    if path not in _QUERY_TEXT:
        _QUERY_TEXT[path] = [
            r["instruction"] for r in csv.DictReader(io.StringIO(path.read_text()))
        ]
    return _QUERY_TEXT[path][row]


def _archived_report(case_id: str, prediction: str) -> BankReport:
    """The archived prediction as a report: the same entries `evaluate()` reads from it,
    so its score through our pipeline equals its score on the raw string."""
    import re

    pattern = (
        r"{\s*"
        r'(?:"root cause occurrence datetime":\s*"(.*?)")?,?\s*'
        r'(?:"root cause component":\s*"(.*?)")?,?\s*'
        r'(?:"root cause reason":\s*"(.*?)")?\s*}'
    )
    failures = [
        BankFailure(time=t, component=c, reason=r) for t, c, r in re.findall(pattern, prediction)
    ]
    return BankReport(case_id=case_id, failures=failures, summary="archived prediction")
