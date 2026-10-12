"""The grader selftest (CLAUDE.md, Invariants): an oracle passes, random lands at chance,
an empty or wrong report fails, and fabricated evidence is caught. Runs inside
`make verify` on synthetic cases whose answer is planted where the paper says to look."""

from __future__ import annotations

import random
import statistics
from pathlib import Path

import numpy as np
import pytest

from tests.rcaeval_fixture import ONSET_S, make_fault_case
from yi_agent.data.rcaeval import FAULTS, CaseLabel, parse_case_name
from yi_agent.eval import baro, baselines, harness
from yi_agent.eval.case_data import CaseData
from yi_agent.eval.grade import RunInvalid, grade, score_case
from yi_agent.eval.grounding import check
from yi_agent.eval.report import LogCitation, MetricCitation, Report, SpanCitation, Trial
from yi_agent.eval.scoring import (
    ac_at,
    avg_at,
    chance_ac,
    chance_avg,
    macro_f1,
    normalize_service,
    service_ranking,
    wilson,
)
from yi_agent.eval.tables import breakdown, render

# One case per RE2 fault and three code-level ones, over three systems.
NAMES = [
    "re2tt_ts-order-service_cpu_1",
    "re2ob_cartservice_mem_1",
    "re2ss_carts_disk_1",
    "re2tt_ts-route-service_socket_2",
    "re2ob_emailservice_delay_1",
    "re2ss_orders_loss_3",
    "re3ss_carts_f1_1",
    "re3tt_ts-auth-service_f2_1",
    "re3ob_emailservice_f5_2",
]
OTHERS = ["alpha", "beta-db", "gamma", "delta", "epsilon", "zeta", "eta"]


@pytest.fixture(scope="module")
def cases(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, CaseLabel]]:
    data_dir = tmp_path_factory.mktemp("data")
    labels = {}
    for k, name in enumerate(NAMES):
        label = parse_case_name(name)
        services = list(OTHERS)
        services.insert(k % (len(OTHERS) + 1), label.service)  # the answer moves around
        make_fault_case(data_dir, name, services)
        labels[label.id] = label
    return data_dir, labels


def run(name: str, cases: tuple[Path, dict[str, CaseLabel]], seed: int = 0) -> list[Trial]:
    data_dir, labels = cases
    b = baselines.make(name, list(labels.values()), data_dir, seed=seed)
    return baselines.run_baseline(b, list(labels.values()), data_dir)


def graded(trials: list[Trial], cases: tuple[Path, dict[str, CaseLabel]]) -> dict[str, object]:
    data_dir, labels = cases
    g = grade(trials, labels, list(labels), data_dir)
    assert not g.infra_errors, g.infra_errors
    return breakdown(g.scores)["all"]


# ---- the anchors


def test_oracle_scores_one_everywhere(cases: tuple[Path, dict[str, CaseLabel]]) -> None:
    r = graded(run("oracle", cases), cases)
    assert r["ac1"] == r["ac3"] == r["avg5"] == 1.0
    assert r["fault_macro_f1_re2"] == r["fault_macro_f1_re3"] == 1.0
    assert r["exists"] == r["holds"] == r["on_target"] == 1.0
    assert r["expected_evidence"] == 1.0  # the planted indicator is found for every fault


def test_empty_report_scores_zero(cases: tuple[Path, dict[str, CaseLabel]]) -> None:
    r = graded(run("empty", cases), cases)
    assert r["ac1"] == r["ac3"] == r["avg5"] == 0.0
    assert r["fault_macro_f1_re2"] == r["fault_macro_f1_re3"] == 0.0
    assert r["expected_evidence"] == 0.0
    assert r["exists"] is None  # no claims, nothing to ground


def test_confidently_wrong_report_scores_zero(cases: tuple[Path, dict[str, CaseLabel]]) -> None:
    r = graded(run("wrong", cases), cases)
    assert r["ac1"] == r["ac3"] == r["avg5"] == 0.0
    assert r["fault_macro_f1_re2"] == r["fault_macro_f1_re3"] == 0.0


