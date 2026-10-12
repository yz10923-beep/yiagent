"""`yi`: the command line. `yi data ...` (S1: fetch, check, inspect, audit, load, unload) and
`yi eval ...` (S2: baseline, score, reproduce-baro, evidence-coverage, metric-kinds)."""

from __future__ import annotations

import argparse
import io
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from elasticsearch import ConnectionError as ESConnectionError
from elasticsearch import Elasticsearch

from yi_agent.config import ConfigError, Settings, load_settings
from yi_agent.data.alert import make_alert
from yi_agent.data.check import AuditResult, audit_case, check_download, inspect_case
from yi_agent.data.es_load import (
    LOADER_VERSION,
    LoadError,
    ensure_loaded,
    load_case,
    unload_case,
)
from yi_agent.data.rcaeval import LABELS_PATH, SPLIT_PATH, CaseLabel, fetch, read_labels
from yi_agent.data.split import read_split, split_hash
from yi_agent.data.telemetry import TelemetryError, read_window
from yi_agent.eval import baselines
from yi_agent.eval import run as eval_run
from yi_agent.eval.grade import RunInvalid
from yi_agent.eval.harness import HarnessNotApproved

SPLITS = ("dev", "dev_small", "test", "all")


def _labels() -> dict[str, CaseLabel]:
    return {c.id: c for c in read_labels(LABELS_PATH)}


def _ids(args: argparse.Namespace, labels: dict[str, CaseLabel]) -> list[str]:
    if args.ids:
        unknown = [i for i in args.ids if i not in labels]
        if unknown:
            raise SystemExit(f"unknown case ids: {unknown}")
        return list(args.ids)
    if args.split == "all":
        return sorted(labels)
    ids: list[str] = getattr(read_split(SPLIT_PATH), args.split)
    return ids


def _es(settings: Settings) -> Elasticsearch:
    return Elasticsearch(settings.es_url, request_timeout=120)


def cmd_fetch(settings: Settings, args: argparse.Namespace) -> int:
    print(f"downloading RE2 + RE3 into {settings.data_dir} (about 3 GB, resumable)")
    fetch(settings.data_dir)
    print("done; next: yi data check")
    return 0


def cmd_check(settings: Settings, args: argparse.Namespace) -> int:
    labels = _labels()
    problems = check_download(settings.data_dir, list(labels.values()))
    split = read_split(SPLIT_PATH)
    print(f"split.json sha256 {split_hash(SPLIT_PATH.read_text())}")
    for part in ("dev", "dev_small", "test"):
        ids: list[str] = getattr(split, part)
        by = Counter(f"{labels[i].suite}-{labels[i].system}" for i in ids)
        print(
            f"  {part:9} {len(ids):>3} cases  "
            + "  ".join(f"{k}:{v}" for k, v in sorted(by.items()))
        )
    for p in problems:
        print("PROBLEM", p)
    print(f"{len(labels)} cases checked, {len(problems)} problems")
    return 1 if problems else 0


def cmd_inspect(settings: Settings, args: argparse.Namespace) -> int:
    labels = _labels()
    ids = args.ids
    if not ids:  # one dev_small case per suite and system
        seen: dict[tuple[str, str], str] = {}
        for i in read_split(SPLIT_PATH).dev_small:
            seen.setdefault((labels[i].suite, labels[i].system), i)
        ids = list(seen.values())
    for i in ids:
        c = labels[i]
        print(f"=== {i} ({c.suite}-{c.system})")
        print(inspect_case(settings.data_dir / c.name))
    return 0


def cmd_audit(settings: Settings, args: argparse.Namespace) -> int:
    labels = _labels()
    result = AuditResult()
    ids = _ids(args, labels)
    for n, i in enumerate(ids, 1):
        audit_case(settings.data_dir / labels[i].name, result)
        if n % 20 == 0:
            print(f"  {n}/{len(ids)}", file=sys.stderr)
    print(result.render())
    return 0


