"""Grading OpenRCA Bank (S5 step 2): OpenRCA's own rules, the report's rendering into its
prediction format, and the grader selftest (an oracle passes, a null is at chance, a
wrong answer fails), on the synthetic Bank. The real check, Microsoft's archived run
re-graded 136 of 136, runs on the VM (`yi eval bank-replay`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.openrca_fixture import ARCHIVED, make_bank
from tests.unit.test_openrca import _labels, _use_fixture
from yi_agent.data import openrca
from yi_agent.eval import bank, harness
from yi_agent.eval.bank import BankFailure, BankReport, BankTrial
from yi_agent.eval.openrca_evaluate import evaluate

ONE = (
    "The only root cause occurrence time is within 1 minutes (i.e., <=1min) of "
    "2021-03-04 23:41:00\n"
    "The only predicted root cause component is MG01\n"
    "The only predicted root cause reason is network latency\n"
)
TWO = (
    "The 1-th predicted root cause component is IG02\n"
    "The 2-th predicted root cause component is apache01\n"
)


def _pred(*failures: BankFailure) -> str:
    return bank.prediction_json(BankReport(case_id="x", failures=list(failures)))


@pytest.mark.parametrize(
    ("failures", "score"),
    [
        (
            [BankFailure(time="2021-03-04 23:41:00", component="MG01", reason="network latency")],
            1.0,
        ),
        (
            [BankFailure(time="2021-03-04 23:42:00", component="MG01", reason="network latency")],
            1.0,
        ),
        (
            [BankFailure(time="2021-03-04 23:42:01", component="MG01", reason="network latency")],
            0.67,
        ),
        ([BankFailure(time="23:41", component="MG01", reason="network latency")], 0.67),
        ([BankFailure(component="mg01", reason="network latency")], 0.33),  # exact match only
        ([], 0.0),
        ([BankFailure(component="MG01"), BankFailure(component="MG02")], 0.0),  # count differs
    ],
)
def test_openrca_rules_on_one_failure(failures: list[BankFailure], score: float) -> None:
    assert evaluate(_pred(*failures), ONE)[2] == score


def test_two_failures_are_paired_in_whichever_order_scores_best() -> None:
    a, b = BankFailure(component="apache01"), BankFailure(component="IG02")
    assert evaluate(_pred(a, b), TWO)[2] == evaluate(_pred(b, a), TWO)[2] == 1.0
    assert evaluate(_pred(a, a), TWO)[2] == 0.5


def test_a_component_with_quotes_or_unicode_stays_one_entry() -> None:
    odd = BankFailure(component="Tomcat01 “x”", reason="é")
    assert len(bank._archived_report("x", _pred(odd)).failures) == 1


@pytest.fixture
def bank_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data dir with the synthetic download, its labels and its archive, the fixture's
    pins in place, and the current harness approved (in tmp, never the repo's file)."""
    data_dir = tmp_path / "data"
    fx = make_bank(openrca.raw_dir(data_dir))
    _use_fixture(fx, monkeypatch, tmp_path)
    openrca.labels_path(data_dir).write_text(openrca.labels_json(_labels(fx)))
    approval = tmp_path / "harness.sha256"
    approval.write_text(harness.harness_sha() + "\n")
    monkeypatch.setattr(harness, "APPROVAL_FILE", approval)
    return data_dir


def _strict_mean(table: str) -> tuple[float, float]:
    row = next(line for line in table.splitlines() if line.startswith("all "))
    _, _, strict, mean, *_ = row.split()
    return float(strict), float(mean)


def test_the_grader_selftest(bank_data: Path) -> None:
    assert _strict_mean(bank.run_baseline("oracle", "all", bank_data, False)[1]) == (1.0, 1.0)
    assert _strict_mean(bank.run_baseline("empty", "all", bank_data, False)[1]) == (0.0, 0.0)
    assert _strict_mean(bank.run_baseline("wrong", "all", bank_data, False)[1]) == (0.0, 0.0)
    strict, mean = _strict_mean(bank.run_baseline("random", "all", bank_data, False)[1])
    assert 0.0 <= strict < 0.5 and mean < 0.5


def test_results_stay_beside_the_download(bank_data: Path) -> None:
    path, _ = bank.run_baseline("oracle", "all", bank_data, False)
    assert path.is_relative_to(openrca.raw_dir(bank_data))
    assert (path.parent / "scores.json").is_file()


def test_a_run_must_cover_its_split_and_infra_failures_are_not_scored(bank_data: Path) -> None:
    queries = {q.id: q for q in openrca.read_labels(openrca.labels_path(bank_data))}
    ids = sorted(queries)
    trials = [BankTrial(case_id=i, report=BankReport(case_id=i)) for i in ids]
    with pytest.raises(bank.RunInvalid, match="no trial"):
        bank.grade_run(trials[1:], queries, ids)
    with pytest.raises(bank.RunInvalid, match="two trials"):
        bank.grade_run([*trials, trials[0]], queries, ids)
    trials[0] = BankTrial(case_id=ids[0], status="infra_error", error="ES down")
    scores, infra = bank.grade_run(trials, queries, ids)
    assert len(scores) == len(ids) - 1 and [t.case_id for t in infra] == [ids[0]]


def test_the_replay_reproduces_every_archived_score(bank_data: Path) -> None:
    same, n, mismatches, tables = bank.replay_archive(bank_data, False)
    assert (same, n, mismatches) == (len(ARCHIVED), len(ARCHIVED), [])
    strict, mean = _strict_mean(tables["all"])
    assert strict == 0.5 and mean == pytest.approx((1 + 1 + 1 / 3 + 0 + 1 + 0) / 6, abs=0.01)


def test_a_changed_archive_is_refused(bank_data: Path) -> None:
    path = bank.archive_path(bank_data)
    path.write_text(path.read_text().replace("Tomcat01", "Tomcat03"))
    with pytest.raises(ValueError, match="differs from the pinned archive"):
        bank.replay_archive(bank_data, False)


def test_the_bank_eval_commands_run_through_the_cli(
    bank_data: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from yi_agent import cli

    monkeypatch.setenv("YI_DATA_DIR", str(bank_data))
    assert cli.main(["eval", "bank-replay"]) == 0
    out = capsys.readouterr().out
    assert "recorded scores reproduced: 6/6" in out and "MISMATCH" not in out
    assert cli.main(["eval", "bank-baseline", "oracle", "--split", "all"]) == 0
    assert "baseline oracle" in capsys.readouterr().out
    trials = bank.results_dir(bank_data) / "bank-oracle" / "all" / "trials.jsonl"
    assert cli.main(["eval", "bank-score", str(trials), "--split", "all"]) == 0
    assert cli.main(["eval", "bank-score", str(trials), "--split", "dev"]) == 2  # wrong split
    assert "run is invalid" in capsys.readouterr().err


def test_an_unapproved_harness_refuses_bank_scoring(
    bank_data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "APPROVAL_FILE", tmp_path / "none.sha256")
    with pytest.raises(harness.HarnessNotApproved):
        bank.run_baseline("oracle", "all", bank_data, False)
