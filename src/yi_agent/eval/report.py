"""What the agent, and every baseline, returns for one case.

Evidence is typed, so grounding is checked by code against the data: a log line, a span
or a metric, each identified the way the agent saw it in Elasticsearch."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

MIN_EXCERPT = 10  # shorter excerpts ("ERROR", "200") match almost any line


class LogCitation(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["log"] = "log"
    container: str
    at_ms: int  # the line's @timestamp; logs carry 1-second times, so +-1 s is accepted
    excerpt: str = Field(min_length=MIN_EXCERPT)  # verbatim part of the message


class SpanCitation(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["span"] = "span"
    span_id: str


class MetricCitation(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["metric"] = "metric"
    metric: str  # the column name, e.g. "ts-order-service_cpu"
    direction: Literal["up", "down"]  # how it moved after the onset


Citation = Annotated[LogCitation | SpanCitation | MetricCitation, Field(discriminator="kind")]


class Report(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    root_causes: list[str] = Field(default_factory=list)  # most likely first; top 5 graded
    fault_type: str | None = None
    evidence: list[Citation] = Field(default_factory=list)
    summary: str = ""


class Trial(BaseModel):
    """One graded unit: a report, or the infra failure that prevented one. Infra failures
    are counted, never scored (CLAUDE.md, Invariants)."""

    case_id: str
    status: Literal["ok", "infra_error"] = "ok"
    error: str | None = None
    report: Report | None = None


def write_trials(trials: list[Trial], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(t.model_dump_json() + "\n" for t in trials))


def read_trials(path: Path) -> list[Trial]:
    return [Trial.model_validate_json(line) for line in path.read_text().splitlines() if line]
