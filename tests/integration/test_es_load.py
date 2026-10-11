"""Loading a case into a real Elasticsearch: fresh indices named by opaque id, every count
verified, nothing left behind on failure. Run with `make es-test` (needs `make es-up`)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from elasticsearch import Elasticsearch

from tests.rcaeval_fixture import ONSET_S, SECRET, make_case
from yi_agent.data import es_load, telemetry
from yi_agent.data.es_load import LoadError, ensure_loaded, index_name, load_case, unload_case
from yi_agent.data.rcaeval import parse_case_name

pytestmark = pytest.mark.es

NAME = "re2ob_checkoutservice_cpu_1"
CASE_ID = parse_case_name(NAME).id


@pytest.fixture
def es() -> Iterator[Elasticsearch]:
    client = Elasticsearch(os.environ.get("YI_ES_URL", "http://127.0.0.1:9200"), request_timeout=30)
    if not client.ping():
        pytest.fail("Elasticsearch not reachable; run `make es-up`")
    unload_case(client, CASE_ID)
    yield client
    unload_case(client, CASE_ID)


def our_indices(es: Elasticsearch) -> set[str]:
    return set(es.indices.get(index=f"yi-{CASE_ID}-*", allow_no_indices=True))


def test_case_loads_into_fresh_indices_with_every_document(
    es: Elasticsearch, tmp_path: Path
) -> None:
    report = load_case(es, CASE_ID, make_case(tmp_path, NAME))
    assert report.indices == {
        index_name(CASE_ID, "logs"): 5,
        index_name(CASE_ID, "traces"): 3,
        index_name(CASE_ID, "metrics"): 3 * 61 - 10 - 1,
    }
    assert our_indices(es) == set(report.indices)
    # searchable: full text on messages, exact keywords on services, dates on @timestamp
    hits = es.search(index=index_name(CASE_ID, "logs"), query={"match": {"message": "timeout"}})
    assert hits["hits"]["total"]["value"] == 1
    errors = es.count(index=index_name(CASE_ID, "traces"), query={"term": {"statusCode": 500}})
    assert errors["count"] == 1
    after = es.count(
        index=index_name(CASE_ID, "metrics"),
        query={"range": {"@timestamp": {"gte": ONSET_S * 1000}}},
    )
    assert after["count"] == 3 * 31  # 31 rows from the onset on; the null and NaN cells are earlier
    mapping = es.indices.get_mapping(index=index_name(CASE_ID, "traces"))
    props = mapping[index_name(CASE_ID, "traces")]["mappings"]["properties"]
    assert props["traceID"]["type"] == "keyword"
    assert props["startTime"]["type"] == "long"  # us since the epoch: needs 64 bits
    # nothing from the directory: no name, no root_cause.txt
    for kind in ("logs", "traces"):
        for q in (NAME, SECRET):
            r = es.count(
                index=index_name(CASE_ID, kind), query={"query_string": {"query": f'"{q}"'}}
            )
            assert r["count"] == 0


def test_decimals_are_mapped_as_double_not_float(es: Elasticsearch) -> None:
    # Elasticsearch's default maps JSON decimals to 32-bit float, which rounds silently.
    name = index_name(CASE_ID, "traces")
    es.indices.create(index=name, mappings=es_load.MAPPINGS["traces"])
    es.index(index=name, document={"@timestamp": ONSET_S * 1000, "ratio": 0.123456789012})
    props = es.indices.get_mapping(index=name)[name]["mappings"]["properties"]
    assert props["ratio"]["type"] == "double"


def test_a_complete_load_is_reused_and_an_unfinished_one_is_not(
    es: Elasticsearch, tmp_path: Path
) -> None:
    d = make_case(tmp_path, NAME)
    first = ensure_loaded(es, CASE_ID, d)
    assert not first.reused and first.docs == 5 + 3 + 172
    again = ensure_loaded(es, CASE_ID, d)
    assert again.reused and again.indices == first.indices
    # An index without the completion marker (a load that died) is reloaded.
    es.indices.put_mapping(index=index_name(CASE_ID, "logs"), meta={})
    assert not ensure_loaded(es, CASE_ID, d).reused
    # So is one from another loader version.
    es.indices.put_mapping(
        index=index_name(CASE_ID, "metrics"),
        meta={"yi_loader": es_load.LOADER_VERSION + 1, "complete": True, "docs": 172},
    )
    assert not ensure_loaded(es, CASE_ID, d).reused
    # And one whose count no longer matches its marker.
    es.delete_by_query(
        index=index_name(CASE_ID, "logs"), query={"match_all": {}}, refresh=True, max_docs=1
    )
    assert not ensure_loaded(es, CASE_ID, d).reused
    assert ensure_loaded(es, CASE_ID, d).reused


def test_reload_replaces_and_unload_deletes(es: Elasticsearch, tmp_path: Path) -> None:
    d = make_case(tmp_path, NAME)
    load_case(es, CASE_ID, d)
    second = load_case(es, CASE_ID, d)
    assert second.indices[index_name(CASE_ID, "logs")] == 5  # replaced, not doubled
    unload_case(es, CASE_ID)
    assert our_indices(es) == set()


def test_missing_sources_get_no_index(es: Elasticsearch, tmp_path: Path) -> None:
    report = load_case(es, CASE_ID, make_case(tmp_path, "re2ss_carts_cpu_1", traces=False))
    assert index_name(CASE_ID, "traces") not in report.indices
    assert our_indices(es) == set(report.indices)


def test_a_rejected_document_fails_the_load_and_leaves_nothing(
    es: Elasticsearch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = telemetry.iter_docs

    def bad_metrics(kind, path, stats):  # type: ignore[no-untyped-def]
        yield from real(kind, path, stats)
        if kind == "metrics":
            stats.docs += 1
            yield {"@timestamp": ONSET_S * 1000, "metric": "x", "value": "not a number"}

    monkeypatch.setattr(es_load, "iter_docs", bad_metrics)
    with pytest.raises(LoadError, match="rejected"):
        load_case(es, CASE_ID, make_case(tmp_path, NAME))
    assert our_indices(es) == set()


def test_a_count_mismatch_fails_the_load(
    es: Elasticsearch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = telemetry.iter_docs

    def overcounted(kind, path, stats):  # type: ignore[no-untyped-def]
        yield from real(kind, path, stats)
        stats.docs += 1  # the source said one more document than was indexed

    monkeypatch.setattr(es_load, "iter_docs", overcounted)
    with pytest.raises(LoadError, match="produced"):
        load_case(es, CASE_ID, make_case(tmp_path, NAME))
    assert our_indices(es) == set()
