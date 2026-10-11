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
    trace_time: str = "ns",
    log_time: str = "ns",
) -> Path:
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
        cols: dict[str, object] = {
            "timestamp": pa.array([(ONSET_S + i) * 1_000_000_000 for i in range(5)]),
            "container_name": ["frontend", "backend", "backend", "frontend", "backend"],
            "message": [f"GET /cart 200 {i}ms" for i in range(4)] + ["ERROR timeout"],
        }
        if log_time in ("tt", "time-of-day-only"):
            # Seen on the VM in Train Ticket's logs: `timestamp` = '12:02', a time of day.
            # Whether a numeric time column sits beside it there is not known yet ("tt").
            cols["timestamp"] = ["12:02", "12:02", "12:03", "12:03", "12:03"]
        if log_time == "tt":
            cols["time"] = pa.array([ONSET_S + i for i in range(5)], pa.int64())
        pq.write_table(pa.table(cols), d / "logs.parquet")
    if traces:
        if trace_time == "iso":
            tt: pa.Array = pa.array([f"2024-01-22T14:52:{5 + i:02d}" for i in range(3)])
        else:
            tt = pa.array([(ONSET_S + i) * 1_000_000_000 for i in range(3)], pa.int64())
        pq.write_table(
            pa.table(
                {
                    "time": tt,
                    "traceID": ["0001", "0001", "0002"],  # leading zeros must survive
                    "spanID": ["a", "b", "c"],
                    "parentSpanID": [None, "a", None],
                    "serviceName": ["frontend", "backend", "frontend"],
                    "duration": [1500, 900, float("nan")],
                    "statusCode": [200, 500, 200],
                }
            ),
            d / "traces.parquet",
        )
    return d


def make_index(data_dir: Path, rows: list[dict[str, object]]) -> None:
    pq.write_table(pa.Table.from_pylist(rows), data_dir / "cases.parquet")
