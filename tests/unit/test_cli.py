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


def test_reproducing_on_a_half_extracted_input_names_the_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An interrupted extract left a case directory without inject_time.txt (VM, 2026-10-12).
    (tmp_path / "re2tt_ts-auth-service_cpu_1").mkdir()
    argv = ["eval", "reproduce-baro", "--data-dir", str(tmp_path)]
    assert cli.main([*argv, "--metrics-file", "simple_metrics.csv"]) == 2
    err = capsys.readouterr().err
    assert "90 of 90 cases lack simple_metrics.csv or inject_time.txt" in err
    assert "Traceback" not in err


@pytest.mark.parametrize(
    ("argv", "fn"),
    [
        (["agent", "run", "--split", "dev", "--model", "policy:metric-top"], "cmd_agent_run"),
        (["agent", "run", "--placebo", "--variant", "p"], "cmd_agent_run"),
        (["agent", "compare", "baro", "sonnet", "--group", "all", "re2-tt"], "cmd_agent_compare"),
    ],
)
def test_every_agent_command_reaches_its_own_handler(argv: list[str], fn: str) -> None:
    # A subcommand added in the wrong place once took `run`'s handler (VM, 2026-10-12).
    args = cli.build_parser().parse_args(argv)
    assert args.fn.__name__ == fn


def test_every_subcommand_has_a_handler() -> None:
    import argparse

    def leaves(p: argparse.ArgumentParser, path: str) -> list[tuple[str, argparse.ArgumentParser]]:
        subs = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs:
            return [(path, p)]
        return [
            leaf
            for a in subs
            for cmd, sub in a.choices.items()
            for leaf in leaves(sub, f"{path} {cmd}")
        ]

    found = leaves(cli.build_parser(), "yi")
    assert "yi data bank cut" in [path for path, _ in found]
    for path, leaf in found:
        assert leaf.get_default("fn") is not None, path
