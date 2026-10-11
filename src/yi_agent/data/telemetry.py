"""Read one case directory into documents for Elasticsearch.

Harness-side: this reads from a directory whose name is the label, so nothing here may
copy a path, a file name or a case name into a document. Documents carry exactly the
source columns plus `@timestamp` (epoch ms); metrics are reshaped to one document per
non-null cell. `root_cause.txt` is never read.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

type Kind = Literal["logs", "traces", "metrics"]
type Doc = dict[str, Any]

KINDS: tuple[Kind, ...] = ("logs", "traces", "metrics")
# Where a source's time may be, in order of preference, chosen from the values rather
# than trusted by name: every system's traces have a `time` column holding 'HH:MM'
# ('12:02'), with the real start in `startTime` (us) and `startTimeMillis` (VM, 2026-10-11).
TIME_CANDIDATES: dict[Kind, tuple[str, ...]] = {
    "logs": ("timestamp", "time"),
    "traces": ("startTime", "startTimeMillis", "time", "timestamp"),
    "metrics": ("time",),
}
BATCH_ROWS = 10_000

# Epoch values of 2001-2286 in each unit fall in disjoint ranges, so the magnitude of one
# value names its unit.
_UNITS: tuple[tuple[float, str, float], ...] = (
    (1e11, "s", 1e3),
    (1e14, "ms", 1.0),
    (1e17, "us", 1e-3),
    (1e20, "ns", 1e-6),
)


class TelemetryError(Exception):
    pass


def unit_of(value: float) -> tuple[str, float]:
    """(unit name, factor to milliseconds) for one epoch value."""
    v = abs(value)
    if v < 1e9:
        raise TelemetryError(f"timestamp {value} is not an epoch time after 2001")
    for limit, name, to_ms in _UNITS:
        if v < limit:
            return name, to_ms
    raise TelemetryError(f"timestamp {value} is too large for any epoch unit")


def _parse_time_string(s: str) -> float:
    """Epoch ms from an ISO-8601 string or a numeric string."""
    try:
        v = float(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError as e:
            raise TelemetryError(f"unparseable timestamp {s!r}") from e
        if dt.tzinfo is None:  # never the machine's local zone
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp() * 1e3
    return v * unit_of(v)[1]


class TimeNormalizer:
    """Epoch ms for a whole column. The unit is fixed from the first value seen, so one
    file never mixes units."""

    def __init__(self) -> None:
        self.unit: str | None = None
        self._to_ms = 1.0
        self.unparsed = 0  # string values that are not a time; they become None

    def __call__(self, col: pa.ChunkedArray | pa.Array) -> list[int | None]:
        t = col.type
        if pa.types.is_timestamp(t):
            self.unit = self.unit or f"timestamp[{t.unit}]"
            ms = pc.cast(pc.cast(col, pa.timestamp("ms", tz=t.tz)), pa.int64())
            return [None if v is None else int(v) for v in ms.to_pylist()]
        if _is_string(t):
            out: list[int | None] = []
            error: TelemetryError | None = None
            for s in col.to_pylist():
                try:
                    out.append(None if s is None else round(_parse_time_string(s)))
                except TelemetryError as e:
                    out.append(None)
                    self.unparsed += 1
                    error = error or e
            if error is not None and all(v is None for v in out):
                raise error  # not a time column at all
            self.unit = self.unit or "string"
            return out
        if not (pa.types.is_integer(t) or pa.types.is_floating(t)):
            raise TelemetryError(f"time column has type {t}")
        values = [v if v is not None and math.isfinite(v) else None for v in col.to_pylist()]
        if self.unit is None:
            first = next((v for v in values if v is not None), None)
            if first is None:
                return [None] * len(values)
            self.unit, self._to_ms = unit_of(float(first))
        return [None if v is None else round(v * self._to_ms) for v in values]


def _is_string(t: pa.DataType) -> bool:
    return bool(pa.types.is_string(t) or pa.types.is_large_string(t))


def pick_time_column(kind: Kind, pf: pq.ParquetFile) -> str:
    """The first candidate whose values are epoch times. A typed (numeric or timestamp)
    column wins over a string one."""
    names: list[str] = list(pf.schema_arrow.names)
    present = [c for c in TIME_CANDIDATES[kind] if c in names]
    if not present:
        raise TelemetryError(f"{kind}: no time column among {names[:12]}")
    sample = next(pf.iter_batches(batch_size=50, columns=present), None)
    if sample is None or sample.num_rows == 0:
        return present[0]  # an empty file: nothing to place in time
    tried: list[str] = []
    for want_typed in (True, False):
        for c in present:
            col = sample.column(c)
            if _is_string(col.type) == want_typed:
                continue
            try:
                ms = TimeNormalizer()(col)
            except TelemetryError as e:
                tried.append(f"{c} ({col.type}): {e}")
                continue
            if any(v is not None for v in ms):
                return c
            tried.append(f"{c} ({col.type}): no values")
    raise TelemetryError(f"{kind}: no usable time column: {'; '.join(tried)}")


@dataclass
class FileStats:
    rows: int = 0
    docs: int = 0
    skipped_nonfinite: int = 0  # NaN/inf metric cells: JSON can't carry them
    skipped_no_time: int = 0
    time_column: str | None = None
    time_unit: str | None = None
    columns: list[str] = field(default_factory=list)


def _clean(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def iter_docs(kind: Kind, path: Path, stats: FileStats) -> Iterator[Doc]:
    pf = pq.ParquetFile(path)
    names: list[str] = list(pf.schema_arrow.names)
    stats.columns = names
    time_col = stats.time_column = pick_time_column(kind, pf)
    norm = TimeNormalizer()
    for batch in pf.iter_batches(batch_size=BATCH_ROWS):
        stats.rows += batch.num_rows
        ts = norm(batch.column(time_col))
        stats.time_unit = norm.unit
        if kind == "metrics":
            yield from _metric_docs(batch, names, time_col, ts, stats)
            continue
        cols = {n: batch.column(n).to_pylist() for n in names}
        for i, t in enumerate(ts):
            if t is None:
                stats.skipped_no_time += 1
                continue
            doc: Doc = {"@timestamp": t}
            for n in names:
                v = _clean(cols[n][i])
                if v is not None:
                    doc[n] = v
            stats.docs += 1
            yield doc


def _metric_docs(
    batch: pa.RecordBatch, names: list[str], time_col: str, ts: list[int | None], stats: FileStats
) -> Iterator[Doc]:
    for n in names:
        if n == time_col:
            continue
        for t, v in zip(ts, batch.column(n).to_pylist(), strict=True):
            if v is None:
                continue  # not observed at this timestamp: no document, as at rest
            if t is None:
                stats.skipped_no_time += 1
                continue
            if not math.isfinite(v):
                stats.skipped_nonfinite += 1
                continue
            stats.docs += 1
            yield {"@timestamp": t, "metric": n, "value": float(v)}


@dataclass(frozen=True)
class CaseWindow:
    onset_ms: int  # inject_time: what RCAEval's baselines are given
    start_ms: int
    end_ms: int
    sources: tuple[Kind, ...]


def source_paths(case_dir: Path) -> dict[Kind, Path]:
    """The sources this case has. A missing file means the source was not collected."""
    return {k: case_dir / f"{k}.parquet" for k in KINDS if (case_dir / f"{k}.parquet").is_file()}


def read_window(case_dir: Path) -> CaseWindow:
    paths = source_paths(case_dir)
    if "metrics" not in paths:
        raise TelemetryError("case has no metrics.parquet")
    try:
        onset_s = int((case_dir / "inject_time.txt").read_text().strip())
    except (OSError, ValueError) as e:
        raise TelemetryError(f"bad inject_time.txt: {e}") from e
    pf = pq.ParquetFile(paths["metrics"])
    col = pick_time_column("metrics", pf)
    times = pq.read_table(paths["metrics"], columns=[col]).column(col)
    ms = [t for t in TimeNormalizer()(times) if t is not None]
    if not ms:
        raise TelemetryError("metrics.parquet has no timestamps")
    return CaseWindow(
        onset_ms=onset_s * 1000, start_ms=min(ms), end_ms=max(ms), sources=tuple(paths)
    )
