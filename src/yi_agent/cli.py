"""`yi`: the command line. `yi data ...` (S1: fetch, check, inspect, audit, load, unload),
`yi data bank ...` (S5: OpenRCA Bank), `yi eval ...` (S2: baseline, score, reproduce-baro,
evidence-coverage, metric-kinds) and `yi agent run` (S3)."""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from elasticsearch import ConnectionError as ESConnectionError
from elasticsearch import Elasticsearch

from yi_agent.agent.policies import POLICIES
from yi_agent.agent.prompt import GUIDES
from yi_agent.agent.runner import Condition, ConditionChanged, compare, run_split
from yi_agent.agent.tools import TOOLSETS
from yi_agent.config import ConfigError, Settings, load_settings
from yi_agent.data import openrca
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
from yi_agent.eval import bank, baselines
from yi_agent.eval import run as eval_run
from yi_agent.eval.grade import RunInvalid
from yi_agent.eval.harness import HarnessNotApproved
from yi_agent.llm.anthropic_provider import AnthropicProvider
from yi_agent.llm.types import LLMProvider

SPLITS = ("dev", "dev_small", "test", "all")
# Development runs on Sonnet 5.5 (half Opus 5.5's price on every rate); the model
# itself is measured as an S4 A/B, not assumed (docs/ROADMAP.md, S3 decisions).
DEFAULT_MODEL = "claude-sonnet-5-5"


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
    total_docs, total_s, failed, reused = 0, 0.0, [], 0
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
        if report.reused:
            reused += 1
            print(f"{i}: {report.docs:>8} docs already loaded (complete, loader v{LOADER_VERSION})")
            continue
        total_docs += report.docs  # only what this run loaded: a reuse is not a load rate
        total_s += report.seconds
        units = {k: f"{s.time_column}:{s.time_unit}" for k, s in report.stats.items()}
        skipped = sum(s.skipped_nonfinite + s.skipped_no_time for s in report.stats.values())
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
        print(f"loaded {total_docs} docs in {total_s:.1f}s ({total_docs / total_s:,.0f}/s)")
    if reused:
        print(f"{reused} of {len(ids)} cases were already loaded and complete")
    if failed:
        print(f"{len(failed)} cases failed to load: {failed}")
    return 1 if failed else 0


def cmd_eval_bank_baseline(settings: Settings, args: argparse.Namespace) -> int:
    path, table = bank.run_baseline(
        args.name, args.split, settings.data_dir, args.approve_harness, seed=args.seed
    )
    print(f"OpenRCA Bank, {args.split}, baseline {args.name}")
    print(table)
    print(f"trials: {path}")
    return 0


def cmd_eval_bank_score(settings: Settings, args: argparse.Namespace) -> int:
    print(bank.score_run(args.trials, args.split, settings.data_dir, args.approve_harness))
    return 0


def cmd_eval_bank_replay(settings: Settings, args: argparse.Namespace) -> int:
    same, n, mismatches, tables = bank.replay_archive(settings.data_dir, args.approve_harness)
    print("Microsoft's archived RCA-agent run on Bank, re-graded with OpenRCA's evaluate()")
    print(f"recorded scores reproduced: {same}/{n}")
    for m in mismatches[:20]:
        print("MISMATCH", m)
    for split, table in tables.items():
        print(f"\n{split}:")
        print(table)
    return 0 if same == n else 1


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


def cmd_eval_fault_signals(settings: Settings, args: argparse.Namespace) -> int:
    from yi_agent.analysis import fault_signals

    print(fault_signals(args.split, settings.data_dir, args.variant, progress=_progress))
    return 0


def cmd_eval_confusion(settings: Settings, args: argparse.Namespace) -> int:
    from yi_agent.analysis import confusion

    print(confusion(args.split, args.variants))
    return 0


def cmd_eval_agreement(settings: Settings, args: argparse.Namespace) -> int:
    from yi_agent.analysis import agreement

    print(agreement(args.split, args.variants))
    return 0


def cmd_eval_metric_kinds(settings: Settings, args: argparse.Namespace) -> int:
    print(eval_run.metric_kinds(settings.data_dir, args.split))
    return 0


def cmd_agent_run(settings: Settings, args: argparse.Namespace) -> int:
    try:
        cond = Condition(
            model=args.model,
            effort=args.effort,
            max_steps=args.max_steps,
            placebo=args.placebo,
            prompt=args.prompt,
            tools=args.tools,
            mode=args.mode,
        )
    except ValueError as e:
        print(f"bad condition: {e}", file=sys.stderr)
        return 2
    key = settings.anthropic_api_key

    def provider() -> LLMProvider:
        if cond.model in POLICIES:  # a scripted control: no API call
            return POLICIES[cond.model]()
        return AnthropicProvider(
            model=cond.model,
            max_tokens=32_000,
            effort=cond.effort,
            api_key=key.get_secret_value() if key else None,
        )

    summary = asyncio.run(
        run_split(
            args.split,
            settings.data_dir,
            _es(settings),
            provider,
            cond,
            args.variant,
            limit=args.limit,
            parallel=args.parallel,
            progress=_progress,
            unload_after=args.unload_after,
        )
    )
    print(summary)
    return 0


