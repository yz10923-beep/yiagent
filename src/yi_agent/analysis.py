"""What each fault actually did to its root cause, case by case: the evidence S4's
fault-type changes are designed from, so they target a measured cause, not a guess.

Grader-side: it reads the hidden labels, and nothing under agent/ may import it (a test
checks). It reads the source Parquet, like the graders, and never Elasticsearch.

Per case, for the true root-cause service:
- the largest rise (z_up) and fall (z_down) of each metric family over the 10 minutes
  after the onset, scored as BARO scores it (eval.case_data.metric_z);
- metric gaps: seconds without a cpu sample just after the onset (a restart leaves one);
- log lines, and error-looking log lines, in the 10 minutes before and after the onset;
- error spans after the onset;
- with --variant, what that run's agent said the fault was, and its summary.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from statistics import median

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from yi_agent.data.rcaeval import CaseLabel
from yi_agent.eval import run as eval_run
from yi_agent.eval.case_data import CaseData, is_error_status
from yi_agent.eval.grounding import ERROR_LINE
from yi_agent.eval.harness import RESULTS_DIR
from yi_agent.eval.report import read_trials
from yi_agent.eval.scoring import normalize_service, service_of_metric

FAMILIES = ("cpu", "mem", "diskio", "socket", "latency-90", "error", "workload")
WINDOW_MS = 10 * 60 * 1000
GAP_LOOK_S = 120

type Progress = Callable[[str], None]


def _family(metric: str) -> str:
    return metric.split("_", 1)[1] if "_" in metric else ""


def case_signals(label: CaseLabel, data_dir: Path) -> dict[str, object]:
    data = CaseData(data_dir / label.name)
    rc = normalize_service(label.service)
    out: dict[str, object] = {}
    for fam in FAMILIES:
        ups, downs = [], []
        for name in data.metric_names:
            if service_of_metric(name) == rc and _family(name) == fam:
                z = data.metric_z(name)
                if z is not None:
                    ups.append(z[0])
                    downs.append(z[1])
        out[f"{fam}_up"] = max(ups) if ups else None
        out[f"{fam}_down"] = min(downs) if downs else None
    out["gap_s"] = _cpu_gap_s(data, rc)
    onset = data.onset_ms
    logs = data.logs
    for side in ("before", "after"):
        out[f"logs_{side}"] = 0
        out[f"errlogs_{side}"] = 0
    if logs is not None and logs.num_rows:
        names = logs.column("container_name")
        mine = logs.filter(
            pc.is_in(
                names,
                value_set=pa.array(
                    _containers_of(pc.unique(names).to_pylist(), rc), type=names.type
                ),
            )
        )
        at = mine.column("at_ms")
        for side, lo, hi in (
            ("before", onset - WINDOW_MS, onset),
            ("after", onset, onset + WINDOW_MS),
        ):
            part = mine.filter(pc.and_(pc.greater_equal(at, lo), pc.less(at, hi)))
            out[f"logs_{side}"] = part.num_rows
            out[f"errlogs_{side}"] = (
                part.filter(pc.match_substring_regex(part.column("message"), ERROR_LINE)).num_rows
                if part.num_rows
                else 0
            )
    out["errspans_after"] = sum(
        1
        for s in data.spans.values()
        if s.service == rc and s.start_ms >= onset and is_error_status(s.status)
    )
    return out


def _containers_of(names: list[str | None], rc: str) -> list[str]:
    return sorted({n for n in names if n and normalize_service(n) == rc})


def _cpu_gap_s(data: CaseData, rc: str) -> int | None:
    """Seconds without a cpu sample in the first two minutes after the onset."""
    path = data.case_dir / "metrics.parquet"
    names = [n for n in data.metric_names if service_of_metric(n) == rc and _family(n) == "cpu"]
    if not names:
        return None
    t = pq.read_table(path, columns=["time", names[0]])
    onset_s = data.onset_ms // 1000
    times = t.column("time").to_pylist()
    vals = t.column(names[0]).to_pylist()
    seen = {
        int(ts)
        for ts, v in zip(times, vals, strict=True)
        if v is not None and v == v and onset_s <= int(ts) < onset_s + GAP_LOOK_S
    }
    return GAP_LOOK_S - len(seen)


def _fmt(x: object) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.3g}" if abs(x) < 1e5 else f"{x:.1e}"
    return str(x)


def fault_signals(
    split: str,
    data_dir: Path,
    variant: str | None = None,
    progress: Progress | None = None,
    results_dir: Path | None = None,
) -> str:
    labels = eval_run.labels_by_id()
    ids = eval_run.split_ids(split, labels)
    said: dict[str, tuple[str | None, str]] = {}
    if variant:
        path = (results_dir or RESULTS_DIR) / variant / split / "trials.jsonl"
        for t in read_trials(path):
            if t.report is not None:
                said[t.case_id] = (t.report.fault_type, t.report.summary)
    cols = [f"{f}_up" for f in FAMILIES] + ["mem_down", "cpu_down", "gap_s"]
    cols += ["logs_before", "logs_after", "errlogs_before", "errlogs_after", "errspans_after"]
    short = {
        "cpu_up": "cpu_up",
        "mem_up": "mem_up",
        "diskio_up": "disk_up",
        "socket_up": "sock_up",
        "latency-90_up": "lat_up",
        "error_up": "err_up",
        "workload_up": "wl_up",
        "mem_down": "mem_dn",
        "cpu_down": "cpu_dn",
        "logs_before": "logs_b",
        "logs_after": "logs_a",
        "errlogs_before": "errl_b",
        "errlogs_after": "errl_a",
        "errspans_after": "espan_a",
    }
    w = 9  # every short name is under 9 characters, so columns never run together
    head = f"{'case':<12}{'fault':<7}{'said':<7}" + "".join(f"{short.get(c, c):>{w}}" for c in cols)
    rows: dict[str, list[dict[str, object]]] = defaultdict(list)
    lines = [head]
    summaries: list[str] = []
    for n, i in enumerate(sorted(ids, key=lambda i: (labels[i].fault, i)), 1):
        c = labels[i]
        sig = case_signals(c, data_dir)
        guess, summary = said.get(i, (None, ""))
        sig["said"] = guess
        rows[c.fault].append(sig)
        lines.append(
            f"{i:<12}{c.fault:<7}{_fmt(guess):<7}" + "".join(f"{_fmt(sig[k]):>{w}}" for k in cols)
        )
        if guess is not None and guess != c.fault:
            summaries.append(f"  {i} {c.fault} -> {guess}: {re.sub(r'\s+', ' ', summary)[:300]}")
        if progress and n % 10 == 0:
            progress(f"  fault-signals: {n}/{len(ids)} cases")
    lines += ["", "median per fault (n; how often the agent named the fault right):"]
    lines.append(
        f"{'fault':<7}{'n':>3}{'right':>7}" + "".join(f"{short.get(c, c):>9}" for c in cols)
    )
    for fault, rs in sorted(rows.items()):
        right = sum(r["said"] == fault for r in rs)
        meds = []
        for k in cols:
            vals = [float(v) for r in rs if isinstance((v := r[k]), int | float)]
            meds.append(_fmt(median(vals)) if vals else "-")
        lines.append(
            f"{fault:<7}{len(rs):>3}{f'{right}/{len(rs)}' if said else '-':>7}"
            + "".join(f"{m:>{w}}" for m in meds)
        )
    if summaries:
        lines += ["", "the agent's reasoning where it named the wrong fault:", *summaries]
    return "\n".join(lines)


FAMILY = {
    **dict.fromkeys(("cpu", "mem", "disk", "socket"), "resource"),
    **dict.fromkeys(("delay", "loss"), "network"),
    **dict.fromkeys(("f1", "f2", "f3", "f4", "f5"), "code"),
}


def confusion(split: str, variants: list[str], results_dir: Path | None = None) -> str:
    """Per variant, the true fault against the fault the agent named, and how often it
    named at least the right family (resource, network, code). Diagnostic only: the
    graded fault score is the harness's macro-F1."""
    labels = eval_run.labels_by_id()
    ids = eval_run.split_ids(split, labels)
    out: list[str] = []
    for v in variants:
        path = (results_dir or RESULTS_DIR) / v / split / "trials.jsonl"
        if not path.exists():
            out += [f"{v}: no trials at {path}", ""]
            continue
        said = {t.case_id: (t.report.fault_type if t.report else None) for t in read_trials(path)}
        pairs: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        exact = family = 0
        for i in ids:
            truth, guess = labels[i].fault, said.get(i)
            pairs[truth][guess or "none"] += 1
            exact += guess == truth
            family += guess is not None and FAMILY.get(guess) == FAMILY[truth]
        n = len(ids)
        out.append(f"{v}: fault right {exact}/{n}, family right {family}/{n}")
        for truth in sorted(pairs):
            row = sorted(pairs[truth].items(), key=lambda kv: -kv[1])
            got = ", ".join(f"{g} {k}" for g, k in row)
            out.append(f"  {truth:<7}({FAMILY[truth]:<8}) -> {got}")
        out.append("")
    return "\n".join(out).rstrip()
