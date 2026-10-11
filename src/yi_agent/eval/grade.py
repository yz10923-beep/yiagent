"""Grade a run: its trials against the hidden labels and, when the data is there, the
case data. A run is graded over exactly the split it claims: a missing case, a duplicate,
or a case from outside the split makes it invalid rather than quietly smaller."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from yi_agent.data.rcaeval import CaseLabel
from yi_agent.eval import baro
from yi_agent.eval.case_data import CaseData
from yi_agent.eval.grounding import check, indicates, oracle_evidence
from yi_agent.eval.report import Report, Trial
from yi_agent.eval.scoring import (
    CaseScore,
    ac_at,
    avg_at,
    chance_ac,
    chance_avg,
    normalize_service,
)


class RunInvalid(Exception):
    pass


@dataclass
class Graded:
    scores: list[CaseScore] = field(default_factory=list)
    infra_errors: list[Trial] = field(default_factory=list)


def score_case(report: Report, label: CaseLabel, data: CaseData | None) -> CaseScore:
    ranking = report.root_causes
    score = CaseScore(
        case_id=label.id,
        suite=label.suite,
        system=label.system,
        fault=label.fault,
        ac1=ac_at(ranking, label.service, 1),
        ac3=ac_at(ranking, label.service, 3),
        avg5=avg_at(ranking, label.service),
        fault_pred=report.fault_type,
        citations=len(report.evidence),
    )
    if data is None:
        return score
    n = len(baro.candidates(data.window))
    score.chance_ac1 = chance_ac(n, 1)
    score.chance_avg5 = chance_avg(n)
    rc = normalize_service(label.service)
    checks = [check(c, data) for c in report.evidence]
    if checks:
        score.exists = sum(c.exists for c in checks) / len(checks)
        score.holds = sum(c.holds for c in checks) / len(checks)
        score.on_target = sum(c.exists and c.service == rc for c in checks) / len(checks)
    if oracle_evidence(data, label.service, label.fault):
        hit = any(
            indicates(c, r, label.service, label.fault)
            for c, r in zip(report.evidence, checks, strict=True)
        )
        score.expected_evidence = 1.0 if hit else 0.0
    return score


def grade(
    trials: Sequence[Trial],
    labels: dict[str, CaseLabel],
    ids: Sequence[str],
    data_dir: Path | None,
) -> Graded:
    seen: dict[str, Trial] = {}
    for t in trials:
        if t.case_id in seen:
            raise RunInvalid(f"two trials for case {t.case_id}: a variant is one condition")
        seen[t.case_id] = t
    if extra := sorted(set(seen) - set(ids)):
        raise RunInvalid(f"{len(extra)} trials for cases outside the split: {extra[:3]}")
    if missing := sorted(set(ids) - set(seen)):
        raise RunInvalid(f"{len(missing)} cases of the split have no trial: {missing[:3]}")
    out = Graded()
    for i in ids:
        t = seen[i]
        if t.status != "ok" or t.report is None:
            out.infra_errors.append(t)
            continue
        if t.report.case_id != i:
            raise RunInvalid(f"trial for {i} carries a report for {t.report.case_id}")
        label = labels[i]
        data = CaseData(data_dir / label.name) if data_dir is not None else None
        out.scores.append(score_case(t.report, label, data))
    return out