def cmd_agent_bank_run(settings: Settings, args: argparse.Namespace) -> int:
    from yi_agent.agent.bank import BankCondition, run_bank_split

    cond = BankCondition(
        model=args.model,
        effort=args.effort,
        max_steps=args.max_steps,
        prompt=args.prompt,
        placebo=args.placebo,
    )
    key = settings.anthropic_api_key

    def provider() -> LLMProvider:
        return AnthropicProvider(
            model=cond.model,
            max_tokens=32_000,
            effort=cond.effort,
            api_key=key.get_secret_value() if key else None,
        )

    print(
        asyncio.run(
            run_bank_split(
                args.split,
                settings.data_dir,
                _es(settings),
                provider,
                cond,
                args.variant,
                limit=args.limit,
                parallel=args.parallel,
                progress=_progress,
                unload_after=args.unload_after,
            )
        )
    )
    return 0


def cmd_agent_compare(settings: Settings, args: argparse.Namespace) -> int:
    for group in args.group:
        print(compare(args.split, args.variants, group))
    return 0


def _bank_ids(args: argparse.Namespace) -> list[str]:
    split = openrca.read_split()
    known = set(split.dev) | set(split.test)
    if args.ids:
        if unknown := [i for i in args.ids if i not in known]:
            raise SystemExit(f"unknown Bank query ids: {unknown}")
        return list(args.ids)
    if args.split == "all":
        return sorted(known)
    ids: list[str] = getattr(split, args.split)
    return ids


def _bank_verified(settings: Settings) -> list[openrca.BankQuery] | None:
    """The labels, written next to the download, if it checks clean; else None."""
    problems, labels = openrca.check_raw(openrca.raw_dir(settings.data_dir))
    for p in problems:
        print("PROBLEM", p)
    if labels is not None:
        openrca.labels_path(settings.data_dir).write_text(openrca.labels_json(labels))
    return labels


def cmd_bank_fetch(settings: Settings, args: argparse.Namespace) -> int:
    root = openrca.raw_dir(settings.data_dir)
    print(f"downloading OpenRCA Bank from {openrca.HF_REPO} into {root} (26.4 GB, resumable)")
    openrca.fetch(settings.data_dir)
    print(f"fetching Microsoft's archived Bank run (the bar) from {bank.ARCHIVE_URL}")
    bank.fetch_archive(settings.data_dir)
    print("done; next: yi data bank check")
    return 0


def cmd_bank_check(settings: Settings, args: argparse.Namespace) -> int:
    labels = _bank_verified(settings)
    n = len(openrca.TELEMETRY_SIZES) + 2
    if labels is None:
        print(f"{n} files checked: problems found")
        return 1
    by = {q.id: q for q in labels}
    split = openrca.read_split()
    for part in ("dev", "test"):
        ids: list[str] = getattr(split, part)
        two = sum(len(by[i].failures) > 1 for i in ids)
        reasons = Counter(f.reason for i in ids for f in by[i].failures)
        print(f"  {part:5} {len(ids):>3} queries ({two} with two failures), {len(reasons)} reasons")
    print(f"{len(labels)} queries; {n} files checked, 0 problems; labels and split as pinned")
    print(f"labels written to {openrca.labels_path(settings.data_dir)}")
    return 0


def cmd_bank_cut(settings: Settings, args: argparse.Namespace) -> int:
    queries = _bank_verified(settings)
    if queries is None:
        print("not cutting: run `yi data bank check` and fix the download first")
        return 1
    out = openrca.cases_dir(settings.data_dir)
    print(f"cutting {len(queries)} query windows into {out}", flush=True)
    report = openrca.cut(openrca.raw_dir(settings.data_dir), out, queries, _progress)
    tot = report.totals()
    size = sum(f.stat().st_size for f in out.rglob("*.parquet")) / 1e9
    print(
        f"cut {report.cases} cases in {report.seconds / 60:.1f} min, {size:.2f} GB: "
        f"{tot.get('logs', 0):,} log lines, {tot.get('traces', 0):,} spans, "
        f"{tot.get('metrics', 0):,} metric rows"
    )
    print(f"malformed lines skipped: {sum(report.bad_lines.values())} {report.bad_lines}")
    print(f"metric cells recorded twice (mean kept): {report.duplicate_metric_cells}")
    units = Counter(f"{rel.split('/', 1)[1]}: {u}" for rel, u in report.units.items())
    print("timestamp units (files): " + ", ".join(f"{k} x{n}" for k, n in sorted(units.items())))
    for kind, ids in sorted(report.empty().items()):
        print(f"WARNING {len(ids)} cases have no {kind}: {ids[:8]}")
    return 0


