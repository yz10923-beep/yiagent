"""Is each citation real, does it say what it claims, and is it the evidence the paper
names for this fault? Checked by code against the case's data, never by the agent.

- exists: the cited line, span or metric is in this case's data.
- holds: a metric moved the stated way after the onset (BARO's robust z beyond 3); a log
  line or span is at or after the onset (a line from before cannot show the fault).
- on target: the cited item is about the true root-cause service.
- expected evidence: some citation is the paper's root-cause indicator for the true fault
  (RCAEval, WWW 2025, section 3.2): the resource metric for cpu, mem, disk and socket
  (RCAEval's own indicator columns: `_cpu`, `_mem`, `_diskio`, `_socket`), latency for
  delay, latency or an error span for loss, and for code-level faults an error log line
  (a stack trace when there is one) or an error span of the root-cause service."""

from __future__ import annotations

import re
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.compute as pc

from yi_agent.eval.case_data import LOG_TOLERANCE_MS, CaseData, is_error_status
from yi_agent.eval.report import Citation, LogCitation, MetricCitation, SpanCitation
from yi_agent.eval.scoring import normalize_service, service_of_metric

Z_HOLDS = 3.0
METRIC_INDICATORS: dict[str, tuple[str, ...]] = {
    "cpu": ("cpu",),
    "mem": ("mem",),
    "disk": ("diskio",),
    "socket": ("socket",),
    "delay": ("latency-90", "latency-50", "latency"),
    "loss": ("latency-90", "latency-50", "latency"),
}
ERROR_SPAN_FAULTS = {"loss", "f1", "f2", "f3", "f4", "f5"}
ERROR_LOG_FAULTS = {"f1", "f2", "f3", "f4", "f5"}
# RE2 regex syntax (pyarrow) and Python's agree on this one.
ERROR_LINE = r"(?i)exception|error|traceback|panic|fatal|\tat "


@dataclass(frozen=True)
class CitationCheck:
    exists: bool
    holds: bool
    service: str | None  # what the cited item is about
    error: bool = False  # an error span, or a log line that reads as an error


def check(c: Citation, data: CaseData) -> CitationCheck:
    if isinstance(c, LogCitation):
        rows = data.log_rows(c.container, c.at_ms, c.excerpt)
        exists = rows.num_rows > 0
        error = exists and any(re.search(ERROR_LINE, m) for m in rows.column("message").to_pylist())
        return CitationCheck(
            exists=exists,
            holds=exists and c.at_ms >= data.onset_ms - LOG_TOLERANCE_MS,
            service=normalize_service(c.container),
            error=error,
        )
    if isinstance(c, SpanCitation):
        span = data.spans.get(c.span_id)
        if span is None:
            return CitationCheck(exists=False, holds=False, service=None)
        return CitationCheck(
            exists=True,
            holds=span.start_ms >= data.onset_ms,
            service=span.service,
            error=is_error_status(span.status),
        )
    z = data.metric_z(c.metric)
    if z is None:
        return CitationCheck(exists=False, holds=False, service=service_of_metric(c.metric))
    moved = z[0] >= Z_HOLDS if c.direction == "up" else z[1] <= -Z_HOLDS
    return CitationCheck(exists=True, holds=moved, service=service_of_metric(c.metric))


def _metric_suffix(name: str) -> str:
    return name.split("_", 1)[1] if "_" in name else ""


def indicates(c: Citation, result: CitationCheck, service: str, fault: str) -> bool:
    """Is this citation the root-cause indicator for (service, fault)?"""
    if not (result.exists and result.holds and result.service == normalize_service(service)):
        return False
    if isinstance(c, MetricCitation):
        return c.direction == "up" and _metric_suffix(c.metric) in METRIC_INDICATORS.get(fault, ())
    if isinstance(c, SpanCitation):
        return fault in ERROR_SPAN_FAULTS and result.error
    return fault in ERROR_LOG_FAULTS and result.error


def oracle_evidence(data: CaseData, service: str, fault: str) -> list[Citation]:
    """The indicators that exist and hold in this case's data, as citations. Empty means
    the expected evidence is not there to be found, and the case is not graded on it."""
    rc = normalize_service(service)
    found: list[Citation] = []
    for suffix in METRIC_INDICATORS.get(fault, ()):
        for name in sorted(data.metric_names):
            if service_of_metric(name) == rc and _metric_suffix(name) == suffix:
                c = MetricCitation(metric=name, direction="up")
                if check(c, data).holds:
                    found.append(c)
                    break
        if found:
            break
    if fault in ERROR_SPAN_FAULTS:
        for sid, span in data.spans.items():
            if (
                span.service == rc
                and span.start_ms >= data.onset_ms
                and is_error_status(span.status)
            ):
                found.append(SpanCitation(span_id=sid))
                break
    if fault in ERROR_LOG_FAULTS and data.logs is not None:
        logs = data.logs
        containers = {
            c
            for c in pc.unique(logs.column("container_name")).to_pylist()
            if c and normalize_service(c) == rc
        }
        if containers:
            names = logs.column("container_name")
            mask = pc.and_(
                pc.is_in(names, value_set=pa.array(sorted(containers), names.type)),
                pc.greater_equal(logs.column("at_ms"), data.onset_ms),
            )
            hits = logs.filter(mask)
            hits = hits.filter(pc.match_substring_regex(hits.column("message"), ERROR_LINE))
            if hits.num_rows:
                row = hits.slice(0, 1).to_pylist()[0]
                excerpt = row["message"][:160]
                if len(excerpt) >= 10:
                    found.append(
                        LogCitation(
                            container=row["container_name"], at_ms=row["at_ms"], excerpt=excerpt
                        )
                    )
    return found
