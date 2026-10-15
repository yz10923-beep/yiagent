"""OpenRCA Bank cases cut from the synthetic download, loaded into a real Elasticsearch
through `yi data bank load`, as on the VM. Run with `make es-test`."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from elasticsearch import Elasticsearch

from tests.openrca_fixture import make_bank
from tests.unit.test_openrca import _labels, _use_fixture
from yi_agent.data import openrca
from yi_agent.data.es_load import index_name, unload_case

pytestmark = pytest.mark.es


@pytest.fixture
def es() -> Iterator[Elasticsearch]:
    client = Elasticsearch(os.environ.get("YI_ES_URL", "http://127.0.0.1:9200"), request_timeout=30)
    if not client.ping():
        pytest.fail("Elasticsearch not reachable; run `make es-up`")
    yield client


def test_bank_cases_load_with_every_document_and_are_reused(
    es: Elasticsearch,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from yi_agent import cli

    data_dir = tmp_path / "data"
    fx = make_bank(openrca.raw_dir(data_dir))
    monkeypatch.setenv("YI_DATA_DIR", str(data_dir))
    _use_fixture(fx, monkeypatch, tmp_path)
    ids = [q.id for q in _labels(fx)]
    for i in ids:
        unload_case(es, i)
    try:
        assert cli.main(["data", "bank", "cut"]) == 0
        capsys.readouterr()
        argv = ["data", "bank", "load", "--split", "all", "--keep"]
        assert cli.main(argv) == 0
        out = capsys.readouterr().out
        assert f"loaded {len(ids) * (60 + 120 + 60 * 8)} docs" in out
        for i in ids:
            assert es.count(index=index_name(i, "traces"))["count"] == 120
        assert cli.main(argv) == 0
        assert f"{len(ids)} of {len(ids)} cases were already loaded" in capsys.readouterr().out
        assert cli.main(["data", "bank", "load", ids[0]]) == 0  # without --keep: unloaded
        assert not es.indices.exists(index=index_name(ids[0], "logs"))
    finally:
        for i in ids:
            unload_case(es, i)


class BankInvestigator:
    """A stand-in model: one telemetry call, then a report built from the alert alone
    (the window's start, the stated number of failures), through the real loop and tools."""

    def __init__(self, **_: object) -> None:
        self.model = "fake-bank-investigator"
        self.calls = 0

    async def complete(
        self, *, system: str, messages: list[Any], tools: list[Any], on_text: Any = None
    ) -> Any:
        from tests.fakes import tool_response

        text = messages[0]["content"][0]["text"]
        self.calls += 1
        if len(messages) == 1:
            resp = tool_response(("t1", "metric_changes", {"limit": 5}))
        else:
            assert "metric" in json.dumps(messages[-1]["content"])  # the tool answered
            start = re.search(r"Between (\S+ \S+) and", text)
            assert start
            n = 1 if "one failure" in text else int(re.search(r"(\d+) failures", text)[1])  # type: ignore[index]
            report = {
                "failures": [
                    {"time": start[1], "component": "Tomcat01", "reason": "high CPU usage"}
                ]
                * n,
                "evidence": [],
                "summary": "scripted",
            }
            resp = tool_response(("t2", "submit_report", report))
        return resp.model_copy(update={"model": self.model})


def test_the_bank_agent_runs_end_to_end_through_the_cli(
    es: Elasticsearch,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from yi_agent import cli
    from yi_agent.eval import harness

    data_dir = tmp_path / "data"
    fx = make_bank(openrca.raw_dir(data_dir))
    monkeypatch.setenv("YI_DATA_DIR", str(data_dir))
    _use_fixture(fx, monkeypatch, tmp_path)
    approval = tmp_path / "harness.sha256"
    approval.write_text(harness.harness_sha() + "\n")
    monkeypatch.setattr(harness, "APPROVAL_FILE", approval)
    monkeypatch.setattr(cli, "AnthropicProvider", BankInvestigator)
    ids = [q.id for q in _labels(fx)]
    for i in ids:
        unload_case(es, i)
    try:
        assert cli.main(["data", "bank", "check"]) == 0
        assert cli.main(["data", "bank", "cut"]) == 0
        capsys.readouterr()
        argv = ["agent", "bank-run", "--split", "all", "--variant", "bank-smoke"]
        assert cli.main([*argv, "--limit", "2"]) == 0
        assert "partial run: 2 of 6" in capsys.readouterr().out
        assert cli.main(argv) == 0  # resumes, then scores with OpenRCA's grader
        out = capsys.readouterr().out
        assert "strict" in out and "outcomes {'submitted': 6}" in out
        row = next(line for line in out.splitlines() if line.startswith("all "))
        assert 0.0 < float(row.split()[3]) < 1.0  # mean: right on Tomcat01/cpu, wrong elsewhere
        run_dir = openrca.raw_dir(data_dir) / "results" / "bank-smoke" / "all"
        assert json.loads((run_dir / "run.json").read_text())["benchmark"] == "openrca-bank"
        events = json.loads(next((run_dir / "events").iterdir()).read_text())
        first = events["messages"][0]["content"][0]["text"]
        assert "banking platform" in first and "t+0s (the onset)" in first
        assert cli.main([*argv, "--max-steps", "9"]) == 2  # another condition: refused
        assert (
            cli.main(
                [
                    "agent",
                    "bank-run",
                    "--split",
                    "all",
                    "--variant",
                    "bank-unload",
                    "--unload-after",
                ]
            )
            == 0
        )
        assert not any(es.indices.exists(index=index_name(i, "traces")) for i in ids)
        # the placebo: the same run, nothing at or after the real window's start visible
        assert (
            cli.main(
                ["agent", "bank-run", "--split", "all", "--variant", "bank-placebo", "--placebo"]
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "outcomes {'submitted': 6}" in out
        run_dir = openrca.raw_dir(data_dir) / "results" / "bank-placebo" / "all"
        assert json.loads((run_dir / "run.json").read_text())["placebo"]
        from yi_agent.agent.bank import case_tools
        from yi_agent.data.es_load import ensure_loaded

        for q in _labels(fx):
            ensure_loaded(es, q.id, openrca.cases_dir(data_dir) / q.id)
            tools = case_tools(es, q, ("logs", "traces", "metrics"), placebo=True)
            for kind in ("logs", "traces", "metrics"):
                r = tools.data.search(kind, size=1, sort=[{"@timestamp": "desc"}])
                assert r["hits"]["hits"][0]["_source"]["@timestamp"] < q.start_ms, (q.id, kind)
            unload_case(es, q.id)
    finally:
        for i in ids:
            unload_case(es, i)
