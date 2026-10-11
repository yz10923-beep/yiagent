"""Checks run on the VM against the real download: it matches the frozen labels, what the
files actually contain, and whether anything in them could leak the answer."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from yi_agent.data.rcaeval import CaseLabel
from yi_agent.data.telemetry import TIME_COLUMN, TimeNormalizer, read_window, source_paths


def check_download(data_dir: Path, labels: list[CaseLabel]) -> list[str]:
    """Problems found; empty means the download agrees with evals/grader/labels.json."""
    index_path = data_dir / "cases.parquet"
    if not index_path.is_file():
        return [f"no cases.parquet in {data_dir}: run `yi data fetch`"]
    rows = [r for r in pq.read_table(index_path).to_pylist() if r["case"][:3] in ("re2", "re3")]
    by_name = {r["case"]: r for r in rows}
    problems: list[str] = []
    names = {c.name for c in labels}
    if missing := sorted(names - by_name.keys()):
        problems.append(f"{len(missing)} labelled cases not in cases.parquet: {missing[:5]}")
    if extra := sorted(by_name.keys() - names):
        problems.append(f"{len(extra)} cases in cases.parquet not labelled: {extra[:5]}")
    for c in labels:
        r = by_name.get(c.name)
        if r is None:
            continue
        # The name is the label: the index must say the same thing.
        if r["root_cause_service"] != c.service or r["fault"] != c.fault:
            problems.append(
                f"{c.name}: index says {r['root_cause_service']}/{r['fault']}, name says "
                f"{c.service}/{c.fault}"
            )
        d = data_dir / c.name
        if not d.is_dir():
            problems.append(f"{c.name}: directory missing")
            continue
        try:
            w = read_window(d)
        except Exception as e:
            problems.append(f"{c.name}: {e}")
            continue
        if w.onset_ms != r["inject_time"] * 1000:
            problems.append(f"{c.name}: inject_time.txt differs from the index")
        has = source_paths(d)
        for kind, flag in (("logs", "has_logs"), ("traces", "has_traces")):
            if (kind in has) != bool(r[flag]):
                problems.append(f"{c.name}: {kind}.parquet present={kind in has}, index {flag}={r[flag]}")
    return problems


def _short(v: Any, n: int = 120) -> str:
    s = repr(v)
    return s if len(s) <= n else s[:n] + "..."


def inspect_case(case_dir: Path) -> str:
    """Schema, row counts, time units and two sample rows per file, for a human to read."""
    w = read_window(case_dir)
    out = [
        f"window {(w.end_ms - w.start_ms) / 60000:.1f} min, onset at "
        f"+{(w.onset_ms - w.start_ms) / 1000:.0f}s, sources {w.sources}"
    ]
    for kind, path in source_paths(case_dir).items():
        pf = pq.ParquetFile(path)
        schema = pf.schema_arrow
        out.append(f"[{kind}] {pf.metadata.num_rows} rows, {len(schema)} columns")
        cols = [f"{f.name}:{f.type}" for f in schema]
        out.append("  columns: " + ", ".join(cols[:14]) + (" ..." if len(cols) > 14 else ""))
        if kind == "metrics":
            names = [f.name for f in schema if f.name != "time"]
            out.append(f"  metric names (sample): {names[:8]} ... {names[-4:]}")
        first = next(pf.iter_batches(batch_size=2))
        norm = TimeNormalizer()
        ms = norm(first.column(TIME_COLUMN[kind]))
        out.append(f"  time unit: {norm.unit}; first rows at ms {ms}")
        for row in first.to_pylist():
            items = list(row.items())[:12]
            out.append("  row: {" + ", ".join(f"{k}: {_short(v, 80)}" for k, v in items) + "}")
    return "\n".join(out)


# Things that would give the answer away if the agent could see them: the case's own name
# (or any case name), and traces of the fault injector itself.
LEAK_PATTERNS: dict[str, str] = {
    "case name": r"\bre[123](ob|ss|tt)_",
    "chaos": r"chaos",
    "stress tool": r"stress-ng|stressng|\bstress\b",
    "network injector": r"toxiproxy|pumba|netem|tc qdisc|\btc\b.*\bdelay\b",
    "inject": r"inject",
    "fault": r"fault",
}


@dataclass
class AuditResult:
    cases: int = 0
    # pattern -> source ("logs.message", "metrics:columns", ...) -> [hits, cases with hits]
    hits: dict[str, dict[str, list[int]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(lambda: [0, 0]))
    )
    examples: dict[tuple[str, str], list[str]] = field(default_factory=lambda: defaultdict(list))

    def render(self) -> str:
        lines = [f"audited {self.cases} cases"]
        for pat in LEAK_PATTERNS:
            for source, (n, k) in sorted(self.hits.get(pat, {}).items()):
                lines.append(f"  {pat:16} {source:28} {n:>9} hits in {k:>3} cases")
                for ex in self.examples[(pat, source)]:
                    lines.append(f"      e.g. {ex}")
        if len(lines) == 1:
            lines.append("  no hits")
        return "\n".join(lines)


def audit_case(case_dir: Path, result: AuditResult) -> None:
    result.cases += 1
    for kind, path in source_paths(case_dir).items():
        table = pq.read_table(path)
        texts: dict[str, pa.ChunkedArray] = {
            f"{kind}.{f.name}": table.column(f.name)
            for f in table.schema
            if pa.types.is_string(f.type) or pa.types.is_large_string(f.type)
        }
        texts[f"{kind}:columns"] = pa.chunked_array([pa.array(table.schema.names)])
        for source, col in texts.items():
            for pat, regex in LEAK_PATTERNS.items():
                mask = pc.match_substring_regex(col, regex, ignore_case=True)
                n = int(pc.sum(pc.cast(mask, pa.int64())).as_py() or 0)
                if not n:
                    continue
                entry = result.hits[pat][source]
                entry[0] += n
                entry[1] += 1
                ex = result.examples[(pat, source)]
                if len(ex) < 2:
                    found = pc.filter(col, pc.fill_null(mask, False)).to_pylist()[:1]
                    ex += [_short(v) for v in found if not _seen(v, ex)]


def _seen(v: Any, examples: list[str]) -> bool:
    return _short(v) in examples
