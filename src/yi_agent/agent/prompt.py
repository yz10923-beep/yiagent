"""What the model is told. The system prompt is the same for every case (so it is cached
and so nothing case-specific can leak through it); the case enters only through the alert.

The fault descriptions say how each injected fault is made and where it shows, as the
RCAEval paper describes them: general knowledge about the benchmark's fault types, the
same for every case, never which fault or service a case has."""

from __future__ import annotations

import hashlib

from yi_agent.data.alert import Alert

FAULT_GUIDE = """\
Fault types (fault_type must be one of these):
- cpu: a CPU hog in the root-cause service's container (its cpu metric rises).
- mem: a memory hog (its mem metric rises).
- disk: disk I/O stress (its diskio metric rises).
- socket: socket stress, many connections opened (its socket metric rises).
- delay: network delay injected on the service (its latency rises, and so does the
  latency of every service waiting on it; traces show where the time is spent).
- loss: packet loss on the service (latency rises and requests fail or retry).
- f1: a code bug that passes incorrect parameter values.
- f2: a code bug that drops required parameters.
- f3: a code bug that skips a function call.
- f4: a code bug that returns incorrect values.
- f5: a code bug with a missing exception handler (exceptions escape).
Code bugs (f1-f5) show in the service's logs (errors, exceptions, stack traces) and in
error spans more than in resource metrics."""

SYSTEM = f"""\
You are an on-call site reliability engineer investigating an incident in a
microservice application. Find the service where the fault was injected (the root
cause), the fault type, and the evidence, using only the read-only telemetry tools.

Every service near the root cause can look unhealthy: callers of a slow service are
slow too, and a busy node can raise its neighbours' metrics. Separate the cause from
its symptoms before you answer, and check your leading hypothesis against a second
source of telemetry (metrics, logs, traces) when the incident has one.

{FAULT_GUIDE}

Finish by calling submit_report exactly once:
- root_causes: up to 5 service names, most likely first, written as they appear in
  metric names (the part before the first "_") or in log container names.
- evidence: 1 to 5 items copied exactly from tool output, about the root cause: a log
  line (container, at_ms, and a verbatim excerpt of at least 10 characters from that one
  line), a span (span_id), or a metric (its full name and whether it went up or down).
  The evidence is checked against the data.
You have a limited number of tool calls; spend them on the questions that separate
your hypotheses."""

SYSTEM_SHA = hashlib.sha256(SYSTEM.encode()).hexdigest()[:12]


def first_message(alert: Alert) -> str:
    return f"{alert.render()}\n\nInvestigate, then call submit_report."


NUDGE = (
    "You stopped without calling submit_report. Call it now with your best answer; "
    "an answer with no report scores nothing."
)


def budget_notice(remaining: int) -> str:
    return (
        f"[{remaining} tool round(s) left. Call submit_report before they run out: an "
        "investigation that ends without a report scores nothing.]"
    )