def test_random_lands_at_chance(cases: tuple[Path, dict[str, CaseLabel]]) -> None:
    data_dir, labels = cases
    data = {i: CaseData(data_dir / c.name) for i, c in labels.items()}  # read each case once
    chance1 = statistics.mean(chance_ac(len(baro.candidates(d.window)), 1) for d in data.values())
    chance5 = statistics.mean(chance_avg(len(baro.candidates(d.window))) for d in data.values())
    assert chance1 == pytest.approx(1 / 8)  # 8 candidate services per case
    ac1, avg5, f1 = [], [], []
    for seed in range(300):
        rand = baselines.make_random(seed)
        scores = [score_case(rand(c, data[i]), c, None) for i, c in labels.items()]
        ac1 += [x.ac1 for x in scores]
        avg5 += [x.avg5 for x in scores]
        re2 = [(x.fault, x.fault_pred) for x in scores if x.suite == "re2"]
        f1.append(macro_f1([t for t, _ in re2], [p for _, p in re2], FAULTS["re2"]))
    # 2700 draws: one standard error of AC@1 is about 0.006
    assert statistics.mean(ac1) == pytest.approx(chance1, abs=0.025)
    assert statistics.mean(avg5) == pytest.approx(chance5, abs=0.025)
    assert statistics.mean(f1) < 0.35  # chance for 6 classes is about 1/6


def test_baro_finds_planted_resource_faults(cases: tuple[Path, dict[str, CaseLabel]]) -> None:
    data_dir, labels = cases
    trials = run("baro", cases)
    by = {t.case_id: t for t in trials}
    for i, label in labels.items():
        report = by[i].report
        assert report is not None
        if label.suite == "re2":
            assert report.root_causes[0] == normalize_service(label.service), label.name
            want = "delay" if label.fault in ("delay", "loss") else label.fault
            assert report.fault_type == want, label.name  # loss looks like delay to BARO
        else:
            assert report.fault_type is None  # no metric family for code-level faults


# ---- grounding catches what is made up


def test_fabricated_citations_fail_grounding(cases: tuple[Path, dict[str, CaseLabel]]) -> None:
    data_dir, labels = cases
    label = parse_case_name("re3ss_carts_f1_1")
    data = CaseData(data_dir / label.name)
    real_log = LogCitation(
        container="carts", at_ms=(ONSET_S + 5) * 1000, excerpt="NullPointerException: price"
    )
    assert (
        check(real_log, data).exists and check(real_log, data).holds and check(real_log, data).error
    )
    made_up = [
        LogCitation(
            container="carts", at_ms=(ONSET_S + 5) * 1000, excerpt="OutOfMemoryError in heap"
        ),
        LogCitation(
            container="carts", at_ms=(ONSET_S + 50) * 1000, excerpt="NullPointerException: price"
        ),
        LogCitation(
            container="gamma", at_ms=(ONSET_S + 5) * 1000, excerpt="NullPointerException: price"
        ),
        SpanCitation(span_id="not-a-span"),
        MetricCitation(metric="carts_threads", direction="up"),
    ]
    for c in made_up:
        assert not check(c, data).exists, c
    # Real but does not hold: a stationary metric did not go up; a line from before the onset.
    assert not check(MetricCitation(metric="gamma_cpu", direction="up"), data).holds
    before = LogCitation(
        container="gamma", at_ms=(ONSET_S - 60) * 1000, excerpt="handled request 0 in"
    )
    assert check(before, data).exists and not check(before, data).holds


def test_metric_direction_is_checked(cases: tuple[Path, dict[str, CaseLabel]]) -> None:
    data_dir, _ = cases
    data = CaseData(data_dir / "re2tt_ts-order-service_cpu_1")
    assert check(MetricCitation(metric="ts-order-service_cpu", direction="up"), data).holds
    assert not check(MetricCitation(metric="ts-order-service_cpu", direction="down"), data).holds


def test_evidence_about_another_service_is_off_target(
    cases: tuple[Path, dict[str, CaseLabel]],
) -> None:
    data_dir, labels = cases
    label = parse_case_name("re2tt_ts-order-service_cpu_1")
    data = CaseData(data_dir / label.name)
    report = Report(
        case_id=label.id,
        root_causes=["ts-order-service"],
        fault_type="cpu",
        evidence=[MetricCitation(metric="gamma_mem", direction="down")],  # exists, wrong service
    )
    s = score_case(report, label, data)
    assert s.exists == 1.0 and s.on_target == 0.0 and s.expected_evidence == 0.0
    assert s.ac1 == 1.0  # the answer is graded separately from its evidence