def cmd_bank_inspect(settings: Settings, args: argparse.Namespace) -> int:
    cases = openrca.cases_dir(settings.data_dir)
    print(openrca.coverage(cases))
    ids = args.ids or openrca.read_split().dev[:2]
    for i in ids:
        d = cases / i
        w = openrca.read_case_window(d)
        print(f"=== {i}")
        print(
            f"window {openrca.local_str(w['start_ms'])} to {openrca.local_str(w['end_ms'])} "
            f"(UTC+8), baseline from {openrca.local_str(w['baseline_start_ms'])}"
        )
        # inspect_case's first line is RCAEval's window (inject_time.txt): not Bank's
        print(inspect_case(d).split("\n", 1)[1])
        pairs, ms, us = openrca.span_containment(d)
        print(
            f"  span durations: of {pairs} same-host parent/child pairs, {ms:.0%} nest if "
            f"durations are ms (the cut's assumption), {us:.0%} if they are us"
        )
    return 0


def cmd_bank_audit(settings: Settings, args: argparse.Namespace) -> int:
    result = AuditResult()
    ids = _bank_ids(args)
    for n, i in enumerate(ids, 1):
        audit_case(openrca.cases_dir(settings.data_dir) / i, result)
        if n % 20 == 0:
            _progress(f"  {n}/{len(ids)}")
    print(result.render())
    return 0


