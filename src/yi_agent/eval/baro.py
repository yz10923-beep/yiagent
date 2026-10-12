"""BARO, ported from RCAEval (Copyright (c) 2024 Luan Pham, MIT License; see
LICENSES/LICENSE-RCAEval; `RCAEval/e2e/baro.py` and the data preparation in
`main.py`), with numpy in place of pandas and scikit-learn. Each step mirrors RCAEval's
default `main.py --method baro` run, so the numbers compare with the paper's; the
published RE2-TT numbers are the check (`yi eval reproduce-baro`, docs/ROADMAP.md).

BARO ranks metrics, not services: for each metric, scale the 10 minutes after the onset
by the median and interquartile range of the 10 minutes before, and score it by the
largest value. A service ranks where its first metric does."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from yi_agent.eval.scoring import service_of_metric

HALF_WINDOW_ROWS = 10 * 60  # `--length 20` minutes, half on each side, at 1-second rows
EPS = 10 * np.finfo(np.float64).eps  # scikit-learn's _handle_zeros_in_scale threshold


@dataclass(frozen=True)
class MetricWindow:
    """The metric table as RCAEval prepares it, split at the onset."""

    names: list[str]
    before: np.ndarray  # rows x metrics
    after: np.ndarray


def _ffill_then_zero(x: np.ndarray) -> np.ndarray:
    """pandas `ffill()` then `fillna(0)`, column-wise."""
    x = x.copy()
    n_rows = x.shape[0]
    idx = np.where(np.isnan(x), 0, np.arange(n_rows)[:, None])
    np.maximum.accumulate(idx, axis=0, out=idx)
    filled = x[idx, np.arange(x.shape[1])[None, :]]
    return np.where(np.isnan(filled), 0.0, filled)


def family(metric: str) -> str:
    return metric.split("_", 1)[1] if "_" in metric else ""


def prepare(metrics_path: Path, onset_s: int, exclude: tuple[str, ...] = ()) -> MetricWindow:
    """`exclude` drops metric families (e.g. "workload"): only for diagnosing a gap with
    the published numbers, never for the BARO baseline itself."""
    table = pq.read_table(metrics_path)
    names = [n for n in table.column_names if n not in ("time", "time.1")]
    names = [n for n in names if not n.endswith("_latency-50")]  # "only select lat-90"
    names = [n for n in names if family(n) not in exclude]
    # main.py keeps only `ts-` columns for paths containing "mm-tt" or "torai-TT"; the
    # published RE2-TT run reads data/RE2/RE2-TT/..., so that filter never applied to it.
    return prepare_columns(table, names, onset_s)


def prepare_columns(table: pa.Table, names: list[str], onset_s: int) -> MetricWindow:
    """inf to NaN, forward-fill, then 0 (over the whole case, as main.py does), then the
    10 minutes before and after the onset."""
    times = table.column("time").to_numpy().astype(np.int64)
    if names:
        data = np.column_stack(
            [table.column(n).to_numpy(zero_copy_only=False).astype(np.float64) for n in names]
        )
    else:
        data = np.empty((len(times), 0))
    data[~np.isfinite(data)] = np.nan
    data = _ffill_then_zero(data)
    before_rows = np.flatnonzero(times < onset_s)[-HALF_WINDOW_ROWS:]
    after_rows = np.flatnonzero(times >= onset_s)[:HALF_WINDOW_ROWS]
    # main.py renames `_latency-90` to `_latency`; the service prefix is unchanged by it.
    return MetricWindow(names=names, before=data[before_rows], after=data[after_rows])


def _not_constant(x: np.ndarray) -> np.ndarray:
    """RCAEval's drop_constant: keep a column if any value differs from its first."""
    if x.shape[0] == 0:
        return np.zeros(x.shape[1], dtype=bool)
    return np.asarray((x != x[0]).any(axis=0))


def rank_metrics(w: MetricWindow) -> list[tuple[str, float]]:
    """(metric, score), best first. Constant columns are dropped per half, as in
    RCAEval's preprocess(), and only columns surviving in both halves are scored."""
    keep = _not_constant(w.before) & _not_constant(w.after)
    scored: list[tuple[str, float]] = []
    for j in np.flatnonzero(keep):
        a, b = w.before[:, j], w.after[:, j]
        q25, median, q75 = np.percentile(a, [25, 50, 75])
        scale = q75 - q25
        if scale < EPS:
            scale = 1.0
        scored.append((w.names[j], float(np.max((b - median) / scale))))
    # Python's sort is stable, so ties keep column order, as pandas' sorted() did.
    return sorted(scored, key=lambda t: t[1], reverse=True)


def rank_services(metric_ranks: list[tuple[str, float]]) -> list[str]:
    out: list[str] = []
    for metric, _ in metric_ranks:
        s = service_of_metric(metric)
        if s not in out:
            out.append(s)
    return out


def candidates(w: MetricWindow) -> list[str]:
    """The services RCAEval's chance level counts: prefixes of the columns left after
    dropping constant ones over the whole window (main.py's n_candidates_map)."""
    both = np.vstack([w.before, w.after])
    keep = _not_constant(both)
    return sorted({service_of_metric(n) for n, k in zip(w.names, keep, strict=True) if k})
