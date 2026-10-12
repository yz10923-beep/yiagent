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


FAMILIES = ("cpu", "mem", "diskio", "socket", "latency-50", "latency-90")
# The metric family the paper names as each fault's root-cause indicator.
INDICATOR = {"cpu": "cpu", "mem": "mem", "disk": "diskio", "socket": "socket"}
NOISE = [0.9, 1.1, 1.0, 1.2, 0.8, 1.05, 0.95, 1.15, 0.85, 1.0]  # gives every series an IQR


def make_fault_case(data_dir: Path, name: str, services: list[str]) -> Path:
    """A case whose answer is planted where the paper says to look: the root cause's
    resource metric jumps after the onset (cpu, mem, disk, socket), its latency jumps
    (delay, loss), it emits error spans (loss, code-level) and an exception (code-level).
    Every other series is stationary noise. 120 one-second rows on each side of the onset."""
    from yi_agent.data.rcaeval import parse_case_name

    label = parse_case_name(name)
    rc, fault = label.service, label.fault
    d = data_dir / name
    d.mkdir(parents=True)
    (d / "inject_time.txt").write_text(f"{ONSET_S}\n")
    times = list(range(ONSET_S - 120, ONSET_S + 120))
    cols: dict[str, object] = {"time": pa.array(times, pa.int64())}
    for s in services:
        for fam in FAMILIES:
            base = [10.0 * NOISE[i % len(NOISE)] for i in range(len(times))]
            jump = (s == rc) and (
                fam == INDICATOR.get(fault)
                or (fault in ("delay", "loss") and fam.startswith("latency"))
            )
            cols[f"{s}_{fam}"] = [
                v + (500.0 if jump and t >= ONSET_S else 0.0)
                for v, t in zip(base, times, strict=True)
            ]
    pq.write_table(pa.table(cols), d / "metrics.parquet")
    code_level = fault.startswith("f")
    log_rows: dict[str, list[object]] = {"timestamp": [], "container_name": [], "message": []}
    for i, t in enumerate(range(ONSET_S - 60, ONSET_S + 60, 10)):
        for s in services:
            log_rows["timestamp"].append(t)
            log_rows["container_name"].append(s)
            log_rows["message"].append(f"handled request {i} in 12ms")
    if code_level:
        log_rows["timestamp"].append(ONSET_S + 5)
        log_rows["container_name"].append(rc)
        log_rows["message"].append(
            "java.lang.NullPointerException: price is null\n\tat svc.Cart.total(Cart.java:42)"
        )
    pq.write_table(pa.table(log_rows), d / "logs.parquet")
    spans: dict[str, list[object]] = {
        k: []
        for k in (
            "time",
            "traceID",
            "spanID",
            "parentSpanID",
            "serviceName",
            "operationName",
            "startTime",
            "duration",
            "statusCode",
        )
    }
    # One trace per step: services[0] calls services[1] calls ... Each span spends 1 ms
    # itself; with a delay the root cause spends 200 ms more, and every caller waits.
    for i, t in enumerate(range(ONSET_S - 60, ONSET_S + 60, 5)):
        own = [
            1000 + (200_000 if s == rc and fault == "delay" and t >= ONSET_S else 0)
            for s in services
        ]
        for j, s in enumerate(services):
            erroring = s == rc and t >= ONSET_S and (fault == "loss" or code_level)
            spans["time"].append("12:02")
            spans["traceID"].append(f"t{i:04x}")
            spans["spanID"].append(f"{i:04x}{j:04x}")
            spans["parentSpanID"].append(f"{i:04x}{j - 1:04x}" if j else None)
            spans["serviceName"].append(s)
            spans["operationName"].append(f"{s}/Handle")
            spans["startTime"].append(t * 1_000_000 + j * 100)
            spans["duration"].append(sum(own[j:]))
            spans["statusCode"].append(500 if erroring else 200)
    pq.write_table(
        pa.table(
            {
                **spans,
                "startTime": pa.array(spans["startTime"], pa.int64()),
                "duration": pa.array(spans["duration"], pa.int64()),
                "statusCode": pa.array(spans["statusCode"], pa.int64()),
            }
        ),
        d / "traces.parquet",
    )
    return d
