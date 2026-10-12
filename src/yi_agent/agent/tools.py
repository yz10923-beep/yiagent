"""The agent's tools: read-only queries over one case's indices, plus submit_report.

Every tool is bound to one case id at construction (`CaseIndices`): the model names a
service or a metric, never an index, so it cannot reach another case. The client wrapper
exposes only `search` and `count`, so no tool can write.

Outputs are compact text tables, not JSON: they are read by the model, and a table is
about half the tokens. Every value the model may cite (a log line's container, time and
text, a span id, a metric name) is printed exactly as the grader will look it up.
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from elasticsearch import ApiError, Elasticsearch, TransportError

from yi_agent.data.es_load import index_name
from yi_agent.data.rcaeval import FAULTS
from yi_agent.data.telemetry import Kind

HALF_WINDOW_MS = 10 * 60 * 1000  # BARO's 10 minutes on each side of the onset
EPS = 1e-12
MAX_CHARS = 8_000  # one tool result; the model is told to narrow the query instead
LINE_CHARS = 300  # a log message as shown (the full text is still searchable)
ERROR_STATUS: dict[str, Any] = {  # gRPC 1-16 and HTTP 4xx/5xx (eval.case_data.is_error_status)
    "bool": {
        "should": [
            {"range": {"statusCode": {"gte": 1, "lt": 100}}},
            {"range": {"statusCode": {"gte": 400}}},
        ],
        "minimum_should_match": 1,
    }
}
ALL_FAULTS = tuple(f for fs in FAULTS.values() for f in fs)


class ToolError(Exception):
    """The model's call was wrong (unknown metric, bad argument): it sees the message."""


class ToolInfraError(Exception):
    """Elasticsearch failed: an infra failure, never a score (CLAUDE.md, Invariants)."""


class CaseIndices:
    """Read-only access to one case's indices. Nothing else is reachable through it."""

    def __init__(self, es: Elasticsearch, case_id: str, sources: tuple[Kind, ...]) -> None:
        self._es = es
        self._case_id = case_id
        self.sources = sources

    def search(self, kind: Kind, **body: Any) -> dict[str, Any]:
        if kind not in self.sources:
            raise ToolError(f"no {kind} were collected for this incident")
        try:
            return dict(self._es.search(index=index_name(self._case_id, kind), **body))
        except ApiError as e:
            if 400 <= e.status_code < 500:
                raise ToolError(f"query rejected: {e.message}") from e
            raise ToolInfraError(f"Elasticsearch {e.status_code}: {e.message}") from e
        except TransportError as e:
            raise ToolInfraError(f"Elasticsearch unreachable: {e}") from e


def _window(onset_ms: int, which: str) -> dict[str, Any]:
    lo, hi = onset_ms - HALF_WINDOW_MS, onset_ms + HALF_WINDOW_MS
    rng = {"before": (lo, onset_ms), "after": (onset_ms, hi), "all": (lo, hi)}
    if which not in rng:
        raise ToolError(f"window must be one of {sorted(rng)}")
    a, b = rng[which]
    return {"range": {"@timestamp": {"gte": a, "lt": b}}}


def _service_filter(field: str, service: str | None) -> list[dict[str, Any]]:
    if not service:
        return []
    return [
        {
            "bool": {
                "should": [{"term": {field: service}}, {"prefix": {field: service + "-"}}],
                "minimum_should_match": 1,
            }
        }
    ]


def _escape_wildcard(s: str) -> str:
    return s.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")


def _clip(s: str, n: int = LINE_CHARS) -> str:
    s = s.replace("\n", "\\n").replace("\t", "\\t")
    return s if len(s) <= n else s[:n] + f"... [{len(s)} chars]"


def _num(x: float | None, digits: int = 3) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x:.{digits}g}" if abs(x) < 1e6 else f"{x:.3e}"


def _rel(at_ms: int, onset_ms: int) -> str:
    s = (at_ms - onset_ms) / 1000
    return f"{s:+.0f}s"


