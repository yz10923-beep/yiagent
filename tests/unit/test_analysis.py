"""The S4 fault-signals diagnostic, run through the CLI as Yi runs it, on fixture cases
under the real dev names with the answers planted."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.rcaeval_fixture import make_fault_case
from yi_agent import analysis, cli
from yi_agent.data.rcaeval import LABELS_PATH, SPLIT_PATH, read_labels
from yi_agent.data.split import read_split
from yi_agent.eval.report import Report, Trial, write_trials


@pytest.fixture(scope="module")
def dev_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    by_id = {c.id: c for c in read_labels(LABELS_PATH)}
    data_dir = tmp_path_factory.mktemp("data")
    for k, i in enumerate(read_split(SPLIT_PATH).dev):
        c = by_id[i]
        services = ["alpha", "beta", "gamma"]
        services.insert(k % 4, c.service)
        make_fault_case(data_dir, c.name, services)
    return data_dir


def test_fault_signals_runs_through_the_cli_and_shows_the_planted_signals(
    dev_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    by_id = {c.id: c for c in read_labels(LABELS_PATH)}
    ids = read_split(SPLIT_PATH).dev
    # an agent that always says "cpu", with a summary that says why
    trials = [
        Trial(case_id=i, report=Report(case_id=i, fault_type="cpu", summary="cpu rose a lot"))
        for i in ids
    ]
    write_trials(trials, tmp_path / "v" / "dev" / "trials.jsonl")
    monkeypatch.setattr(analysis, "RESULTS_DIR", tmp_path)
    monkeypatch.setenv("YI_DATA_DIR", str(dev_dir))
    assert cli.main(["eval", "fault-signals", "--split", "dev", "--variant", "v"]) == 0
    out = capsys.readouterr().out
    medians = out.split("median per fault")[1].split("the agent's reasoning")[0]
    rows = {line.split()[0]: line.split() for line in medians.splitlines()[2:] if line.strip()}
    cpu_n = sum(by_id[i].fault == "cpu" for i in ids)
    assert rows["cpu"][2] == f"{cpu_n}/{cpu_n}"  # "cpu" is right exactly on the cpu cases
    assert rows["socket"][2] == "0/" + rows["socket"][1]
    head = medians.splitlines()[1].split()
    assert len(head) == len(rows["cpu"])  # every column name stands alone
    col = {name: k for k, name in enumerate(head)}
    assert float(rows["cpu"][col["cpu_up"]]) > 100  # the planted +500 jump, robust-scaled
    assert float(rows["mem"][col["cpu_up"]]) < 10  # nothing planted there
    assert int(rows["f1"][col["errl_a"]]) >= 1 and int(rows["f1"][col["errl_b"]]) == 0
    assert int(rows["loss"][col["espan_a"]]) > 0
    assert re.search(r"socket -> cpu: cpu rose a lot", out)


def test_nothing_the_agent_runs_imports_the_label_reading_analysis() -> None:
    src = Path(__file__).resolve().parents[2] / "src" / "yi_agent" / "agent"
    for f in src.glob("*.py"):
        assert "analysis" not in f.read_text(), f


def test_confusion_runs_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    by_id = {c.id: c for c in read_labels(LABELS_PATH)}
    ids = read_split(SPLIT_PATH).dev
    # names the truth on cpu cases, "mem" on everything else, and nothing on one case
    trials = [
        Trial(
            case_id=i,
            report=Report(case_id=i, fault_type="cpu" if by_id[i].fault == "cpu" else "mem"),
        )
        for i in ids[1:]
    ] + [Trial(case_id=ids[0], report=Report(case_id=ids[0]))]
    write_trials(trials, tmp_path / "v" / "dev" / "trials.jsonl")
    monkeypatch.setattr(analysis, "RESULTS_DIR", tmp_path)
    assert cli.main(["eval", "confusion", "v", "missing", "--split", "dev"]) == 0
    out = capsys.readouterr().out
    n = len(ids)
    resource = sum(analysis.FAMILY[by_id[i].fault] == "resource" for i in ids[1:])
    exact = sum(by_id[i].fault in ("cpu", "mem") for i in ids[1:])
    assert f"v: fault right {exact}/{n}, family right {resource}/{n}" in out
    assert re.search(r"socket \(resource\) -> mem \d+", out)
    assert "none 1" in out
    assert "missing: no trials" in out


def test_agreement_counts_stable_and_flipping_cases_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    by_id = {c.id: c for c in read_labels(LABELS_PATH)}
    ids = read_split(SPLIT_PATH).dev
    flip = ids[:5]  # run a is right on these, run b names "cpu" (wrong unless truth is cpu)

    def run(name: str, flipped: bool) -> None:
        trials = [
            Trial(
                case_id=i,
                report=Report(
                    case_id=i, fault_type="cpu" if flipped and i in flip else by_id[i].fault
                ),
            )
            for i in ids
        ]
        write_trials(trials, tmp_path / name / "dev" / "trials.jsonl")

    run("a", False)
    run("b", True)
    monkeypatch.setattr(analysis, "RESULTS_DIR", tmp_path)
    assert cli.main(["eval", "agreement", "a", "b", "--split", "dev"]) == 0
    out = capsys.readouterr().out
    changed = [i for i in flip if by_id[i].fault != "cpu"]
    n = len(ids)
    assert f"a {n}/{n}, b {n - len(changed)}/{n}" in out
    assert f"right in every run {n - len(changed)}/{n}, wrong in every run 0/{n}" in out
    assert f"answer differs between runs {len(changed)}/{n}" in out
    assert cli.main(["eval", "agreement", "a", "nope", "--split", "dev"]) == 0
    assert "nope: no trials" in capsys.readouterr().out
