"""The alert: everything the agent is told about a case before it starts querying.

It carries what RCAEval's baselines get (the anomaly onset, the system) and what any
on-call engineer would know (which telemetry exists, over which window), and nothing
derived from the label: no service, no symptom, no fault (docs/ROADMAP.md, S1).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from yi_agent.data.rcaeval import SYSTEMS, CaseLabel
from yi_agent.data.telemetry import KINDS, CaseWindow, Kind


class Alert(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    system: str  # the application's name, e.g. "Train Ticket"
    onset_ms: int
    start_ms: int
    end_ms: int
    sources: tuple[Kind, ...]

    def render(self) -> str:
        have = ", ".join(self.sources)
        missing = [k for k in KINDS if k not in self.sources]
        lines = [
            f"Incident {self.case_id} in the {self.system} microservice application.",
            f"An anomaly was detected starting at {_iso(self.onset_ms)}.",
            f"Telemetry from {_iso(self.start_ms)} to {_iso(self.end_ms)}: {have}.",
        ]
        if missing:
            lines.append(f"Not collected for this incident: {', '.join(missing)}.")
        return "\n".join(lines)


def make_alert(label: CaseLabel, window: CaseWindow) -> Alert:
    return Alert(
        case_id=label.id,
        system=SYSTEMS[label.system],
        onset_ms=window.onset_ms,
        start_ms=window.start_ms,
        end_ms=window.end_ms,
        sources=window.sources,
    )


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
