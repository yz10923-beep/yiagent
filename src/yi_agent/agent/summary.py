"""The one-call baseline's input (S4 addendum): a fixed summary of one case's telemetry,
computed the same way for every case, with no model in the loop.

It answers "do you even need an agent?": the same model, prompt guide and report as the
agent, but instead of querying it gets this, once. It holds what the agent's first tool
calls would show, with every limit fixed in advance:
- metrics: per service, each metric family's largest rise and fall (BARO's robust z, 10
  minutes after the onset against the 10 before, as `metric_changes` scores them), the
  services ranked by their largest rise; the top metrics by name, so they can be cited;
  seconds without a cpu sample just after the onset (a restart leaves a gap);
- logs: per container, lines and error-looking lines before and after the onset; the
  largest error groups after it; message groups that are new after it (startup lines);
- traces: per service and operation, spans, error spans and p50/p90 duration before and
  after, largest p90 growth first (the agent's trace_stats default); services whose
  error spans grew; the slowest spans after the onset.

It reads the case's Parquet files, not Elasticsearch, so a split can run without
loading it. A case directory's name is its label: nothing here prints a path or a name,
only what the telemetry itself contains (containers, services, metric names, lines).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from yi_agent.data.telemetry import TimeNormalizer, pick_time_column, read_window, source_paths
from yi_agent.eval import baro
from yi_agent.eval.case_data import CaseData, is_error_status
from yi_agent.eval.grounding import ERROR_LINE

# part of the run's condition: bump when the summary changes. v2 (2026-10-14): traces per
# (service, operation) as trace_stats shows them, not per service, which hid which call
# got slow (a delay on emailservice showed only as checkout's p90; dev_small smoke).
SUMMARY_VERSION = 2
HALF_MS = 10 * 60 * 1000
TOP_SERVICES = 10
TOP_METRICS = 15
TOP_DIPS = 5
LOG_GROUPS = 12
NEW_GROUPS = 8
TRACE_OPERATIONS = 25  # trace_stats' default limit
SLOW_SPANS = 8
GAP_LOOK_S = 120
LINE_CHARS = 200
MAX_CHARS = 40_000  # about 12K tokens; the rest is cut, and the cut is said

_VOLATILE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|0x[0-9a-f]+|[0-9a-f]{16,}|\d+",
    re.IGNORECASE,
)


def template(message: str) -> str:
    """A log line with ids and numbers masked, so repeats of one message group together."""
    return _VOLATILE.sub("#", message.split("\n", 1)[0])[:LINE_CHARS]


def _f(x: float) -> str:
    return f"{x:+.1f}" if abs(x) < 1e4 else f"{x:+.1e}"


def _metric_section(data: CaseData, onset_s: int) -> list[str]:
    path = source_paths(data.case_dir)["metrics"]
    table = pq.read_table(path)
    names = [n for n in table.column_names if n not in ("time", "time.1")]
    w = baro.prepare_columns(table, names, onset_s)
    if w.before.shape[0] == 0 or w.after.shape[0] == 0:
        return ["## Metrics", "(no samples on one side of the onset)"]
    q25, med, q75 = np.percentile(w.before, [25, 50, 75], axis=0)
    scale = np.where(q75 - q25 < baro.EPS, 1.0, q75 - q25)
    z = (w.after - med) / scale
    up, down = z.max(axis=0), z.min(axis=0)
    # per service and family, the largest rise and fall over its metrics
    by_service: dict[str, dict[str, tuple[float, float]]] = defaultdict(dict)
    for j, n in enumerate(w.names):
        svc, _, fam = n.partition("_")
        u, d = by_service[svc].get(fam, (-np.inf, np.inf))
        by_service[svc][fam] = (max(u, float(up[j])), min(d, float(down[j])))
    ranked = sorted(by_service, key=lambda s: -max(u for u, _ in by_service[s].values()))
    gaps = _cpu_gaps(table, onset_s)
    out = [
        "## Metrics (robust z: 10 min after the onset against the 10 min before; "
        "rise / fall per metric family)",
        f"Services ranked by their largest rise (top {TOP_SERVICES} of {len(ranked)}):",
    ]
    for s in ranked[:TOP_SERVICES]:
        fams = ", ".join(f"{f} {_f(u)} / {_f(d)}" for f, (u, d) in sorted(by_service[s].items()))
        gap = f"; no cpu sample for {gaps[s]} s of the first {GAP_LOOK_S} s" if gaps.get(s) else ""
        out.append(f"- {s}: {fams}{gap}")
    order = np.argsort(-up)
    out.append("Largest rises (metric name, z):")
    out += [f"- {w.names[j]} {_f(float(up[j]))}" for j in order[:TOP_METRICS]]
    out.append("Largest falls:")
    out += [f"- {w.names[j]} {_f(float(down[j]))}" for j in np.argsort(down)[:TOP_DIPS]]
    return out


def _cpu_gaps(table: pa.Table, onset_s: int) -> dict[str, int]:
    """Seconds without a cpu sample in the first GAP_LOOK_S after the onset, per service."""
    times = table.column("time").to_numpy().astype(np.int64)
    rows = (times >= onset_s) & (times < onset_s + GAP_LOOK_S)
    out: dict[str, int] = {}
    for n in table.column_names:
        if n.endswith("_cpu"):
            vals = table.column(n).to_numpy(zero_copy_only=False).astype(np.float64)[rows]
            seen = len({int(t) for t, v in zip(times[rows], vals, strict=True) if np.isfinite(v)})
            if GAP_LOOK_S - seen > 0:
                out[n.split("_", 1)[0]] = GAP_LOOK_S - seen
    return out


def _log_section(data: CaseData) -> list[str]:
    logs = data.logs
    if logs is None or logs.num_rows == 0:
        return ["## Logs", "(no logs for this incident)"]
    onset = data.onset_ms
    at = logs.column("at_ms")
    near = logs.filter(pc.and_(pc.greater_equal(at, onset - HALF_MS), pc.less(at, onset + HALF_MS)))
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    groups_after: Counter[tuple[str, str]] = Counter()
    groups_before: Counter[tuple[str, str]] = Counter()
    example: dict[tuple[str, str], tuple[str, int, str]] = {}
    error = re.compile(ERROR_LINE)
    for c, m, t in zip(
        near.column("container_name").to_pylist(),
        near.column("message").to_pylist(),
        near.column("at_ms").to_pylist(),
        strict=True,
    ):
        c, m = c or "?", m or ""
        side = "after" if t >= onset else "before"
        is_err = bool(error.search(m))
        counts[c][side] += 1
        counts[c][f"err_{side}"] += is_err
        key = (c, template(m))
        if side == "after":
            groups_after[key] += 1
            if key not in example:
                example[key] = (c, t, m.split("\n", 1)[0][:LINE_CHARS])
        else:
            groups_before[key] += 1
    out = [
        "## Logs (10 min before / after the onset)",
        "Per container: lines before -> after; error-looking lines before -> after:",
    ]
    for c in sorted(counts, key=lambda c: -counts[c]["err_after"]):
        k = counts[c]
        out.append(
            f"- {c}: {k['before']} -> {k['after']}; errors {k['err_before']} -> {k['err_after']}"
        )

    def show(key: tuple[str, str]) -> str:
        c, t, line = example[key]
        return (
            f"- {groups_after[key]} after / {groups_before[key]} before: "
            f"container={c} at_ms={t} line={line!r}"
        )

    errs = [k for k in groups_after if error.search(k[1])]
    errs.sort(key=lambda k: -groups_after[k])
    out.append(f"Largest error-looking groups after the onset (top {LOG_GROUPS} of {len(errs)}):")
    out += [show(k) for k in errs[:LOG_GROUPS]] or ["- none"]
    new = [k for k in groups_after if groups_before[k] == 0 and not error.search(k[1])]
    new.sort(key=lambda k: -groups_after[k])
    out.append(f"Message groups new after the onset (top {NEW_GROUPS} of {len(new)}):")
    out += [show(k) for k in new[:NEW_GROUPS]] or ["- none"]
    return out


def _trace_section(case_dir: Path, onset: int) -> list[str]:
    path = source_paths(case_dir).get("traces")
    if path is None:
        return ["## Traces", "(no traces for this incident)"]
    pf = pq.ParquetFile(path)
    col = pick_time_column("traces", pf)
    have = set(pf.schema_arrow.names)
    cols = [
        c
        for c in (col, "spanID", "serviceName", "operationName", "duration", "statusCode")
        if c in have
    ]
    t = pq.read_table(path, columns=cols)
    start = np.array([-1 if v is None else v for v in TimeNormalizer()(t.column(col))], np.int64)
    svc = t.column("serviceName").to_pylist()
    ops = t.column("operationName").to_pylist() if "operationName" in have else [None] * t.num_rows
    dur_us = t.column("duration").to_numpy(zero_copy_only=False).astype(np.float64)
    status = t.column("statusCode").to_pylist() if "statusCode" in have else [None] * t.num_rows
    timed = np.isfinite(dur_us)  # a span without a duration says nothing about latency
    before = (start >= onset - HALF_MS) & (start < onset) & timed
    after = (start >= onset) & (start < onset + HALF_MS) & timed
    # per (service, operation), as the agent's trace_stats shows them by default
    rows: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    errs: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for i in np.flatnonzero(before | after):
        side = "after" if after[i] else "before"
        key = (svc[i] or "?", ops[i] or "-")
        rows[key][side].append(dur_us[i])
        errs[key][side] += is_error_status(status[i])

    def p(xs: list[float], q: int) -> str:
        return f"{np.percentile(xs, q) / 1000:.1f}" if xs else "-"

    def ratio(k: tuple[str, str]) -> float:
        a, b = rows[k]["after"], rows[k]["before"]
        if not a:
            return 0.0
        if not b:
            return float("inf")
        return float(np.percentile(a, 90) / max(np.percentile(b, 90), 1.0))

    # trace_stats' order: largest p90 ratio first, operations seen only after the onset first
    ranked = sorted(rows, key=lambda k: -1e18 if ratio(k) == float("inf") else -ratio(k))
    out = [
        "## Traces (10 min before / after the onset; durations in ms)",
        f"Per service and operation (top {TRACE_OPERATIONS} of {len(ranked)}, largest p90 "
        "ratio first): spans before -> after; error spans before -> after; p50 before -> "
        "after; p90 before -> after; p90 ratio:",
    ]
    for k in ranked[:TRACE_OPERATIONS]:
        r, e = rows[k], errs[k]
        out.append(
            f"- {k[0]} {k[1]}: {len(r['before'])} -> {len(r['after'])}; "
            f"errors {e['before']} -> {e['after']}; "
            f"p50 {p(r['before'], 50)} -> {p(r['after'], 50)}; "
            f"p90 {p(r['before'], 90)} -> {p(r['after'], 90)}; x{ratio(k):.2f}"
        )
    per_service: Counter[str] = Counter()
    before_errors: Counter[str] = Counter()
    for (s, _), e in errs.items():
        per_service[s] += e["after"]
        before_errors[s] += e["before"]
    grew = [(s, n) for s, n in per_service.most_common() if n > before_errors[s]]
    out.append("Error spans per service, before -> after (services with more after):")
    out += [f"- {s}: {before_errors[s]} -> {n}" for s, n in grew[:TRACE_OPERATIONS]] or ["- none"]
    idx = np.flatnonzero(after)
    slow = idx[np.argsort(-dur_us[idx])][:SLOW_SPANS]
    ids = t.column("spanID").to_pylist()
    out.append("Slowest spans after the onset:")
    out += [
        f"- span_id={ids[i]} service={svc[i]} operation={ops[i]} "
        f"duration={dur_us[i] / 1000:.1f} ms status={status[i]}"
        for i in slow
    ] or ["- none"]
    return out


def build_summary(case_dir: Path) -> str:
    """The fixed summary of one case, at most MAX_CHARS."""
    data = CaseData(case_dir)
    onset = read_window(case_dir).onset_ms
    parts = _metric_section(data, onset // 1000) + [""] + _log_section(data)
    if "traces" in source_paths(case_dir):
        parts += [""] + _trace_section(case_dir, onset)
    text = "\n".join(parts)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n[summary cut at its size limit]"
    return text
