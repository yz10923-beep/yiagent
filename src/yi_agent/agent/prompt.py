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


# S4 (2026-10-13): the same eleven types, with how each is injected and what separates
# the ones S3 confused (measured with `yi eval fault-signals` on dev: a socket stressor
# raises cpu more than sockets; disk stress fills the page cache; a code fault arrives as
# a redeploy, whose restart moves memory). Mechanisms only: nothing here names a service
# or a case, and the test split checks that it generalizes.
FAULT_GUIDE_V2 = """\
Fault types (fault_type must be one of these), how each is injected, and how to tell
apart the ones that look alike:
- cpu: a CPU stressor in the service's container. Its cpu rises and stays up; its
  socket count barely changes.
- socket: a socket stressor, which opens and closes many sockets and burns CPU doing
  it. Its cpu rises, often more than anything else, and its socket metric rises
  clearly too. When cpu and sockets both rise on a service that did not restart, it is
  socket, not cpu.
- mem: a memory stressor. Its mem rises and stays high for the whole fault, without a
  restart.
- disk: a disk I/O stressor. Its diskio jumps a hundredfold or more. Heavy writes
  also fill the page cache, so its memory rises too: when both move, the one far
  beyond its normal range is the fault.
- delay: network delay added to the service. Every request to it is slower, so its
  latency rises across the board (median as well as p90), and its callers' latency
  rises with it.
- loss: packets to and from the service are dropped. Retransmissions make some
  requests very slow while most are unaffected, so its tail latency (p90) rises far
  more than its median; requests may time out, and failed health checks may restart it.
- f1-f5: code bugs, injected by deploying modified code. The deploy restarts the
  service: its logs show startup messages, its memory drops and climbs back (or jumps
  during warm-up and later returns to normal), its cpu spikes briefly. A restart
  followed by new errors or error responses is a code fault, not a resource fault,
  even though memory and cpu moved. Then choose by what the errors say:
  - f1: incorrect parameter values (a call gets a wrong value, such as a flag or an
    argument it cannot handle, and fails or misbehaves).
  - f2: missing parameters (a call is made without a required argument: errors about
    a null or missing value at the call).
  - f3: a missing function call (a step is skipped, so later logic fails or returns
    incomplete results).
  - f4: incorrect return values (a function returns a wrong value, such as null or
    empty, and its callers fail on it).
  - f5: a missing exception handler (an exception that used to be handled now escapes,
    failing the request or crashing the process)."""

GUIDES = {"v1": FAULT_GUIDE, "v2": FAULT_GUIDE_V2}

_TEMPLATE = """\
You are an on-call site reliability engineer investigating an incident in a
microservice application. Find the service where the fault was injected (the root
cause), the fault type, and the evidence, using only the read-only telemetry tools.

Every service near the root cause can look unhealthy: callers of a slow service are
slow too, and a busy node can raise its neighbours' metrics. Separate the cause from
its symptoms before you answer, and check your leading hypothesis against a second
source of telemetry (metrics, logs, traces) when the incident has one.

{guide}

Finish by calling submit_report exactly once:
- root_causes: up to 5 service names, most likely first, written as they appear in
  metric names (the part before the first "_") or in log container names.
- evidence: 1 to 5 items copied exactly from tool output, about the root cause: a log
  line (container, at_ms, and a verbatim excerpt of at least 10 characters from that one
  line), a span (span_id), or a metric (its full name and whether it went up or down).
  The evidence is checked against the data.
You have a limited number of tool calls; spend them on the questions that separate
your hypotheses."""


def system_prompt(version: str = "v1") -> str:
    if version not in GUIDES:
        raise ValueError(f"unknown prompt version {version!r}: {sorted(GUIDES)}")
    return _TEMPLATE.format(guide=GUIDES[version])


def system_sha(version: str = "v1") -> str:
    return hashlib.sha256(system_prompt(version).encode()).hexdigest()[:12]


SYSTEM = system_prompt("v1")  # the S3 prompt, byte for byte
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