# ---- the run must be the whole split, and infra failures are not scores


def test_runs_that_are_not_the_split_are_invalid(cases: tuple[Path, dict[str, CaseLabel]]) -> None:
    data_dir, labels = cases
    trials = run("oracle", cases)
    ids = list(labels)
    with pytest.raises(RunInvalid, match="no trial"):
        grade(trials[1:], labels, ids, data_dir)
    with pytest.raises(RunInvalid, match="two trials"):
        grade([*trials, trials[0]], labels, ids, data_dir)
    with pytest.raises(RunInvalid, match="outside the split"):
        grade(trials, labels, ids[1:], data_dir)


def test_infra_errors_are_counted_not_scored(cases: tuple[Path, dict[str, CaseLabel]]) -> None:
    data_dir, labels = cases
    trials = run("oracle", cases)
    trials[0] = Trial(case_id=trials[0].case_id, status="infra_error", error="529 overloaded")
    g = grade(trials, labels, list(labels), data_dir)
    assert len(g.infra_errors) == 1 and len(g.scores) == len(labels) - 1
    assert breakdown(g.scores)["all"]["ac1"] == 1.0  # not dragged down by the outage
    assert "1 infra-error trials excluded" in render(breakdown(g.scores), len(g.infra_errors))


def test_a_crashing_baseline_case_becomes_an_error_trial(
    cases: tuple[Path, dict[str, CaseLabel]],
) -> None:
    data_dir, labels = cases

    def crash(label: CaseLabel, data: CaseData) -> Report:
        raise ValueError("boom")

    trials = baselines.run_baseline(crash, list(labels.values())[:2], data_dir)
    assert [t.status for t in trials] == ["infra_error", "infra_error"]
    assert trials[0].error == "ValueError: boom"


# ---- the formulas, against hand-worked values


def test_ac_and_avg_as_rcaeval_defines_them() -> None:
    ranking = ["a", "b", "c", "d"]
    assert [ac_at(ranking, "c", k) for k in range(1, 6)] == [0, 0, 1, 1, 1]
    assert avg_at(ranking, "c") == pytest.approx(3 / 5)
    assert avg_at(ranking, "z") == 0.0
    # names normalized as RCAEval does: case, spaces, "-db", duplicates
    assert service_ranking([" Carts-DB", "carts", "orders"]) == ["carts", "orders"]
    assert ac_at(["carts-db"], "carts", 1) == 1.0


def test_chance_matches_rcaeval() -> None:
    assert chance_ac(8, 1) == pytest.approx(1 / 8)
    assert chance_ac(3, 5) == 1.0
    assert chance_avg(10) == pytest.approx((0.1 + 0.2 + 0.3 + 0.4 + 0.5) / 5)


def test_macro_f1_by_hand() -> None:
    truth = ["cpu", "cpu", "mem", "mem"]
    pred = ["cpu", "mem", "mem", None]  # cpu: P=1 R=.5; mem: P=.5 R=.5
    assert macro_f1(truth, pred, ["cpu", "mem", "disk"]) == pytest.approx((2 / 3 + 1 / 2) / 2)
    assert macro_f1(["f1"], ["banana"], ["f1", "f2"]) == 0.0  # out of vocabulary is a miss


def test_wilson_interval() -> None:
    lo, hi = wilson(8, 10)
    assert (round(lo, 3), round(hi, 3)) == (0.490, 0.943)
    assert wilson(0, 0) == (0.0, 0.0)


# ---- the BARO port, step by step against RCAEval's code