def cmd_bank_load(settings: Settings, args: argparse.Namespace) -> int:
    es = _es(settings)
    ids = _bank_ids(args)
    loader = ensure_loaded if args.keep else load_case
    total_docs, total_s, reused, failed = 0, 0.0, 0, []
    for n, i in enumerate(ids, 1):
        _progress(f"[{n}/{len(ids)}] {i} ...")
        try:
            report = loader(es, i, openrca.cases_dir(settings.data_dir) / i, _progress)
        except (LoadError, TelemetryError) as e:
            print(f"{i}: FAILED {e}")
            failed.append(i)
            continue
        if report.reused:
            reused += 1
            print(f"{i}: {report.docs:>8} docs already loaded (complete, loader v{LOADER_VERSION})")
            continue
        total_docs += report.docs
        total_s += report.seconds
        print(f"{i}: {report.docs:>8} docs in {report.seconds:5.1f}s {report.indices}", flush=True)
        if not args.keep:
            unload_case(es, i)
    if total_s:
        print(f"loaded {total_docs} docs in {total_s:.1f}s ({total_docs / total_s:,.0f}/s)")
    if reused:
        print(f"{reused} of {len(ids)} cases were already loaded and complete")
    if failed:
        print(f"{len(failed)} cases failed to load: {failed}")
    return 1 if failed else 0


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
    bank_cmds = data.add_parser("bank", help="OpenRCA Bank (S5)").add_subparsers(
        dest="bank_cmd", required=True
    )
    bank_cmds.add_parser("fetch", help="download Bank (26.4 GB) from Hugging Face").set_defaults(
        fn=cmd_bank_fetch
    )
    bank_cmds.add_parser(
        "check", help="verify the download against the frozen labels"
    ).set_defaults(fn=cmd_bank_check)
    bank_cmds.add_parser("cut", help="cut each query's window into a case directory").set_defaults(
        fn=cmd_bank_cut
    )
    c = bank_cmds.add_parser("inspect", help="schemas, time units, span durations of cut cases")
    c.add_argument("ids", nargs="*", help="query ids (default: two dev queries)")
    c.set_defaults(fn=cmd_bank_inspect)
    for name, fn, help_ in (
        ("audit", cmd_bank_audit, "scan cut cases for anything that could leak the answer"),
        ("load", cmd_bank_load, "load cut cases into Elasticsearch, verify counts, then unload"),
    ):
        c = bank_cmds.add_parser(name, help=help_)
        c.add_argument("ids", nargs="*", help="query ids (default: the whole --split)")
        c.add_argument("--split", choices=("dev", "test", "all"), default="dev")
        if name == "load":
            c.add_argument("--keep", action="store_true", help="keep the indices; reuse a load")
        c.set_defaults(fn=fn)

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
    c = ev.add_parser("bank-baseline", help="OpenRCA Bank: run a baseline and score it")
    c.add_argument("name", choices=sorted(bank.BASELINES))
    c.add_argument("--split", choices=bank.SPLITS, default="dev")
    c.add_argument("--seed", type=int, default=0)
    c.add_argument("--approve-harness", action="store_true", help=approve_help)
    c.set_defaults(fn=cmd_eval_bank_baseline)
    c = ev.add_parser("bank-score", help="OpenRCA Bank: grade a trials.jsonl over a split")
    c.add_argument("trials", type=Path)
    c.add_argument("--split", choices=bank.SPLITS, default="dev")
    c.add_argument("--approve-harness", action="store_true", help=approve_help)
    c.set_defaults(fn=cmd_eval_bank_score)
    c = ev.add_parser("bank-replay", help="re-grade Microsoft's archived Bank run (the bar)")
    c.add_argument("--approve-harness", action="store_true", help=approve_help)
    c.set_defaults(fn=cmd_eval_bank_replay)
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
    c = ev.add_parser(
        "fault-signals", help="what each fault did to its root cause (free; reads labels)"
    )
    c.add_argument("--split", choices=eval_run.SPLITS, default="dev")
    c.add_argument("--variant", help="also show what this run's agent said the fault was")
    c.set_defaults(fn=cmd_eval_fault_signals)
    c = ev.add_parser("confusion", help="true vs named fault per variant (free; reads labels)")
    c.add_argument("variants", nargs="+")
    c.add_argument("--split", choices=eval_run.SPLITS, default="dev")
    c.set_defaults(fn=cmd_eval_confusion)
    c = ev.add_parser(
        "agreement", help="repeated runs of one condition, case by case (free; reads labels)"
    )
    c.add_argument("variants", nargs="+")
    c.add_argument("--split", choices=eval_run.SPLITS, default="dev")
    c.set_defaults(fn=cmd_eval_agreement)
    c = ev.add_parser("metric-kinds", help="metric families each system exports")
    c.add_argument("--split", choices=eval_run.SPLITS, default="dev_small")
    c.set_defaults(fn=cmd_eval_metric_kinds)

    ag = sub.add_parser("agent", help="the agent (costs money)").add_subparsers(
        dest="cmd", required=True
    )
    c = ag.add_parser("run", help="run the agent on a split (resumable), then score it")
    c.add_argument("--split", choices=eval_run.SPLITS, default="dev_small")
    c.add_argument("--limit", type=int, help="only the first N cases (a smoke test; not scored)")
    c.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"a model id, or a scripted control: {', '.join(POLICIES)} (free)",
    )
    c.add_argument("--effort", default="high", choices=("low", "medium", "high", "xhigh", "max"))
    c.add_argument("--max-steps", type=int, default=25, help="model calls per case")
    c.add_argument("--variant", default="agent", help="results directory: one condition each")
    c.add_argument("--parallel", type=int, default=3, help="cases investigated at once")
    c.add_argument(
        "--placebo",
        action="store_true",
        help="control: a false onset 6 min early, nothing visible from the real one on",
    )
    c.add_argument(
        "--unload-after",
        action="store_true",
        help="delete each case's indices after its trial (keeps few cases loaded)",
    )
    c.add_argument("--prompt", choices=sorted(GUIDES), default="v1", help="prompt version")
    c.add_argument(
        "--mode",
        choices=("agent", "one-call"),
        default="agent",
        help="one-call: the baseline with one model call on a fixed summary, no tools",
    )
    c.add_argument("--tools", choices=sorted(TOOLSETS), default="v1", help="tool version")
    c.set_defaults(fn=cmd_agent_run)
    c = ag.add_parser("bank-run", help="the agent on OpenRCA Bank queries (resumable; costs money)")
    c.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    c.add_argument("--variant", required=True, help="results directory name (one condition)")
    c.add_argument("--limit", type=int, help="only the first N queries (a smoke test)")
    c.add_argument("--model", default=DEFAULT_MODEL)
    c.add_argument("--effort", default="high")
    c.add_argument("--max-steps", type=int, default=25)
    c.add_argument("--prompt", default="bank-v1", help="Bank prompt version")
    c.add_argument("--parallel", type=int, default=3)
    c.add_argument("--unload-after", action="store_true", help="unload each query after its trial")
    c.add_argument(
        "--placebo",
        action="store_true",
        help="control: show only the baseline, its last 15 minutes as the window",
    )
    c.set_defaults(fn=cmd_agent_bank_run)
    c = ag.add_parser("compare", help="variants side by side, from their scores (free)")
    c.add_argument("variants", nargs="+", help="results directories, e.g. baro agent")
    c.add_argument("--split", choices=eval_run.SPLITS, default="dev_small")
    c.add_argument("--group", nargs="+", default=["all"], help="e.g. all re2-tt 're2 delay'")
    c.set_defaults(fn=cmd_agent_compare)
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
    except (RunInvalid, bank.RunInvalid) as e:
        print(f"run is invalid, not scored: {e}", file=sys.stderr)
        return 2
    except ConditionChanged as e:
        print(str(e), file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"missing input: {e}", file=sys.stderr)
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
