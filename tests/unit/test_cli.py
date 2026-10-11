from __future__ import annotations

from pathlib import Path

import pytest

from yi_agent import cli
from yi_agent.eval import harness
from yi_agent.eval.report import Trial, write_trials


def test_scoring_refuses_an_unapproved_harness_with_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(harness, "APPROVAL_FILE", tmp_path / "harness.sha256")  # none yet
    monkeypatch.setenv("YI_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["eval", "baseline", "empty", "--split", "dev_small"]) == 2
    err = capsys.readouterr().err
    assert "changed since it was approved" in err and "--approve-harness" in err
    assert "Traceback" not in err
    assert not (tmp_path / "harness.sha256").exists()  # refusing never approves


def test_an_incomplete_run_is_invalid_with_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    approval = tmp_path / "harness.sha256"
    approval.write_text(harness.harness_sha() + "\n")  # a scratch approval, not the repo's
    monkeypatch.setattr(harness, "APPROVAL_FILE", approval)
    trials = tmp_path / "trials.jsonl"
    write_trials([Trial(case_id="cdeadbeef00", status="infra_error", error="x")], trials)
    assert cli.main(["eval", "score", str(trials), "--split", "dev_small"]) == 2
    assert "run is invalid, not scored" in capsys.readouterr().err
