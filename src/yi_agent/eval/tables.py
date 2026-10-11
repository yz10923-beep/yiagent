"""Breakdowns of graded cases and the plain-text table `yi eval` prints."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from yi_agent.data.rcaeval import FAULTS
from yi_agent.eval.scoring import CaseScore, summarize


def breakdown(scores: Sequence[CaseScore]) -> dict[str, dict[str, Any]]:
    """Overall, per suite-system, and per suite-fault (the paper reports per fault)."""
    groups: dict[str, list[CaseScore]] = defaultdict(list)
    for s in scores:
        groups[f"{s.suite}-{s.system}"].append(s)
        groups[f"{s.suite} {s.fault}"].append(s)
    out = {"all": summarize(scores, FAULTS)}
    for name in sorted(groups):
        out[name] = summarize(groups[name], FAULTS)
    return out


def _f(x: object, digits: int = 2) -> str:
    return f"{x:.{digits}f}" if isinstance(x, float) else "-"


def render(rows: dict[str, dict[str, Any]], infra_errors: int = 0) -> str:
    head = (
        f"{'group':<14}{'n':>4}  {'AC@1':>5} {'95% CI':>11} {'AC@3':>5} {'Avg@5':>6} "
        f"{'chance':>6}  {'F1 re2':>6} {'F1 re3':>6}  {'exists':>6} {'holds':>6} "
        f"{'target':>6} {'expect':>6}"
    )
    lines = [head, "-" * len(head)]
    for name, r in rows.items():
        if not r.get("cases"):
            continue
        lo, hi = r["ac1_ci95"]
        lines.append(
            f"{name:<14}{r['cases']:>4}  {_f(r['ac1']):>5} {f'{lo:.2f}-{hi:.2f}':>11} "
            f"{_f(r['ac3']):>5} {_f(r['avg5']):>6} {_f(r.get('chance_avg5')):>6}  "
            f"{_f(r.get('fault_macro_f1_re2')):>6} {_f(r.get('fault_macro_f1_re3')):>6}  "
            f"{_f(r.get('exists')):>6} {_f(r.get('holds')):>6} {_f(r.get('on_target')):>6} "
            f"{_f(r.get('expected_evidence')):>6}"
        )
    lines.append(
        "chance = Avg@5 of a random ranking over the same candidates; F1 = fault-type "
        "macro-F1 within the suite; exists/holds/target = share of citations; expect = "
        "cites the paper's root-cause indicator (cases where it exists)"
    )
    if infra_errors:
        lines.append(f"!! {infra_errors} infra-error trials excluded from every number above")
    return "\n".join(lines)
