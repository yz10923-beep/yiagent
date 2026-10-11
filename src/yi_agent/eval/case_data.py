"""The grader's view of one case: the source Parquet, read independently of Elasticsearch,
so a loader bug can never make a citation look grounded."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from yi_agent.data.telemetry import (
    Kind,
    TimeNormalizer,
    pick_time_column,
    read_window,
    source_paths,
)
from yi_agent.eval import baro
from yi_agent.eval.scoring import normalize_service

LOG_TOLERANCE_MS = 1000  # log times are whole seconds


def is_error_status(code: int | None) -> bool:
    """gRPC codes 1-16 (Online Boutique; 0 is OK) and HTTP 4xx/5xx are errors."""
    return code is not None and (1 <= code < 100 or code >= 400)


@dataclass(frozen=True)
class Span:
    service: str
    start_ms: int
    status: int | None


@dataclass
class CaseData:
    case_dir: Path
    onset_ms: int = field(init=False)

    def __post_init__(self) -> None:
        self.onset_ms = read_window(self.case_dir).onset_ms

    @cached_property
    def _paths(self) -> dict[Kind, Path]:
        return source_paths(self.case_dir)

    @cached_property
    def logs(self) -> pa.Table | None:
        """container_name, message, and `at_ms` (the loader's @timestamp)."""
        path = self._paths.get("logs")
        if path is None:
            return None
        pf = pq.ParquetFile(path)
        col = pick_time_column("logs", pf)
        t = pq.read_table(path, columns=[col, "container_name", "message"])
        at = pa.array(TimeNormalizer()(t.column(col)), pa.int64())
        return pa.table(
            {
                "container_name": t.column("container_name"),
                "message": t.column("message"),
                "at_ms": at,
            }
        )

    @cached_property
    def spans(self) -> dict[str, Span]:
        path = self._paths.get("traces")
        if path is None:
            return {}
        pf = pq.ParquetFile(path)
        col = pick_time_column("traces", pf)
        t = pq.read_table(path, columns=[col, "spanID", "serviceName", "statusCode"])
        starts = TimeNormalizer()(t.column(col))
        return {
            sid: Span(service=normalize_service(svc or ""), start_ms=s or 0, status=code)
            for sid, svc, s, code in zip(
                t.column("spanID").to_pylist(),
                t.column("serviceName").to_pylist(),
                starts,
                t.column("statusCode").to_pylist(),
                strict=True,
            )
        }

    @cached_property
    def window(self) -> baro.MetricWindow:
        return baro.prepare(self._paths["metrics"], self.onset_ms // 1000)

    @cached_property
    def metric_names(self) -> set[str]:
        return set(pq.ParquetFile(self._paths["metrics"]).schema_arrow.names) - {"time"}

    def metric_z(self, name: str) -> tuple[float, float] | None:
        """(max, min) robust z of the 10 minutes after the onset against the 10 before,
        BARO's statistic, prepared as BARO prepares it (latency-50 included here, since
        an agent may cite it). None if the metric does not exist."""
        if name not in self.metric_names:
            return None
        t = pq.read_table(self._paths["metrics"], columns=["time", name])
        w = baro.prepare_columns(t, [name], self.onset_ms // 1000)
        a, b = w.before[:, 0], w.after[:, 0]
        if len(a) == 0 or len(b) == 0:
            return (0.0, 0.0)
        q25, median, q75 = np.percentile(a, [25, 50, 75])
        scale = q75 - q25 if q75 - q25 >= baro.EPS else 1.0
        z = (b - median) / scale
        return (float(np.max(z)), float(np.min(z)))

    def log_rows(self, container: str, at_ms: int, excerpt: str) -> pa.Table:
        logs = self.logs
        if logs is None:
            return pa.table({})
        mask = pc.and_(
            pc.equal(logs.column("container_name"), container),
            pc.less_equal(pc.abs(pc.subtract(logs.column("at_ms"), at_ms)), LOG_TOLERANCE_MS),
        )
        near = logs.filter(mask)
        return near.filter(pc.match_substring(near.column("message"), excerpt))
