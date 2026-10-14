"""OpenRCA Bank (S5 step 1): the frozen labels and split, window parsing, the download
check, and the cut, on the real labels and on a synthetic download in the real layout."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests.openrca_fixture import BIG_ID, BankFixture, make_bank
from yi_agent.data import openrca
from yi_agent.data.openrca import BankQuery, local_ms, local_str, parse_window
from yi_agent.data.telemetry import FileStats, TelemetryError, iter_docs, pick_time_column

SPLIT_SHA256 = "7a08c564ebaa154c30ba58da1b14ef28b3cba585b62de46e3966aa6b7c544bf9"


# ---- the frozen split (the labels are built on the VM; `yi data bank check` verifies
# their pinned hash and that this split is reproduced from them) -------------------------


def test_the_split_is_the_frozen_one() -> None:
    assert hashlib.sha256(openrca.SPLIT_PATH.read_bytes()).hexdigest() == SPLIT_SHA256
    s = openrca.read_split()
    assert (len(s.dev), len(s.test)) == (34, 102) and set(s.dev).isdisjoint(s.test)
    assert all(len(i) == 11 and i[0] == "b" for i in s.dev + s.test)
    assert set(s.dev) | set(s.test) == {openrca.query_id(row) for row in range(136)}


def test_no_bank_data_is_in_the_repo() -> None:
    """Bank's data is for private evaluation (S5 decisions): labels, scoring points and
    records never enter the repo, only hashes and opaque ids."""
    root = Path(__file__).resolve().parents[2]
    tracked = [p for d in ("evals", "src", "tests", "docs") for p in (root / d).rglob("*")]
    for p in tracked:
        if p.is_file() and p.suffix in (".json", ".csv", ".jsonl", ".parquet"):
            text = p.read_text(errors="ignore")
            assert "predicted root cause" not in text, p
            assert "Mysql02" not in text or p.suffix != ".json", p


def test_ids_are_opaque_and_stable() -> None:
    assert openrca.query_id(0) == openrca.query_id(0) != openrca.query_id(1)
    assert all(c in "0123456789abcdef" for c in openrca.query_id(7)[1:])


# ---- windows and scoring points --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("On March 4, 2021, within the time range of 14:30 to 15:00, a", "2021-03-04 14:30", None),
        ("On March 4, 2021, between 18:00 and 18:30, there", "2021-03-04 18:00", None),
        ("of March 12, 2021, from 23:30 to 00:00, there", "2021-03-12 23:30", "2021-03-13 00:00"),
        (
            "of March 6, 2021, from 23:30 to March 7, 2021, at 00:00, there",
            "2021-03-06 23:30",
            "2021-03-07 00:00",
        ),
    ],
)
def test_windows_are_parsed_in_utc_plus_8(text: str, start: str, end: str | None) -> None:
    s, e = parse_window(text)
    assert local_str(s) == start + ":00"
    assert e - s == openrca.WINDOW_MS
    if end:
        assert local_str(e) == end + ":00"
    assert local_ms("2021-03-04 14:57:00") == 1614841020 * 1000  # record.csv's own pair


def test_a_window_that_is_not_30_minutes_is_refused() -> None:
    with pytest.raises(ValueError, match="not 30"):
        parse_window("On March 4, 2021, from 14:30 to 15:30, a failure")


def test_labels_refuse_scoring_points_that_disagree_with_the_records(tmp_path: Path) -> None:
    fx = make_bank(tmp_path)
    q = (fx.root / "Bank" / "query.csv").read_text()
    r = (fx.root / "Bank" / "record.csv").read_text()
    assert len(openrca.labels_from_csv(q, r)) == 6
    with pytest.raises(ValueError, match="disagree"):
        openrca.labels_from_csv(q.replace("component is Tomcat01", "component is Tomcat02"), r)
    with pytest.raises(ValueError, match="records in window"):
        openrca.labels_from_csv(q, r.replace("2021-03-04 10:12:00", "x").replace("1614823920", "1"))


def test_two_failures_out_of_time_order_pair_by_position(tmp_path: Path) -> None:
    fx = make_bank(tmp_path)
    qs = _labels(fx)
    two = next(q for q in qs if len(q.failures) == 2)
    assert two.asked == ("time", "component")
    assert [f.component for f in two.failures] == ["apache01", "IG02"]  # time order
    assert "1-th predicted root cause component is IG02" in two.scoring_points  # verbatim


# ---- the download check ----------------------------------------------------------------


def _labels(fx: BankFixture) -> list[BankQuery]:
    bank = fx.root / "Bank"
    return openrca.labels_from_csv(
        (bank / "query.csv").read_text(), (bank / "record.csv").read_text()
    )


def test_check_accepts_the_download_it_was_frozen_from(tmp_path: Path) -> None:
    fx = make_bank(tmp_path)
    problems, labels = openrca.check_raw(fx.root, **fx.pins())  # type: ignore[arg-type]
    assert problems == [] and labels == _labels(fx)


def test_check_reports_a_truncated_file_a_missing_one_and_other_labels(tmp_path: Path) -> None:
    fx = make_bank(tmp_path)
    day = fx.root / "Bank" / "telemetry" / "2021_03_04"
    with (day / "trace" / "trace_span.csv").open("a") as f:
        f.write("1,2\n")
    (day / "log" / "log_service.csv").unlink()
    problems, labels = openrca.check_raw(fx.root, **fx.pins())  # type: ignore[arg-type]
    assert labels is None
    assert any("trace_span.csv" in p and "expected" in p for p in problems)
    assert any("log_service.csv missing" in p for p in problems)
    fx2 = make_bank(tmp_path / "again")
    pins = {**fx2.pins(), "labels_sha": "0" * 64}
    assert openrca.check_raw(fx2.root, **pins) == (  # type: ignore[arg-type]
        ["labels built from the download differ from the pinned hash"],
        None,
    )
    pins = {**fx2.pins(), "split_text": "{}"}
    problems, _ = openrca.check_raw(fx2.root, **pins)  # type: ignore[arg-type]
    assert problems == ["the split rebuilt from the labels differs from evals/split_bank.json"]
    pins = {**fx2.pins(), "query_sha": "0" * 64}
    assert openrca.check_raw(fx2.root, **pins)[0] == [
        "query.csv: sha256 differs from the pinned one"
    ]  # type: ignore[arg-type]


# ---- the cut ---------------------------------------------------------------------------


@pytest.fixture
def cut(tmp_path: Path) -> tuple[Path, list[BankQuery], openrca.CutReport]:
    fx = make_bank(tmp_path / "openrca")
    qs = _labels(fx)
    out = tmp_path / "cases"
    return out, qs, openrca.cut(fx.root, out, qs)


def test_each_case_holds_its_window_and_the_baseline_before_it(
    cut: tuple[Path, list[BankQuery], openrca.CutReport],
) -> None:
    out, qs, report = cut
    for q in qs:
        d = out / q.id
        assert sorted(p.name for p in d.iterdir()) == [
            "logs.parquet",
            "metrics.parquet",
            "traces.parquet",
            "window.json",
        ]
        w = json.loads((d / "window.json").read_text())
        assert set(w) == {"cut_version", "start_ms", "end_ms", "baseline_start_ms"}
        assert w["baseline_start_ms"] == q.start_ms - openrca.BASELINE_MS
        logs = pq.read_table(d / "logs.parquet")["timestamp"].to_pylist()
        spans = pq.read_table(d / "traces.parquet")["startTime"].to_pylist()
        times = pq.read_table(d / "metrics.parquet")["time"].to_pylist()
        lo, hi = w["baseline_start_ms"], q.end_ms
        # one row a minute: the baseline's first minute is in, the window's end is not
        assert sorted(t * 1000 for t in logs) == list(range(lo, hi, 60_000))
        assert sorted(t * 1000 for t in times) == list(range(lo, hi, 60_000))
        assert len(spans) == 120 and all(lo <= t < hi for t in spans)
        assert report.rows[q.id] == {"logs": 60, "traces": 120, "metrics": 60}


def test_a_window_after_midnight_takes_its_baseline_from_the_previous_day(
    cut: tuple[Path, list[BankQuery], openrca.CutReport],
) -> None:
    out, qs, _ = cut
    q = next(q for q in qs if local_str(q.start_ms) == "2021-03-05 00:00:00")
    logs = pq.read_table(out / q.id / "logs.parquet")["timestamp"].to_pylist()
    assert local_str(min(logs) * 1000) == "2021-03-04 23:30:00"  # from 2021_03_04's file


def test_the_cut_keeps_rcaeval_column_names_ids_and_text_intact(
    cut: tuple[Path, list[BankQuery], openrca.CutReport],
) -> None:
    out, qs, report = cut
    d = out / qs[0].id
    spans = pq.read_table(d / "traces.parquet").to_pylist()
    parent = next(s for s in spans if s["parentSpanID"].startswith("root-"))
    assert int(parent["spanID"]) > 2**63 and parent["spanID"].startswith(BIG_ID[:10])
    assert parent["duration"] == 67_000.0  # 67 ms, stored in us
    assert parent["serviceName"] == "dockerB2"
    log = pq.read_table(d / "logs.parquet").to_pylist()[0]
    assert log["container_name"] == "Tomcat01" and log["log_name"] == "gc"
    assert "\n  [Times: real=0.08]" in log["message"]
    metrics = pq.read_table(d / "metrics.parquet").schema.names
    assert "Tomcat01_OSLinux-CPU_CPU_CPUCpuUtil" in metrics and "ServiceTest1_mrt" in metrics
    assert report.bad_lines == {
        "2021_03_04/log/log_service.csv": 1,
        "2021_03_05/log/log_service.csv": 1,
    }
    # 2021-03-05 00:00 is recorded twice (20.5 and 30.5) and only one window holds it
    assert report.duplicate_metric_cells == 1
    assert report.units["2021_03_04/trace/trace_span.csv"] == "ms"
    assert report.units["2021_03_05/trace/trace_span.csv"] == "s"  # still cut, in ms
    assert report.units["2021_03_05/log/log_service.csv"] == "s"
    assert report.empty() == {}
    q = next(q for q in qs if local_str(q.start_ms) == "2021-03-05 00:00:00")
    row = pq.read_table(out / q.id / "metrics.parquet").to_pylist()
    first = next(r for r in row if r["time"] * 1000 == q.start_ms)
    assert first["Tomcat01_OSLinux-CPU_CPU_CPUCpuUtil"] == 25.5


def test_the_loader_reads_a_cut_case_with_the_right_time_units(
    cut: tuple[Path, list[BankQuery], openrca.CutReport],
) -> None:
    out, qs, _ = cut
    d, q = out / qs[0].id, qs[0]
    for kind, col in (("logs", "timestamp"), ("traces", "startTime"), ("metrics", "time")):
        path = d / f"{kind}.parquet"
        assert pick_time_column(kind, pq.ParquetFile(path)) == col
        stats = FileStats()
        docs = list(iter_docs(kind, path, stats))
        assert docs and stats.skipped_no_time == 0
        assert all(q.start_ms - openrca.BASELINE_MS <= x["@timestamp"] < q.end_ms for x in docs)


def test_no_label_reaches_a_case_directory(
    cut: tuple[Path, list[BankQuery], openrca.CutReport],
) -> None:
    """Telemetry names components (that is the evidence); nothing in a case may carry a
    reason, a scoring point, a task or a record row."""
    out, qs, _ = cut
    forbidden = [*openrca.REASONS, "scoring", "task_", "root cause", "query", "record"]
    for d in out.iterdir():
        for p in d.iterdir():
            if p.suffix == ".json":
                texts = [p.read_text()]
            else:
                t = pq.read_table(p)
                texts = list(t.schema.names) + [
                    str(v) for c in t.columns if pa.types.is_string(c.type) for v in c.to_pylist()
                ]
            for text in texts:
                assert not any(f.lower() in text.lower() for f in forbidden), (p, text[:80])


def test_an_interrupted_cut_leaves_the_last_complete_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = make_bank(tmp_path / "openrca")
    qs = _labels(fx)
    out = tmp_path / "cases"
    openrca.cut(fx.root, out, qs)
    before = sorted(p.name for p in out.iterdir())

    def boom(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(openrca, "_write", boom)
    with pytest.raises(OSError, match="disk full"):
        openrca.cut(fx.root, out, qs)
    assert sorted(p.name for p in out.iterdir()) == before
    monkeypatch.undo()
    openrca.cut(fx.root, out, qs)  # a rerun replaces the partial directory
    assert not (tmp_path / "cases.partial").exists()


def test_a_file_in_two_time_units_stops_the_cut(tmp_path: Path) -> None:
    fx = make_bank(tmp_path / "openrca")
    path = fx.root / "Bank" / "telemetry" / "2021_03_04" / "trace" / "trace_span.csv"
    with path.open("a") as f:
        f.write("1614823920,dockerB2,a,b,c,1\n")  # seconds, in a file of ms
    with pytest.raises(TelemetryError, match="more than one unit"):
        openrca.cut(fx.root, tmp_path / "cases", _labels(fx))


def test_span_containment_tells_ms_durations_from_us(
    cut: tuple[Path, list[BankQuery], openrca.CutReport],
) -> None:
    out, qs, _ = cut
    assert openrca.span_containment(out / qs[0].id) == (60, 1.0, 0.0)


# ---- the commands Yi runs, through `yi` ------------------------------------------------


def _use_fixture(fx: BankFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point `yi data bank ...` at the fixture's pins, as the real ones point at Bank."""
    split = tmp_path / "split_bank.json"
    split.write_text(fx.split_text)
    monkeypatch.setattr(openrca, "SPLIT_PATH", split)
    monkeypatch.setattr(openrca, "TELEMETRY_SIZES", fx.sizes)
    monkeypatch.setattr(openrca, "QUERY_SHA256", fx.query_sha)
    monkeypatch.setattr(openrca, "RECORD_SHA256", fx.record_sha)
    monkeypatch.setattr(openrca, "LABELS_SHA256", fx.labels_sha)


