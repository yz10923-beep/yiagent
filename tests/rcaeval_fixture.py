"""Tiny synthetic RCAEval cases in the real on-disk layout, for tests without the download.

Each case has one minute of metrics around the onset, a few log lines and spans, the
hazards the loader must handle (null and NaN metric cells, ns trace times, an ISO time
column), and a root_cause.txt with a secret line that must never be loaded."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ONSET_S = 1_705_935_125
SECRET = "SECRET-ROOT-CAUSE-LINE never loaded"


def make_case(
    data_dir: Path,
    name: str,
    *,
    logs: bool = True,
    traces: bool = True,
    trace_time: str = "real",
) -> Path:
    """The schemas are the real ones (`yi data inspect` on the VM, 2026-10-11): logs carry an
    int64 `timestamp` in seconds; traces carry a useless `time` string ('12:02', HH:MM) and
    the real start in `startTime` (us) and `startTimeMillis`; `statusCode` is null in Train
    Ticket. trace_time="iso" replaces the trace times with ISO strings only;
    "time-of-day-only" leaves only the HH:MM string, so no trace time is usable."""
    d = data_dir / name
    d.mkdir(parents=True)
    (d / "inject_time.txt").write_text(f"{ONSET_S}\n")
    (d / "root_cause.txt").write_text(SECRET + "\n")
    times = list(range(ONSET_S - 30, ONSET_S + 31))  # 61 one-second rows
    pq.write_table(
        pa.table(
            {
                "time": pa.array(times, pa.int64()),
                "frontend_cpu": [0.1] * 61,
                "backend_latency-50": [None] * 10 + [5.0] * 51,  # 10 unobserved cells
                "backend_mem": [float("nan")] + [1.0] * 60,  # 1 NaN cell
            }
        ),
        d / "metrics.parquet",
    )
    if logs:
        pq.write_table(
            pa.table(
                {
                    "timestamp": pa.array([ONSET_S + i for i in range(5)], pa.int64()),
                    "container_name": ["frontend", "backend", "backend", "frontend", "backend"],
                    "message": [f"GET /cart 200 {i}ms" for i in range(4)] + ["ERROR timeout"],
                }
            ),
            d / "logs.parquet",
        )
    if traces:
        start_us = [ONSET_S * 1_000_000 + i * 1_000_000 + 952 for i in range(3)]
        cols: dict[str, object] = {
            "time": ["12:02", "12:02", "12:03"],
            "traceID": ["0001", "0001", "0002"],  # leading zeros must survive
            "spanID": ["a", "b", "c"],
            "serviceName": ["frontend", "backend", "frontend"],
            "methodName": pa.array([None, None, None], pa.string()),
            "operationName": ["GET /cart", "CartService/Get", "GET /"],
            "parentSpanID": [None, "a", None],
            "startTimeMillis": pa.array([u // 1000 for u in start_us], pa.int64()),
            "startTime": pa.array(start_us, pa.int64()),
            "duration": pa.array([1500, 900, 300], pa.int64()),
            "statusCode": pa.array([0, 500, None], pa.int64()),
        }
        if trace_time in ("iso", "time-of-day-only"):
            del cols["startTime"], cols["startTimeMillis"]
        if trace_time == "iso":
            cols["time"] = [f"2024-01-22T14:52:{5 + i:02d}" for i in range(3)]
        pq.write_table(pa.table(cols), d / "traces.parquet")
    return d


def make_index(data_dir: Path, rows: list[dict[str, object]]) -> None:
    pq.write_table(pa.Table.from_pylist(rows), data_dir / "cases.parquet")