def cmd_load(settings: Settings, args: argparse.Namespace) -> int:
    labels = _labels()
    es = _es(settings)
    total_docs, total_s, failed = 0, 0.0, []
    ids = _ids(args, labels)
    loader = ensure_loaded if args.keep else load_case

    def progress(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    for n, i in enumerate(ids, 1):
        case_dir = settings.data_dir / labels[i].name
        progress(f"[{n}/{len(ids)}] {i} ({labels[i].suite}-{labels[i].system}) ...")
        try:
            report = loader(es, i, case_dir, progress)
        except (LoadError, TelemetryError) as e:
            print(f"{i}: FAILED {e}")
            failed.append(i)
            continue
        total_docs += report.docs
        total_s += report.seconds
        units = {k: f"{s.time_column}:{s.time_unit}" for k, s in report.stats.items()}
        skipped = sum(s.skipped_nonfinite + s.skipped_no_time for s in report.stats.values())
        if report.reused:
            print(f"{i}: {report.docs:>8} docs already loaded (complete, loader v{LOADER_VERSION})")
            continue
        print(
            f"{i}: {report.docs:>8} docs in {report.seconds:5.1f}s "
            f"({report.docs / max(report.seconds, 1e-9):,.0f}/s) {report.indices} "
            f"time units {units} skipped cells {skipped}",
            flush=True,
        )
        if args.show_alert:
            print(make_alert(labels[i], read_window(case_dir)).render())
        if not args.keep:
            unload_case(es, i)
    if total_s:
        print(f"total {total_docs} docs in {total_s:.1f}s ({total_docs / total_s:,.0f}/s)")
    if failed:
        print(f"{len(failed)} cases failed to load: {failed}")
    return 1 if failed else 0


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def cmd_eval_baseline(settings: Settings, args: argparse.Namespace) -> int:
    path, table = eval_run.run_baseline(
        args.name,
        args.split,
        settings.data_dir,
        approve=args.approve_harness,
        variant=args.variant,
        seed=args.seed,
        progress=_progress,
    )
    print(table)
    print(f"trials: {path}\nscores: {path.parent / 'scores.json'}")
    return 0


def cmd_eval_score(settings: Settings, args: argparse.Namespace) -> int:
    table, _ = eval_run.score_run(args.trials, args.split, settings.data_dir, args.approve_harness)
    print(table)
    return 0


def cmd_eval_reproduce_baro(settings: Settings, args: argparse.Namespace) -> int:
    print(
        eval_run.reproduce_baro(
            args.data_dir or settings.data_dir,
            args.systems,
            progress=_progress,
            exclude=tuple(args.exclude),
            show_misses=args.show_misses,
            metrics_file=args.metrics_file,
        )
    )
    return 0


def cmd_eval_coverage(settings: Settings, args: argparse.Namespace) -> int:
    print(eval_run.evidence_coverage(settings.data_dir, args.split, progress=_progress))
    return 0


def cmd_eval_metric_kinds(settings: Settings, args: argparse.Namespace) -> int:
    print(eval_run.metric_kinds(settings.data_dir, args.split))
    return 0


def cmd_unload(settings: Settings, args: argparse.Namespace) -> int:
    es = _es(settings)
    for i in args.ids:
        unload_case(es, i)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="yi", description=__doc__)
    sub = p.add_subparsers(dest="group", required=True)
    data = sub.add_parser("data", help="benchmark data").add_subparsers(dest="cmd", required=True)

    data.add_parser("fetch", help="download RE2 + RE3 from Hugging Face").set_defaults(fn=cmd_fetch)
    data.add_parser("check", help="verify the download against the frozen labels").set_defaults(
        fn=cmd_check
    )
    c = data.add_parser("inspect", help="schemas, time units, sample rows")
    c.add_argument("ids", nargs="*")
    c.set_defaults(fn=cmd_inspect)
    for name, fn, help_ in (
        ("audit", cmd_audit, "scan telemetry for anything that could leak the answer"),
        ("load", cmd_load, "load cases into Elasticsearch, verify counts, then unload"),
    ):
        c = data.add_parser(name, help=help_)
        c.add_argument("ids", nargs="*", help="case ids (default: the whole --split)")
        c.add_argument("--split", choices=SPLITS, default="dev_small")
        if name == "load":
            c.add_argument(
                "--keep",
                action="store_true",
                help="keep the indices, and reuse a complete earlier load instead of reloading",
            )
            c.add_argument("--show-alert", action="store_true")
        c.set_defaults(fn=fn)
    c = data.add_parser("unload", help="delete a case's indices")
    c.add_argument("ids", nargs="+")
    c.set_defaults(fn=cmd_unload)

    ev = sub.add_parser("eval", help="graders and baselines").add_subparsers(
        dest="cmd", required=True
    )
    approve_help = "approve the current harness hash (Yi only, after reviewing the diff)"
    c = ev.add_parser("baseline", help="run a non-LLM baseline on a split and score it")
    c.add_argument("name", choices=baselines.NAMES)
    c.add_argument("--split", choices=eval_run.SPLITS, default="dev")
    c.add_argument("--variant", help="results directory name (default: the baseline's)")
    c.add_argument("--seed", type=int, default=0)
    c.add_argument("--approve-harness", action="store_true", help=approve_help)
    c.set_defaults(fn=cmd_eval_baseline)
    c = ev.add_parser("score", help="grade a trials.jsonl over a whole split")
    c.add_argument("trials", type=Path)
    c.add_argument("--split", choices=eval_run.SPLITS, default="dev")
    c.add_argument("--approve-harness", action="store_true", help=approve_help)
    c.set_defaults(fn=cmd_eval_score)
    c = ev.add_parser("reproduce-baro", help="BARO on all RE2 cases vs the published numbers")
    c.add_argument("--systems", nargs="+", choices=("ob", "ss", "tt"), default=["tt"])
    c.add_argument("--exclude", nargs="*", default=[], help="metric families to drop (diagnosis)")
    c.add_argument("--show-misses", action="store_true", help="top metrics where BARO misses")
    c.add_argument("--data-dir", type=Path, help="case directories (default: YI_DATA_DIR)")
    c.add_argument(
        "--metrics-file", type=Path, help="read this file per case, e.g. simple_metrics.csv"
    )
    c.set_defaults(fn=cmd_eval_reproduce_baro)
    c = ev.add_parser("evidence-coverage", help="does each case's root-cause indicator exist?")
    c.add_argument("--split", choices=eval_run.SPLITS, default="dev")
    c.set_defaults(fn=cmd_eval_coverage)
    c = ev.add_parser("metric-kinds", help="metric families each system exports")
    c.add_argument("--split", choices=eval_run.SPLITS, default="dev_small")
    c.set_defaults(fn=cmd_eval_metric_kinds)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    # Progress goes to stderr, unbuffered; through `| tee` stdout would be block-buffered
    # and each case's result and alert would print after the next case's progress.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    try:
        rc: int = args.fn(settings, args)
    except ESConnectionError:
        print(
            f"Elasticsearch not reachable at {settings.es_url}: run `make es-up`", file=sys.stderr
        )
        return 2
    except HarnessNotApproved as e:
        print(str(e), file=sys.stderr)
        return 2
    except RunInvalid as e:
        print(f"run is invalid, not scored: {e}", file=sys.stderr)
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