def test_robust_z_and_zero_spread() -> None:
    before = np.array([[1.0, 5.0], [2.0, 5.0], [3.0, 5.0], [4.0, 5.0], [5.0, 5.0]])
    after = np.array([[3.0, 5.0], [11.0, 6.0]])
    w = baro.MetricWindow(names=["a_cpu", "b_cpu"], before=before, after=after)
    # a: median 3, IQR 4-2 = 2, max z = (11-3)/2 = 4. b: constant before, so dropped
    # (RCAEval's drop_constant runs per half).
    assert baro.rank_metrics(w) == [("a_cpu", 4.0)]
    w2 = baro.MetricWindow(
        names=["c_mem"],
        before=np.array([[1.0], [1.0], [1.0], [1.0], [2.0]]),
        after=np.array([[1.0], [7.0]]),
    )
    # IQR 0 (linear percentiles of 1,1,1,1,2: q25 = q75 = 1): scale becomes 1, as in
    # scikit-learn's RobustScaler. (With 1,1,1,2, q75 is 1.25: interpolation matters.)
    assert baro.rank_metrics(w2) == [("c_mem", 6.0)]


def test_ffill_then_zero_like_pandas() -> None:
    x = np.array([[np.nan, 1.0], [2.0, np.nan], [np.nan, np.nan], [4.0, 3.0]])
    out = baro._ffill_then_zero(x)
    assert out.tolist() == [[0.0, 1.0], [2.0, 1.0], [2.0, 1.0], [4.0, 3.0]]


def test_services_rank_by_first_metric_with_db_folded() -> None:
    ranks = [("carts-db_cpu", 9.0), ("orders_mem", 8.0), ("carts_mem", 7.0)]
    assert baro.rank_services(ranks) == ["carts", "orders"]


def test_window_drops_latency_50_and_keeps_ten_minutes(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    times = list(range(ONSET_S - 700, ONSET_S + 700))
    pq.write_table(
        pa.table(
            {
                "time": pa.array(times, pa.int64()),
                "a_latency-50": [1.0] * len(times),
                "a_latency-90": [float(i % 7) for i in range(len(times))],
            }
        ),
        tmp_path / "m.parquet",
    )
    w = baro.prepare(tmp_path / "m.parquet", ONSET_S)
    assert w.names == ["a_latency-90"]
    assert w.before.shape == (600, 1) and w.after.shape == (600, 1)


# ---- the harness lock


def test_harness_hash_changes_with_any_hashed_file_and_needs_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "grader.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(harness, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(harness, "hashed_files", lambda: [f])
    monkeypatch.setattr(harness, "APPROVAL_FILE", tmp_path / "harness.sha256")
    with pytest.raises(harness.HarnessNotApproved, match="approved=none"):
        harness.check_approval(approve=False)
    sha = harness.check_approval(approve=True)  # what Yi's --approve-harness does
    assert harness.check_approval(approve=False) == sha
    f.write_text("x = 2  # an easier grader\n")
    with pytest.raises(harness.HarnessNotApproved, match="changed since it was approved"):
        harness.check_approval(approve=False)


def test_the_hash_covers_graders_labels_split_and_data_reading() -> None:
    names = {p.relative_to(harness.REPO_ROOT).as_posix() for p in harness.hashed_files()}
    for must in (
        "src/yi_agent/eval/grade.py",
        "src/yi_agent/eval/grounding.py",
        "src/yi_agent/eval/scoring.py",
        "src/yi_agent/eval/baro.py",
        "src/yi_agent/eval/run.py",
        "src/yi_agent/data/telemetry.py",
        "evals/grader/labels.json",
        "evals/split.json",
    ):
        assert must in names


def test_random_baseline_is_reproducible(cases: tuple[Path, dict[str, CaseLabel]]) -> None:
    a = [t.report for t in run("random", cases, seed=3)]
    b = [t.report for t in run("random", cases, seed=3)]
    c = [t.report for t in run("random", cases, seed=4)]
    assert a == b and a != c
    random.seed(0)  # the baseline must not depend on the global generator
    assert [t.report for t in run("random", cases, seed=3)] == a


def test_exclude_drops_whole_metric_families_for_diagnosis(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    times = list(range(ONSET_S - 5, ONSET_S + 5))
    cols = {"time": pa.array(times, pa.int64())}
    for name in ("a_cpu", "a_workload", "b_error", "b_latency-90"):
        cols[name] = pa.array([float(i) for i in range(len(times))])
    pq.write_table(pa.table(cols), tmp_path / "m.parquet")
    w = baro.prepare(tmp_path / "m.parquet", ONSET_S, exclude=("workload", "error"))
    assert w.names == ["a_cpu", "b_latency-90"]
    assert baro.family("ts-order-service_latency-90") == "latency-90"