@dataclass
class CaseTools:
    data: CaseIndices
    onset_ms: int

    # --- discovery -----------------------------------------------------------------

    def overview(self) -> str:
        """Which services exist, by source, with activity before and after the onset."""
        out = [f"times: onset = t+0s; windows are 10 minutes on each side ({self.data.sources})"]
        if "metrics" in self.data.sources:
            r = self.data.search(
                "metrics", size=0, aggs={"m": {"terms": {"field": "metric", "size": 5000}}}
            )
            fams: dict[str, set[str]] = {}
            for b in r["aggregations"]["m"]["buckets"]:
                svc, _, fam = b["key"].partition("_")
                fams.setdefault(svc, set()).add(fam)
            out.append(f"metrics: {len(fams)} services (metric name = <service>_<family>)")
            out += [f"  {s}: {', '.join(sorted(f))}" for s, f in sorted(fams.items())]
        split = {
            "before": {"filter": _window(self.onset_ms, "before")},
            "after": {"filter": _window(self.onset_ms, "after")},
        }
        if "logs" in self.data.sources:
            r = self.data.search(
                "logs",
                size=0,
                aggs={"c": {"terms": {"field": "container_name", "size": 500}, "aggs": split}},
            )
            out.append("logs: lines per container, 10 min before / 10 min after the onset")
            for b in r["aggregations"]["c"]["buckets"]:
                out.append(f"  {b['key']}: {b['before']['doc_count']} / {b['after']['doc_count']}")
        if "traces" in self.data.sources:
            err = {"errors": {"filter": ERROR_STATUS}}
            r = self.data.search(
                "traces",
                size=0,
                aggs={
                    "s": {
                        "terms": {"field": "serviceName", "size": 500},
                        "aggs": {k: {**v, "aggs": err} for k, v in split.items()},
                    }
                },
            )
            out.append("traces: spans (error spans) per service, before / after the onset")
            for b in r["aggregations"]["s"]["buckets"]:
                bf, af = b["before"], b["after"]
                out.append(
                    f"  {b['key']}: {bf['doc_count']} ({bf['errors']['doc_count']}) / "
                    f"{af['doc_count']} ({af['errors']['doc_count']})"
                )
        return "\n".join(out)

    # --- metrics -------------------------------------------------------------------

    def metric_changes(
        self, service: str | None = None, family: str | None = None, limit: int = 25
    ) -> str:
        """Each metric scored the way BARO scores it: the 10 minutes after the onset
        against the median and interquartile range of the 10 minutes before."""
        filters: list[dict[str, Any]] = [_window(self.onset_ms, "all")]
        if service:
            filters.append({"prefix": {"metric": service + "_"}})
        if family:
            filters.append({"wildcard": {"metric": "*_" + family}})
        r = self.data.search(
            "metrics",
            size=0,
            query={"bool": {"filter": filters}},
            aggs={
                "m": {
                    "terms": {"field": "metric", "size": 5000},
                    "aggs": {
                        "before": {
                            "filter": _window(self.onset_ms, "before"),
                            "aggs": {
                                "q": {
                                    "percentiles": {
                                        "field": "value",
                                        "percents": [25, 50, 75],
                                        "tdigest": {"compression": 1000},
                                    }
                                }
                            },
                        },
                        "after": {
                            "filter": _window(self.onset_ms, "after"),
                            "aggs": {"s": {"stats": {"field": "value"}}},
                        },
                    },
                }
            },
        )
        rows = []
        for b in r["aggregations"]["m"]["buckets"]:
            name = b["key"]
            q = b["before"]["q"]["values"]
            s = b["after"]["s"]
            if not b["before"]["doc_count"] or not s["count"]:
                continue
            scale = 1e6 if name.endswith("_mem") else 1.0  # bytes to MB, as BARO does
            q25, med, q75 = (q[k] / scale for k in ("25.0", "50.0", "75.0"))
            hi, lo, avg = s["max"] / scale, s["min"] / scale, s["avg"] / scale
            iqr = q75 - q25
            flat = iqr < EPS
            div = 1.0 if flat else iqr
            z_up, z_down = (hi - med) / div, (lo - med) / div
            rows.append((max(abs(z_up), abs(z_down)), name, med, iqr, avg, hi, z_up, z_down, flat))
        if not rows:
            return (
                "no metrics match"
                + (f" service={service!r}" if service else "")
                + (f" family={family!r}" if family else "")
            )
        rows.sort(key=lambda t: -t[0])
        out = [
            f"{len(rows)} metrics, strongest change first (mem in MB). z = (value after - "
            "median before) / IQR before; 'flat' = no spread before, so z is the raw change.",
            f"{'metric':<44} {'median_bef':>10} {'iqr_bef':>9} {'mean_aft':>10} "
            f"{'max_aft':>10} {'z_up':>9} {'z_down':>9}",
        ]
        for _, name, med, iqr, avg, hi, zu, zd, flat in rows[: max(1, min(limit, 200))]:
            out.append(
                f"{name:<44} {_num(med):>10} {_num(iqr):>9} {_num(avg):>10} {_num(hi):>10} "
                f"{_num(zu):>9} {_num(zd):>9}{'  flat' if flat else ''}"
            )
        if len(rows) > limit:
            out.append(f"... {len(rows) - limit} more (raise limit, or filter by service/family)")
        return "\n".join(out)

    def metric_series(self, metric: str, step_s: int = 30) -> str:
        """One metric over the window, averaged per step, to see the shape of a change."""
        step_s = max(5, min(step_s, 300))
        r = self.data.search(
            "metrics",
            size=0,
            query={
                "bool": {"filter": [{"term": {"metric": metric}}, _window(self.onset_ms, "all")]}
            },
            aggs={
                "t": {
                    "histogram": {
                        "field": "@timestamp",
                        "interval": step_s * 1000,
                        "offset": self.onset_ms % (step_s * 1000),
                    },
                    "aggs": {
                        "avg": {"avg": {"field": "value"}},
                        "max": {"max": {"field": "value"}},
                    },
                }
            },
        )
        buckets = r["aggregations"]["t"]["buckets"]
        if not buckets:
            raise ToolError(f"no metric named {metric!r} (names are listed by overview)")
        out = [f"{metric}, per {step_s}s step (t relative to the onset): avg, max"]
        for b in buckets:
            if b["doc_count"]:
                out.append(
                    f"  t{_rel(int(b['key']), self.onset_ms):>7} {_num(b['avg']['value']):>10} "
                    f"{_num(b['max']['value']):>10}"
                )
        return "\n".join(out)

    # --- logs ----------------------------------------------------------------------

    def search_logs(
        self,
        query: str | None = None,
        contains: str | None = None,
        service: str | None = None,
        window: str = "after",
        limit: int = 15,
    ) -> str:
        """Log lines grouped by exact text, most frequent first, each with how often it
        appeared before the onset (a line that is new after the onset is a lead)."""
        limit = max(1, min(limit, 50))
        must: list[dict[str, Any]] = []
        if query:
            must.append({"simple_query_string": {"query": query, "fields": ["message"]}})
        if contains:
            # Full text misses parts of dotted words ("java.lang.NullPointerException" is
            # one token), so a substring match on the exact line is offered too.
            must.append(
                {
                    "wildcard": {
                        "message.raw": {
                            "value": f"*{_escape_wildcard(contains)}*",
                            "case_insensitive": True,
                        }
                    }
                }
            )
        filters = [*_service_filter("container_name", service)]
        q = {"bool": {"must": must, "filter": [*filters, _window(self.onset_ms, window)]}}
        r = self.data.search(
            "logs",
            size=0,
            track_total_hits=True,
            query=q,
            aggs={
                "c": {"terms": {"field": "container_name", "size": 30}},
                "m": {
                    "terms": {"field": "message.raw", "size": limit},
                    "aggs": {
                        "first": {
                            "top_hits": {
                                "size": 1,
                                "sort": [{"@timestamp": "asc"}],
                                "_source": ["container_name", "@timestamp"],
                            }
                        }
                    },
                },
                "long": {"missing": {"field": "message.raw"}},
            },
        )
        total = r["hits"]["total"]["value"]
        if not total:
            return "no log lines match"
        aggs = r["aggregations"]
        groups = aggs["m"]["buckets"]
        before: dict[str, int] = {}
        if window == "after" and groups:
            rb = self.data.search(
                "logs",
                size=0,
                query={
                    "bool": {"must": must, "filter": [*filters, _window(self.onset_ms, "before")]}
                },
                aggs={
                    "m": {
                        "terms": {
                            "field": "message.raw",
                            "include": [g["key"] for g in groups],
                            "size": len(groups),
                        }
                    }
                },
            )
            before = {b["key"]: b["doc_count"] for b in rb["aggregations"]["m"]["buckets"]}
        containers = ", ".join(f"{b['key']} {b['doc_count']}" for b in aggs["c"]["buckets"])
        out = [f"{total} lines in window={window}; by container: {containers}"]
        out.append("count [before] | first seen: container, at_ms (t) | message")
        for g in groups:
            hit = g["first"]["hits"]["hits"][0]["_source"]
            at = int(hit["@timestamp"])
            b = f" [{before.get(g['key'], 0)}]" if window == "after" else ""
            out.append(
                f"{g['doc_count']}{b} | {hit['container_name']}, at_ms={at} "
                f"({_rel(at, self.onset_ms)}) | {_clip(g['key'])}"
            )
        if n_long := aggs["long"]["doc_count"]:
            r2 = self.data.search(
                "logs",
                size=3,
                query={
                    "bool": {
                        "must": must,
                        "filter": [*q["bool"]["filter"]],
                        "must_not": [{"exists": {"field": "message.raw"}}],
                    }
                },
                sort=[{"@timestamp": "asc"}],
                _source=["container_name", "@timestamp", "message"],
            )
            out.append(f"{n_long} lines over 2048 chars are not grouped; the first ones:")
            for h in r2["hits"]["hits"]:
                s = h["_source"]
                at = int(s["@timestamp"])
                out.append(
                    f"1 | {s['container_name']}, at_ms={at} ({_rel(at, self.onset_ms)}) | "
                    f"{_clip(s['message'], 600)}"
                )
        return "\n".join(out)

    # --- traces --------------------------------------------------------------------

    def trace_stats(self, service: str | None = None, limit: int = 25) -> str:
        """Latency and errors per (service, operation), before vs after the onset,
        largest p90 increase first."""
        limit = max(1, min(limit, 100))
        side = {
            "n": {"value_count": {"field": "duration"}},
            "p": {"percentiles": {"field": "duration", "percents": [50, 90]}},
            "errors": {"filter": ERROR_STATUS},
        }
        r = self.data.search(
            "traces",
            size=0,
            query={
                "bool": {
                    "filter": [
                        *_service_filter("serviceName", service),
                        _window(self.onset_ms, "all"),
                    ]
                }
            },
            aggs={
                "s": {
                    "terms": {"field": "serviceName", "size": 200},
                    "aggs": {
                        "o": {
                            "terms": {"field": "operationName", "size": 200, "missing": "-"},
                            "aggs": {
                                "before": {
                                    "filter": _window(self.onset_ms, "before"),
                                    "aggs": side,
                                },
                                "after": {"filter": _window(self.onset_ms, "after"), "aggs": side},
                            },
                        }
                    },
                }
            },
        )
        rows = []
        for sb in r["aggregations"]["s"]["buckets"]:
            for ob in sb["o"]["buckets"]:
                bf, af = ob["before"], ob["after"]
                p50b, p90b = (bf["p"]["values"].get(k) for k in ("50.0", "90.0"))
                p50a, p90a = (af["p"]["values"].get(k) for k in ("50.0", "90.0"))
                ratio = (p90a / p90b) if p90a and p90b else (math.inf if p90a else 0.0)
                rows.append((ratio, sb["key"], ob["key"], bf, af, p50b, p90b, p50a, p90a))
        if not rows:
            return "no spans match"
        rows.sort(key=lambda t: -t[0] if math.isfinite(t[0]) else -1e18)
        ms = 1000.0  # durations are in microseconds
        out = [
            "per service and operation, before -> after the onset; latency in ms; "
            "largest p90 ratio first",
            f"{'service':<28} {'operation':<40} {'spans':>13} {'errors':>11} "
            f"{'p50':>15} {'p90':>15} {'p90x':>6}",
        ]
        for ratio, svc, op, bf, af, p50b, p90b, p50a, p90a in rows[:limit]:

            def lat(a: float | None, b: float | None) -> str:
                return f"{_num(a / ms if a else None)}->{_num(b / ms if b else None)}"

            out.append(
                f"{svc:<28} {_clip(op, 40):<40} {bf['n']['value']:>6}->{af['n']['value']:<6} "
                f"{bf['errors']['doc_count']:>5}->{af['errors']['doc_count']:<5} "
                f"{lat(p50b, p50a):>15} {lat(p90b, p90a):>15} {_num(ratio, 2):>6}"
            )
        if len(rows) > limit:
            out.append(f"... {len(rows) - limit} more (raise limit, or filter by service)")
        return "\n".join(out)

    def find_spans(
        self,
        service: str | None = None,
        operation: str | None = None,
        errors_only: bool = False,
        limit: int = 10,
    ) -> str:
        """Spans after the onset, slowest first: ids to cite, traces to open."""
        limit = max(1, min(limit, 50))
        filters = [*_service_filter("serviceName", service), _window(self.onset_ms, "after")]
        if operation:
            filters.append({"term": {"operationName": operation}})
        if errors_only:
            filters.append(ERROR_STATUS)
        r = self.data.search(
            "traces",
            size=limit,
            track_total_hits=True,
            query={"bool": {"filter": filters}},
            sort=[{"duration": {"order": "desc", "unmapped_type": "long"}}],
            _source=[
                "spanID",
                "traceID",
                "serviceName",
                "operationName",
                "duration",
                "statusCode",
                "@timestamp",
            ],
        )
        hits = r["hits"]["hits"]
        if not hits:
            return "no spans match"
        out = [
            f"{r['hits']['total']['value']} spans match after the onset; slowest {len(hits)}:",
            "span_id | trace_id | service | operation | duration_ms | status | t",
        ]
        for h in hits:
            s = h["_source"]
            out.append(
                f"{s['spanID']} | {s['traceID']} | {s['serviceName']} | "
                f"{_clip(str(s.get('operationName')), 60)} | {_num(s['duration'] / 1000)} | "
                f"{s.get('statusCode')} | {_rel(int(s['@timestamp']), self.onset_ms)}"
            )
        return "\n".join(out)

    def trace_tree(self, trace_id: str) -> str:
        """One trace as a call tree, with each span's own time (its duration minus its
        children's): where a slow request actually spent its time."""
        r = self.data.search(
            "traces",
            size=200,
            query={"term": {"traceID": trace_id}},
            sort=[{"@timestamp": "asc"}],
            _source=[
                "spanID",
                "parentSpanID",
                "serviceName",
                "operationName",
                "duration",
                "statusCode",
                "@timestamp",
            ],
        )
        spans = [h["_source"] for h in r["hits"]["hits"]]
        if not spans:
            raise ToolError(f"no trace {trace_id!r}")
        by_id = {s["spanID"]: s for s in spans}
        kids: dict[str | None, list[dict[str, Any]]] = {}
        for s in spans:
            parent = s.get("parentSpanID")
            kids.setdefault(parent if parent in by_id else None, []).append(s)
        out = [
            f"trace {trace_id}: {len(spans)} spans. span_id service operation: "
            "duration_ms (own ms) status"
        ]

        def walk(s: dict[str, Any], depth: int) -> None:
            if len(out) > 120:
                return
            children = kids.get(s["spanID"], [])
            own = s["duration"] - sum(c["duration"] for c in children)
            op = _clip(str(s.get("operationName")), 60)
            out.append(
                f"{'  ' * depth}{s['spanID']} {s['serviceName']} {op}: "
                f"{_num(s['duration'] / 1000)} ({_num(max(own, 0) / 1000)}) {s.get('statusCode')}"
            )
            for c in children:
                walk(c, depth + 1)

        for root in kids.get(None, []):
            walk(root, 0)
        return "\n".join(out)


