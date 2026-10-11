"""Scores from reports and hidden labels: pure functions, no data access.

Root cause is graded as RCAEval grades it (AC@k, Avg@k, names normalized the same way),
fault type by macro-F1 within one suite. Infra failures never reach these functions."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel

TOP_K = 5


def normalize_service(name: str) -> str:
    """RCAEval maps a metric's prefix to a service with `.replace("-db", "")`, so a
    database counts as its service; every method is graded through the same mapping."""
    return name.strip().lower().replace("-db", "")


def service_of_metric(metric: str) -> str:
    return normalize_service(metric.split("_")[0])


def service_ranking(names: Iterable[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        s = normalize_service(n)
        if s and s not in out:
            out.append(s)
    return out


def ac_at(ranking: Sequence[str], truth: str, k: int) -> float:
    return 1.0 if normalize_service(truth) in service_ranking(ranking)[:k] else 0.0


def avg_at(ranking: Sequence[str], truth: str, k: int = TOP_K) -> float:
    return sum(ac_at(ranking, truth, j) for j in range(1, k + 1)) / k


def chance_ac(n_candidates: int, k: int) -> float:
    """A uniformly random ranking of n candidates has the answer in its top k with
    probability min(k, n)/n (RCAEval's Evaluator.chance_accuracy)."""
    return min(k, n_candidates) / n_candidates if n_candidates > 0 else 0.0


def chance_avg(n_candidates: int, k: int = TOP_K) -> float:
    return sum(chance_ac(n_candidates, j) for j in range(1, k + 1)) / k


def macro_f1(truth: Sequence[str], pred: Sequence[str | None], vocabulary: Sequence[str]) -> float:
    """Macro-F1 over the classes that occur in the truth or among valid predictions (as
    scikit-learn's default). An answer outside the vocabulary, or none, is a miss for the
    true class and a false positive for no class."""
    vocab = set(vocabulary)
    valid = [p if p in vocab else None for p in pred]
    classes = sorted(set(truth) | {p for p in valid if p is not None})
    if not classes:
        return 0.0
    f1s = []
    for c in classes:
        tp = sum(1 for t, p in zip(truth, valid, strict=True) if t == c and p == c)
        fp = sum(1 for t, p in zip(truth, valid, strict=True) if t != c and p == c)
        fn = sum(1 for t, p in zip(truth, valid, strict=True) if t == c and p != c)
        f1s.append(2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 0.0)
    return sum(f1s) / len(f1s)


def wilson(successes: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a proportion; honest at small n, where p +- 2 se is not."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


class CaseScore(BaseModel):
    case_id: str
    suite: str
    system: str
    fault: str
    ac1: float
    ac3: float
    avg5: float
    chance_ac1: float | None = None
    chance_avg5: float | None = None
    fault_pred: str | None = None
    # grounding, when graded against the data (None: not graded)
    citations: int = 0
    exists: float | None = None
    holds: float | None = None
    on_target: float | None = None
    expected_evidence: float | None = None


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _mean_opt(xs: Iterable[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return _mean(vals) if vals else None


def summarize(
    scores: Sequence[CaseScore], vocabulary: Mapping[str, Sequence[str]]
) -> dict[str, object]:
    """One row of numbers for a set of cases. Fault-type F1 is per suite, never pooled."""
    n = len(scores)
    out: dict[str, object] = {"cases": n}
    if not n:
        return out
    ac1 = sum(s.ac1 for s in scores)
    out["ac1"] = ac1 / n
    out["ac1_ci95"] = wilson(ac1, n)
    out["ac3"] = _mean([s.ac3 for s in scores])
    out["avg5"] = _mean([s.avg5 for s in scores])
    out["chance_ac1"] = _mean_opt(s.chance_ac1 for s in scores)
    out["chance_avg5"] = _mean_opt(s.chance_avg5 for s in scores)
    by_suite: dict[str, list[CaseScore]] = defaultdict(list)
    for s in scores:
        by_suite[s.suite].append(s)
    for suite, xs in sorted(by_suite.items()):
        out[f"fault_macro_f1_{suite}"] = macro_f1(
            [x.fault for x in xs], [x.fault_pred for x in xs], vocabulary[suite]
        )
    for key in ("exists", "holds", "on_target", "expected_evidence"):
        out[key] = _mean_opt(getattr(s, key) for s in scores)
    out["citations_per_case"] = _mean([s.citations for s in scores])
    return out
