from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from tests.rcaeval_fixture import ONSET_S, make_case
from yi_agent.data.telemetry import (
    FileStats,
    TelemetryError,
    TimeNormalizer,
    iter_docs,
    read_window,
    source_paths,
    unit_of,
)

T = 1_705_935_125  # 2024-01-22T14:52:05Z


@pytest.mark.parametrize(
    ("value", "unit"),
    [(T, "s"), (T * 1_000, "ms"), (T * 1_000_000, "us"), (T * 1_000_000_000, "ns")],
)
def test_epoch_unit_is_read_from_magnitude(value: int, unit: str) -> None:
    assert unit_of(value)[0] == unit
    assert TimeNormalizer()(pa.array([value])) == [T * 1000]


def test_not_an_epoch_is_an_error() -> None:
    with pytest.raises(TelemetryError):
        unit_of(12345)


def test_nan_times_are_missing_not_a_crash() -> None:
    assert TimeNormalizer()(pa.array([float("nan"), T + 0.5])) == [None, T * 1000 + 500]


def test_float_seconds_keep_milliseconds() -> None:
    assert TimeNormalizer()(pa.array([T + 0.25])) == [T * 1000 + 250]


def test_iso_strings_are_utc_when_naive() -> None:
    norm = TimeNormalizer()
    got = norm(pa.array(["2024-01-22T14:52:05", "2024-01-22T14:52:05Z", None]))
    assert got == [T * 1000, T * 1000, None]
    assert norm.unit == "string"


def test_arrow_timestamps_become_ms() -> None:
    col = pa.array([T * 1_000_000], pa.timestamp("us", tz="UTC"))
    assert TimeNormalizer()(col) == [T * 1000]


def test_unit_is_fixed_per_file() -> None:
    # A later batch with a small value must not switch the unit.
    norm = TimeNormalizer()
    norm(pa.array([T * 1_000_000_000]))
    assert norm(pa.array([T * 1_000_000_000 + 5_000_000])) == [T * 1000 + 5]


def test_logs_become_one_doc_per_line_with_source_columns_only(tmp_path: Path) -> None:
    d = make_case(tmp_path, "re2ob_checkoutservice_cpu_1")
    stats = FileStats()
    docs = list(iter_docs("logs", d / "logs.parquet", stats))
    assert stats.rows == stats.docs == len(docs) == 5
    assert stats.time_unit == "ns"
    assert set(docs[0]) == {"@timestamp", "timestamp", "container_name", "message"}
    assert docs[0]["@timestamp"] == ONSET_S * 1000


def test_traces_keep_string_ids_and_drop_nan_fields(tmp_path: Path) -> None:
    d = make_case(tmp_path, "re2ob_checkoutservice_cpu_1")
    docs = list(iter_docs("traces", d / "traces.parquet", FileStats()))
    assert [x["traceID"] for x in docs] == ["0001", "0001", "0002"]
    assert "parentSpanID" not in docs[0] and docs[1]["parentSpanID"] == "a"
    assert "duration" not in docs[2]  # NaN: the field is absent, the span is kept


def test_iso_trace_times_load(tmp_path: Path) -> None:
    d = make_case(tmp_path, "re2ob_checkoutservice_cpu_1", trace_time="iso")
    docs = list(iter_docs("traces", d / "traces.parquet", FileStats()))
    assert docs[0]["@timestamp"] == T * 1000


def test_metrics_go_long_skipping_unobserved_and_nan_cells(tmp_path: Path) -> None:
    d = make_case(tmp_path, "re2ob_checkoutservice_cpu_1")
    stats = FileStats()
    docs = list(iter_docs("metrics", d / "metrics.parquet", stats))
    # 3 metrics x 61 rows, minus 10 nulls (no document, as at rest) and 1 NaN (counted)
    assert len(docs) == stats.docs == 3 * 61 - 10 - 1
    assert stats.skipped_nonfinite == 1 and stats.rows == 61
    assert set(docs[0]) == {"@timestamp", "metric", "value"}
    assert {x["metric"] for x in docs} == {"frontend_cpu", "backend_latency-50", "backend_mem"}


def test_window_and_missing_sources(tmp_path: Path) -> None:
    d = make_case(tmp_path, "re2ss_carts_cpu_1", traces=False)
    assert set(source_paths(d)) == {"logs", "metrics"}
    w = read_window(d)
    assert w.onset_ms == ONSET_S * 1000
    assert (w.start_ms, w.end_ms) == ((ONSET_S - 30) * 1000, (ONSET_S + 30) * 1000)
    assert w.sources == ("logs", "metrics")