# --- the tool table the model sees --------------------------------------------------

_STR = {"type": "string"}
_INT = {"type": "integer"}


def _spec(
    name: str, description: str, props: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }


SUBMIT = "submit_report"

TOOL_SPECS: list[dict[str, Any]] = [
    _spec(
        "overview",
        "Start here. Lists the services seen in metrics (with their metric families), log "
        "lines per container and spans (and error spans) per service, before and after the "
        "onset.",
        {},
        [],
    ),
    _spec(
        "metric_changes",
        "Metrics ranked by how much they changed after the onset, scored like the BARO "
        "baseline: (value after - median before) / interquartile range before. Filter by "
        "service (the metric name prefix) and/or family (cpu, mem, diskio, socket, "
        "workload, error, latency-50, latency-90).",
        {"service": _STR, "family": _STR, "limit": _INT},
        [],
    ),
    _spec(
        "metric_series",
        "One metric over the 20-minute window, per step (seconds, default 30), relative to "
        "the onset.",
        {"metric": _STR, "step_s": _INT},
        ["metric"],
    ),
    _spec(
        "search_logs",
        "Log lines grouped by exact text, most frequent first, with each group's count "
        "before the onset when window='after' (the default; or 'before', 'all'). query: "
        'full-text search on the message (simple_query_string: words, "phrases", a|b, '
        "-word, prefix*). contains: a case-insensitive substring of the line (finds parts "
        "of dotted names like Java exceptions, which full text does not). service: a "
        "container name.",
        {
            "query": _STR,
            "contains": _STR,
            "service": _STR,
            "window": {"type": "string", "enum": ["after", "before", "all"]},
            "limit": _INT,
        },
        [],
    ),
    _spec(
        "trace_stats",
        "Span latency (p50, p90) and error counts per service and operation, before -> "
        "after the onset, largest p90 increase first.",
        {"service": _STR, "limit": _INT},
        [],
    ),
    _spec(
        "find_spans",
        "Spans after the onset, slowest first, with span and trace ids. Filter by service, "
        "exact operation, errors_only.",
        {"service": _STR, "operation": _STR, "errors_only": {"type": "boolean"}, "limit": _INT},
        [],
    ),
    _spec(
        "trace_tree",
        "One trace as a call tree with each span's own time (duration minus its children's): "
        "where a slow request spent its time.",
        {"trace_id": _STR},
        ["trace_id"],
    ),
    _spec(
        SUBMIT,
        "Your final answer; call it once, when the investigation is done. It ends the "
        "investigation.",
        {
            "root_causes": {
                "type": "array",
                "items": _STR,
                "description": "Service names, most likely first, up to 5.",
            },
            "fault_type": {"type": "string", "enum": list(ALL_FAULTS)},
            "evidence": {
                "type": "array",
                "description": (
                    "1-5 items you saw in tool output, copied exactly. kind=log: container, "
                    "at_ms, excerpt (verbatim text from that one line, at least 10 "
                    "characters). kind=span: span_id. kind=metric: metric, direction "
                    "(up/down)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["log", "span", "metric"]},
                        "container": _STR,
                        "at_ms": _INT,
                        "excerpt": _STR,
                        "span_id": _STR,
                        "metric": _STR,
                        "direction": {"type": "string", "enum": ["up", "down"]},
                    },
                    "required": ["kind"],
                },
            },
            "summary": {"type": "string", "description": "Two or three sentences: why."},
        },
        ["root_causes", "fault_type", "evidence", "summary"],
    ),
]


def dispatch(tools: CaseTools, name: str, args: dict[str, Any]) -> str:
    """Run one telemetry tool. Raises ToolError for a bad call, ToolInfraError for
    Elasticsearch failing."""
    fns: dict[str, Callable[..., str]] = {
        "overview": tools.overview,
        "metric_changes": tools.metric_changes,
        "metric_series": tools.metric_series,
        "search_logs": tools.search_logs,
        "trace_stats": tools.trace_stats,
        "find_spans": tools.find_spans,
        "trace_tree": tools.trace_tree,
    }
    fn = fns.get(name)
    if fn is None:
        raise ToolError(f"unknown tool {name!r}")
    try:
        inspect.signature(fn).bind(**args)
    except TypeError as e:  # wrong or missing arguments
        raise ToolError(f"bad arguments for {name}: {e}") from e
    text = fn(**args)
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS].rsplit("\n", 1)[0]
        text = cut + f"\n[... cut at {MAX_CHARS} chars: filter or lower the limit]"
    return text
