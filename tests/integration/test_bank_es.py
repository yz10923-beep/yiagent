"""OpenRCA Bank cases cut from the synthetic download, loaded into a real Elasticsearch
through `yi data bank load`, as on the VM. Run with `make es-test`."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

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
