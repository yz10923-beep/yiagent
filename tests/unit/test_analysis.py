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
