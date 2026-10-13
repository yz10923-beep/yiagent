"""OpenRCA Bank (S5): queries, labels, the split, the download, and cutting each query's
window out of the day-long CSVs into the case layout the rest of the pipeline reads.

A case is one query of `Bank/query.csv`: a 30-minute window (UTC+8, as OpenRCA states),
the number of failures in it, and what to find (time, component, reason). Its labels are
the query's `scoring_points`, kept verbatim for OpenRCA's own grader, plus the full
`record.csv` rows inside the window; they live next to the download, never in the repo,
and their hash is pinned here. Everything here is harness-side: the agent only ever
gets an opaque id and its window's telemetry.

The cut writes `logs.parquet`, `traces.parquet` and `metrics.parquet` per case, in the
column names of an RCAEval case (docs/ROADMAP.md, S5 decisions), so loading, the tools
and the graders' readers work unchanged. The window's telemetry starts BASELINE_MS before
the query's window and stops at its end: nothing after the window is visible.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import re
import shutil
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pcsv
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict

# A mirror of the Google Drive folder Microsoft's README links (unreachable from the
# cloud container). Its query.csv equals the instructions of Microsoft's own archived
# Bank run (rca/archive/agent-Bank.csv) 136 of 136, and both small files are pinned below.
HF_REPO = "cdreetz/OpenRCA"
SYSTEM = "Bank"
TZ = timezone(timedelta(hours=8))  # "use the UTC+8 time zone" (OpenRCA's Bank prompt)
ID_SALT = "yi-openrca-bank-v1"  # part of the frozen labels: changing it changes every id
_REPO_ROOT = Path(__file__).resolve().parents[3]
# The labels are built from the download on the VM (`yi data bank check`) and never
# committed: Bank's data is for private evaluation (S5 decisions). Only their hash is.
LABELS_SHA256 = "71248d1f0cda87533cb7e9ba3bf35d7e14f0658d037aba4cf9b89a662c70f6e6"
SPLIT_PATH = _REPO_ROOT / "evals" / "split_bank.json"
SPLIT_SEED = 20261013
DEV_SHARE = 0.25
BASELINE_MS = 30 * 60 * 1000
WINDOW_MS = 30 * 60 * 1000

QUERY_SHA256 = "605d3b5c863928caf1611d97858379765858595e150efad2f77d2f733774f588"
RECORD_SHA256 = "eea788a8c6d07d5d9567067d1e740fe71f3221d8357f0e12e1c643c42cbd15f7"
# Bytes of every telemetry file, from the mirror's listing (2026-10-13).
_SIZES = {  # day: (log_service, metric_app, metric_container, trace_span)
    "2021_03_04": (214194991, 617331, 86066591, 1256254711),
    "2021_03_05": (76395542, 278349, 85884730, 561193657),
    "2021_03_06": (328804568, 639422, 84110809, 2632745307),
    "2021_03_07": (416743389, 639097, 84638758, 2474565687),
    "2021_03_09": (375183054, 673884, 83866542, 2377970111),
    "2021_03_10": (434785428, 639571, 84793705, 2551857873),
    "2021_03_12": (438163539, 638751, 83447568, 2556233870),
    "2021_03_23": (447369934, 629656, 77007359, 2469840756),
    "2021_03_24": (435708516, 640570, 76856235, 2484272191),
    "2021_03_25": (447317896, 633446, 89559550, 2578501342),
}
SOURCES: tuple[str, ...] = (
    "log/log_service.csv",
    "metric/metric_app.csv",
    "metric/metric_container.csv",
    "trace/trace_span.csv",
)
TELEMETRY_SIZES: dict[str, int] = {
    f"{SYSTEM}/telemetry/{day}/{src}": size
    for day, sizes in _SIZES.items()
    for src, size in zip(SOURCES, sizes, strict=True)
}

# OpenRCA's Bank prompt lists these as the possible answers; the agent may be told them.
REASONS = (
    "high CPU usage",
    "high memory usage",
    "network latency",
    "network packet loss",
    "high disk I/O read usage",
    "high disk space usage",
    "high JVM CPU load",
    "JVM Out of Memory (OOM) Heap",
)
COMPONENTS = (
    "apache01",
    "apache02",
    "Tomcat01",
    "Tomcat02",
    "Tomcat03",
    "Tomcat04",
    "MG01",
    "MG02",
    "IG01",
    "IG02",
    "Mysql01",
    "Mysql02",
    "Redis01",
    "Redis02",
)
ASKED = ("time", "component", "reason")

# OpenRCA's grader (main/evaluate.py) reads scoring points with exactly these patterns.
POINT_PATTERNS = {
    "component": re.compile(r"The (?:\d+-th|only) predicted root cause component is ([^\n]+)"),
    "reason": re.compile(r"The (?:\d+-th|only) predicted root cause reason is ([^\n]+)"),
    "time": re.compile(
        r"The (?:\d+-th|only) root cause occurrence time is within 1 minutes "
        r"\(i\.e\., <=1min\) of ([^\n]+)"
    ),
}
_MONTHS = (
    "January February March April May June July August September October November December"
).split()
_M = "|".join(_MONTHS)
# "On March 4, 2021, ... from 14:30 to 15:00", "... from 23:30 to March 7, 2021, at 00:00"
_WINDOW = re.compile(
    rf"({_M}) (\d{{1,2}}), (\d{{4}})\D*?(\d{{2}}):(\d{{2}})\D+?"
    rf"(?:({_M}) (\d{{1,2}}), (\d{{4}})\D*?)?(\d{{2}}):(\d{{2}})"
)


class Failure(BaseModel):
    model_config = ConfigDict(frozen=True)

    time_ms: int
    component: str
    reason: str


class BankQuery(BaseModel):
    """What the grader knows about one query. Never shown to the agent, except the window,
    the number of failures and what is asked, which the query itself states."""

    model_config = ConfigDict(frozen=True)

    id: str
    row: int  # position in query.csv
    task: str  # task_1 .. task_7
    start_ms: int
    end_ms: int
    asked: tuple[str, ...]  # a subset of ASKED, in that order
    failures: tuple[Failure, ...]  # record.csv rows in the window, in time order
    scoring_points: str  # verbatim: OpenRCA's grader reads exactly this


class BankSplit(BaseModel):
    seed: int
    salt: str
    dev: list[str]
    test: list[str]


def query_id(row: int) -> str:
    return "b" + hashlib.sha256(f"{ID_SALT}:{SYSTEM}:{row}".encode()).hexdigest()[:10]


def local_ms(s: str) -> int:
    """'2021-03-04 14:57:00' (UTC+8) as epoch ms."""
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp() * 1000)


def local_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M:%S")


def parse_window(instruction: str) -> tuple[int, int]:
    """(start, end) epoch ms of a query's window. A window ending at midnight may name no
    second date ('from 23:30 to 00:00'): it ends the next day."""
    m = _WINDOW.search(instruction)
    if m is None:
        raise ValueError(f"no time window in {instruction[:120]!r}")
    start = datetime(int(m[3]), _MONTHS.index(m[1]) + 1, int(m[2]), int(m[4]), int(m[5]), tzinfo=TZ)
    if m[6]:
        end = datetime(
            int(m[8]), _MONTHS.index(m[6]) + 1, int(m[7]), int(m[9]), int(m[10]), tzinfo=TZ
        )
    else:
        end = start.replace(hour=int(m[9]), minute=int(m[10]))
        if end <= start:
            end += timedelta(days=1)
    s, e = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    if e - s != WINDOW_MS:
        raise ValueError(f"window is {(e - s) / 60000:g} min, not 30: {instruction[:120]!r}")
    return s, e


def parse_points(scoring_points: str) -> dict[str, list[str]]:
    return {k: p.findall(scoring_points) for k, p in POINT_PATTERNS.items()}


def labels_from_csv(query_csv: str, record_csv: str) -> list[BankQuery]:
    """Every query with its record rows. Fails unless each window holds exactly as many
    records as the query has failures, and the scoring points name those records."""
    import csv

    records = sorted(
        (
            Failure(
                time_ms=round(float(r["timestamp"]) * 1000),
                component=r["component"],
                reason=r["reason"],
            )
            for r in csv.DictReader(io.StringIO(record_csv))
        ),
        key=lambda f: f.time_ms,
    )
    out: list[BankQuery] = []
    for row, q in enumerate(csv.DictReader(io.StringIO(query_csv))):
        start, end = parse_window(q["instruction"])
        points = parse_points(q["scoring_points"])
        n = max(len(v) for v in points.values())
        asked = tuple(k for k in ASKED if points[k])
        if not asked or any(len(points[k]) != n for k in asked):
            raise ValueError(f"row {row}: scoring points do not pair up: {points}")
        inside = tuple(f for f in records if start <= f.time_ms < end)
        if len(inside) != n:
            raise ValueError(f"row {row}: {n} failures asked, {len(inside)} records in window")
        # The points are paired by position but not always in time order: compare as sets.
        said = {
            tuple(points[k][i] if k != "time" else str(local_ms(points[k][i])) for k in asked)
            for i in range(n)
        }
        truth = {
            tuple(
                {"time": str(f.time_ms), "component": f.component, "reason": f.reason}[k]
                for k in asked
            )
            for f in inside
        }
        if said != truth:
            raise ValueError(f"row {row}: scoring points {said} disagree with records {truth}")
        out.append(
            BankQuery(
                id=query_id(row),
                row=row,
                task=q["task_index"],
                start_ms=start,
                end_ms=end,
                asked=asked,
                failures=inside,
                scoring_points=q["scoring_points"],
            )
        )
    if len({q.id for q in out}) != len(out):
        raise ValueError("query id collision; change ID_SALT")
    return out


def labels_json(queries: list[BankQuery]) -> str:
    """Canonical bytes: the pinned hash is over exactly this."""
    return json.dumps([q.model_dump() for q in queries], indent=1) + "\n"


def labels_path(data_dir: Path) -> Path:
    return raw_dir(data_dir) / "bank-labels.json"  # harness and graders only


def read_labels(path: Path) -> list[BankQuery]:
    if not path.is_file():
        raise FileNotFoundError(f"{path} missing: run `yi data bank check` (it builds it)")
    return [BankQuery.model_validate(r) for r in json.loads(path.read_text())]


def make_split(queries: list[BankQuery], seed: int = SPLIT_SEED) -> BankSplit:
    """Grouped by window (queries over the same window are the same telemetry, so they
    always land together) and stratified by the reason of the window's failure, with the
    two-failure windows a stratum of their own: about DEV_SHARE of each stratum's windows
    to dev, at least one, never all of them."""
    windows: dict[int, list[BankQuery]] = defaultdict(list)
    for q in queries:
        windows[q.start_ms].append(q)
    strata: dict[str, list[int]] = defaultdict(list)
    for w, qs in sorted(windows.items()):
        f = qs[0].failures
        strata["two failures" if len(f) > 1 else f[0].reason].append(w)
    rng = random.Random(seed)
    dev_windows: set[int] = set()
    for _, ws in sorted(strata.items()):
        rng.shuffle(ws)
        k = min(max(1, round(len(ws) * DEV_SHARE)), len(ws) - 1)
        dev_windows.update(ws[:k])
    dev = sorted(q.id for q in queries if q.start_ms in dev_windows)
    test = sorted(q.id for q in queries if q.start_ms not in dev_windows)
    return BankSplit(seed=seed, salt=ID_SALT, dev=dev, test=test)


def split_json(split: BankSplit) -> str:
    return json.dumps(split.model_dump(), indent=1) + "\n"


def read_split(path: Path | None = None) -> BankSplit:
    return BankSplit.model_validate_json((path or SPLIT_PATH).read_text())


def raw_dir(data_dir: Path) -> Path:
    return data_dir / "openrca"


def cases_dir(data_dir: Path) -> Path:
    return data_dir / "openrca" / "bank-cases"


def fetch(data_dir: Path) -> Path:
    """Download Bank (about 26 GB) into <data_dir>/openrca. Resumable."""
    from huggingface_hub import snapshot_download  # imported here: only the VM needs it

    root = raw_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=HF_REPO,
        repo_type="dataset",
        allow_patterns=[f"{SYSTEM}/query.csv", f"{SYSTEM}/record.csv"]
        + [f"{SYSTEM}/telemetry/*/{src}" for src in SOURCES],
        local_dir=root,
    )
    return root


def check_raw(
    root: Path,
    sizes: dict[str, int] | None = None,
    query_sha: str | None = None,
    record_sha: str | None = None,
    labels_sha: str | None = None,
    split_text: str | None = None,
) -> tuple[list[str], list[BankQuery] | None]:
    """(problems, labels). The labels come back only from a clean download: the pinned
    files, every telemetry file at its pinned size, labels with the pinned hash, and the
    committed split reproduced from them. The pins default to the real download's (read
    at call time, so tests can swap them)."""
    sizes = TELEMETRY_SIZES if sizes is None else sizes
    query_sha, record_sha = query_sha or QUERY_SHA256, record_sha or RECORD_SHA256
    problems: list[str] = []
    q, r = root / SYSTEM / "query.csv", root / SYSTEM / "record.csv"
    for path, want in ((q, query_sha), (r, record_sha)):
        if not path.is_file():
            problems.append(f"{path} missing: run `yi data bank fetch`")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != want:
            problems.append(f"{path.name}: sha256 differs from the pinned one")
    for rel, size in sorted(sizes.items()):
        p = root / rel
        if not p.is_file():
            problems.append(f"{rel} missing")
        elif p.stat().st_size != size:
            problems.append(f"{rel}: {p.stat().st_size} bytes, expected {size}")
    if problems:
        return problems, None
    labels = labels_from_csv(q.read_text(), r.read_text())
    if hashlib.sha256(labels_json(labels).encode()).hexdigest() != (labels_sha or LABELS_SHA256):
        problems.append("labels built from the download differ from the pinned hash")
    split = split_json(make_split(labels))
    if split != (split_text if split_text is not None else SPLIT_PATH.read_text()):
        problems.append("the split rebuilt from the labels differs from evals/split_bank.json")
    return problems, None if problems else labels


# ---- the cut ---------------------------------------------------------------------------

type Progress = Callable[[str], None]
CUT_VERSION = 1  # bump when the cut's output changes shape: cut again, then reload
BLOCK_BYTES = 64 << 20
PROGRESS_BYTES = 512 << 20


@dataclass
class CutReport:
    cases: int = 0
    rows: dict[str, dict[str, int]] = field(default_factory=dict)  # case -> kind -> rows
    bad_lines: dict[str, int] = field(default_factory=dict)  # file -> lines skipped
    duplicate_metric_cells: int = 0  # (time, metric) seen twice: the mean is kept
    seconds: float = 0.0

    def totals(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for kinds in self.rows.values():
            for k, n in kinds.items():
                out[k] += n
        return dict(out)


# Read every id as a string (span ids like 21030400016232062871 overflow int64).
_TYPES: dict[str, dict[str, pa.DataType]] = {
    "log/log_service.csv": {
        "log_id": pa.string(),
        "timestamp": pa.int64(),
        "cmdb_id": pa.string(),
        "log_name": pa.string(),
        "value": pa.string(),
    },
    "trace/trace_span.csv": {
        "timestamp": pa.int64(),
        "cmdb_id": pa.string(),
        "parent_id": pa.string(),
        "span_id": pa.string(),
        "trace_id": pa.string(),
        "duration": pa.float64(),
    },
    "metric/metric_container.csv": {
        "timestamp": pa.int64(),
        "cmdb_id": pa.string(),
        "kpi_name": pa.string(),
        "value": pa.float64(),
    },
    "metric/metric_app.csv": {
        "timestamp": pa.int64(),
        "rr": pa.float64(),
        "sr": pa.float64(),
        "cnt": pa.float64(),
        "mrt": pa.float64(),
        "tc": pa.string(),
    },
}
_TS_TO_MS = {SOURCES[0]: 1000, SOURCES[1]: 1000, SOURCES[2]: 1000, SOURCES[3]: 1}
_LONG = pa.schema([("time", pa.int64()), ("metric", pa.string()), ("value", pa.float64())])
_LOG_SCHEMA = pa.schema(
    [
        ("timestamp", pa.int64()),
        ("container_name", pa.string()),
        ("log_name", pa.string()),
        ("message", pa.string()),
    ]
)
_TRACE_SCHEMA = pa.schema(
    [
        ("startTime", pa.int64()),  # ms
        ("serviceName", pa.string()),
        ("traceID", pa.string()),
        ("spanID", pa.string()),
        ("parentSpanID", pa.string()),
        ("duration", pa.float64()),  # us, as RCAEval's; Bank's raw ms (`yi data bank inspect`)
    ]
)


def _log_rows(t: pa.Table) -> pa.Table:
    return pa.table([t["timestamp"], t["cmdb_id"], t["log_name"], t["value"]], schema=_LOG_SCHEMA)


def _trace_rows(t: pa.Table) -> pa.Table:
    return pa.table(
        [
            t["timestamp"],
            t["cmdb_id"],
            t["trace_id"],
            t["span_id"],
            t["parent_id"],
            pc.multiply(t["duration"], 1000.0),
        ],
        schema=_TRACE_SCHEMA,
    )


def _metric_rows(src: str, t: pa.Table) -> pa.Table:
    """Long form (time s, metric, value). A container metric is `<cmdb_id>_<kpi_name>`,
    an application one `<tc>_<kpi>`: the part before the first `_` is the component."""
    if src == "metric/metric_container.csv":
        name = pc.binary_join_element_wise(t["cmdb_id"], t["kpi_name"], "_")
        return pa.table([t["timestamp"], name, t["value"]], schema=_LONG)
    parts = [
        pa.table(
            [t["timestamp"], pc.binary_join_element_wise(t["tc"], pa.scalar(kpi), "_"), t[kpi]],
            schema=_LONG,
        )
        for kpi in ("rr", "sr", "cnt", "mrt")
    ]
    return pa.concat_tables(parts)


def _wide(long: pa.Table, report: CutReport) -> pa.Table:
    """One row per second that has samples, one column per metric (as RCAEval's)."""
    long = long.filter(pc.is_valid(long["value"]))
    g = long.group_by(["time", "metric"]).aggregate([("value", "mean"), ("value", "count")])
    report.duplicate_metric_cells += int(pc.sum(pc.subtract(g["value_count"], 1)).as_py() or 0)
    times = sorted(set(g["time"].to_pylist()))
    at = {t: i for i, t in enumerate(times)}
    cols: dict[str, list[float | None]] = {}
    for t, m, v in zip(
        g["time"].to_pylist(), g["metric"].to_pylist(), g["value_mean"].to_pylist(), strict=True
    ):
        cols.setdefault(m, [None] * len(times))[at[t]] = v
    out: dict[str, Any] = {"time": pa.array(times, pa.int64())}
    for m in sorted(cols):
        out[m] = pa.array(cols[m], pa.float64())
    return pa.table(out)


def _blocks(path: Path, src: str, report: CutReport) -> Iterator[pa.RecordBatch]:
    rel = f"{path.parent.parent.name}/{src}"

    def skip(_: Any) -> str:
        report.bad_lines[rel] = report.bad_lines.get(rel, 0) + 1
        return "skip"

    reader = pcsv.open_csv(
        path,
        read_options=pcsv.ReadOptions(block_size=BLOCK_BYTES),
        parse_options=pcsv.ParseOptions(
            newlines_in_values=src.startswith("log/"), invalid_row_handler=skip
        ),
        convert_options=pcsv.ConvertOptions(column_types=_TYPES[src]),
    )
    yield from reader


def cut(
    root: Path,
    out_dir: Path,
    queries: list[BankQuery],
    progress: Progress | None = None,
) -> CutReport:
    """Cut every query's window (plus the baseline before it) out of the day files.

    All or nothing: the cases are written under a temporary directory that replaces
    out_dir only when every file has been read, so an interrupted cut is simply rerun."""
    say = progress or (lambda _: None)
    t0 = time.monotonic()
    report = CutReport(cases=len(queries))
    tmp = out_dir.with_name(out_dir.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    spans = {q.id: (q.start_ms - BASELINE_MS, q.end_ms) for q in queries}
    writers: dict[tuple[str, str], pq.ParquetWriter] = {}
    metrics: dict[str, list[pa.Table]] = defaultdict(list)
    files = sorted(
        (p, src) for p in (root / SYSTEM / "telemetry").iterdir() if p.is_dir() for src in SOURCES
    )
    try:
        for n, (day, src) in enumerate(files, 1):
            path = day / src
            if not path.is_file():
                continue
            size = path.stat().st_size
            say(f"[{n}/{len(files)}] {day.name} {src} ({size / 1e9:.2f} GB)")
            done, shown, t_file = 0, 0, time.monotonic()
            for batch in _blocks(path, src, report):
                done += batch.nbytes  # in-memory bytes: a rough read position
                tbl = pa.Table.from_batches([batch])
                ms = pc.multiply(tbl["timestamp"], _TS_TO_MS[src])
                lo, hi = pc.min(ms).as_py(), pc.max(ms).as_py()
                if lo is None:
                    continue
                for qid, (a, b) in spans.items():
                    if hi < a or lo >= b:
                        continue
                    part = tbl.filter(pc.and_(pc.greater_equal(ms, a), pc.less(ms, b)))
                    if part.num_rows == 0:
                        continue
                    _write(tmp, qid, src, part, writers, metrics)
                if done - shown >= PROGRESS_BYTES:
                    shown = done
                    rate = done / max(time.monotonic() - t_file, 1e-9) / 1e6
                    say(f"  {done / 1e9:.1f} GB parsed ({rate:,.0f} MB/s)")
        for (qid, kind), w in writers.items():
            w.close()
            report.rows.setdefault(qid, {})[kind] = pq.ParquetFile(
                tmp / qid / f"{kind}.parquet"
            ).metadata.num_rows
        writers.clear()
        for q in queries:
            d = tmp / q.id
            d.mkdir(exist_ok=True)
            wide = _wide(pa.concat_tables(metrics[q.id] or [_LONG.empty_table()]), report)
            pq.write_table(wide, d / "metrics.parquet")
            kinds = report.rows.setdefault(q.id, {})
            kinds["metrics"] = wide.num_rows
            for kind, schema in (("logs", _LOG_SCHEMA), ("traces", _TRACE_SCHEMA)):
                if kind not in kinds:  # nothing in the window: present, but empty
                    pq.write_table(schema.empty_table(), d / f"{kind}.parquet")
                    kinds[kind] = 0
            window = {
                "cut_version": CUT_VERSION,
                "start_ms": q.start_ms,
                "end_ms": q.end_ms,
                "baseline_start_ms": q.start_ms - BASELINE_MS,
            }
            (d / "window.json").write_text(json.dumps(window) + "\n")
    finally:
        for w in writers.values():
            w.close()
    shutil.rmtree(out_dir, ignore_errors=True)
    tmp.rename(out_dir)
    report.seconds = time.monotonic() - t0
    return report


def _write(
    tmp: Path,
    qid: str,
    src: str,
    part: pa.Table,
    writers: dict[tuple[str, str], pq.ParquetWriter],
    metrics: dict[str, list[pa.Table]],
) -> None:
    if src.startswith("metric/"):
        metrics[qid].append(_metric_rows(src, part))
        return
    kind, rows, schema = (
        ("logs", _log_rows(part), _LOG_SCHEMA)
        if src.startswith("log/")
        else ("traces", _trace_rows(part), _TRACE_SCHEMA)
    )
    w = writers.get((qid, kind))
    if w is None:
        (tmp / qid).mkdir(exist_ok=True)
        w = writers[(qid, kind)] = pq.ParquetWriter(tmp / qid / f"{kind}.parquet", schema)
    w.write_table(rows)


def read_case_window(case_dir: Path) -> dict[str, int]:
    w: dict[str, int] = json.loads((case_dir / "window.json").read_text())
    if w.get("cut_version") != CUT_VERSION:
        raise ValueError(f"{case_dir.name}: cut by another version; run `yi data bank cut`")
    return w


def span_containment(case_dir: Path) -> tuple[int, float, float]:
    """(pairs, share contained if durations are ms, share if they are us), over parent and
    child spans on the same host (no clock skew between them). The cut assumes Bank's
    durations are ms, as its timestamps are; this is the check on real data."""
    t = pq.read_table(case_dir / "traces.parquet").to_pylist()
    by_id = {(s["serviceName"], s["spanID"]): s for s in t}
    pairs = [
        (by_id[(s["serviceName"], s["parentSpanID"])], s)
        for s in t
        if (s["serviceName"], s["parentSpanID"]) in by_id and s["parentSpanID"] != s["spanID"]
    ]
    if not pairs:
        return 0, 0.0, 0.0

    def contained(scale: float) -> float:
        ok = 0
        for p, c in pairs:  # starts in ms; durations as stored (us if the raw was ms)
            p_end = p["startTime"] + p["duration"] / 1000 * scale
            c_end = c["startTime"] + c["duration"] / 1000 * scale
            ok += c["startTime"] >= p["startTime"] - 1 and c_end <= p_end + 1
        return ok / len(pairs)

    return len(pairs), contained(1.0), contained(1e-3)