def test_the_bank_commands_run_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil
    import sys
    import types

    from yi_agent import cli

    source = make_bank(tmp_path / "hub")
    data_dir = tmp_path / "data"
    monkeypatch.setenv("YI_DATA_DIR", str(data_dir))
    _use_fixture(source, monkeypatch, tmp_path)

    def snapshot_download(**kw: object) -> None:  # what the hub would put there
        assert kw["repo_id"] == openrca.HF_REPO
        shutil.copytree(source.root / "Bank", Path(str(kw["local_dir"])) / "Bank")

    hub = types.ModuleType("huggingface_hub")
    hub.snapshot_download = snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

    assert cli.main(["data", "bank", "check"]) == 1  # nothing downloaded yet
    assert "missing" in capsys.readouterr().out
    assert cli.main(["data", "bank", "cut"]) == 1  # refuses before a clean check
    assert "not cutting" in capsys.readouterr().out
    assert cli.main(["data", "bank", "fetch"]) == 0
    assert cli.main(["data", "bank", "check"]) == 0
    out = capsys.readouterr().out
    assert "0 problems; labels and split as pinned" in out
    assert openrca.labels_path(data_dir).is_file()  # next to the download, not in the repo
    assert cli.main(["data", "bank", "cut"]) == 0
    out = capsys.readouterr().out
    assert "cut 6 cases" in out and "malformed lines skipped: 2" in out
    assert "trace/trace_span.csv: ms x1" in out and "trace/trace_span.csv: s x1" in out
    assert "WARNING" not in out
    assert cli.main(["data", "bank", "inspect"]) == 0
    out = capsys.readouterr().out
    assert "100% nest if durations are ms" in out and "time column: startTime, unit ms" in out
    assert "cases with none: 0" in out and "(UTC+8), baseline from 2021-03-0" in out
    assert "inject_time" not in out
    assert cli.main(["data", "bank", "audit", "--split", "all"]) == 0
    assert "audited 6 cases" in capsys.readouterr().out
