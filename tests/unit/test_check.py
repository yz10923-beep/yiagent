from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tests.rcaeval_fixture import ONSET_S, make_case, make_index
from yi_agent.data.check import AuditResult, audit_case, check_download
from yi_agent.data.rcaeval import labels_from_names

TT = "re2tt_ts-order-service_cpu_1"
SS = "re2ss_carts_cpu_1"


def index_row(name: str, service: str, fault: str, traces: bool = True) -> dict[str, object]:
    return {
        "case": name,
        "root_cause_service": service,
        "fault": fault,
        "inject_time": ONSET_S,
        "has_logs": True,
        "has_traces": traces,
    }


def test_a_download_that_agrees_has_no_problems(tmp_path: Path) -> None:
    make_case(tmp_path, TT)
    make_case(tmp_path, SS, traces=False)
    make_index(
        tmp_path, [index_row(TT, "ts-order-service", "cpu"), index_row(SS, "carts", "cpu", False)]
    )
    assert check_download(tmp_path, labels_from_names([TT, SS])) == []


def test_disagreements_are_each_reported(tmp_path: Path) -> None:
    make_case(tmp_path, TT)
    make_index(
        tmp_path,
        [
            index_row(TT, "ts-order-service", "mem"),
            index_row("re2ob_cartservice_cpu_1", "x", "cpu"),
        ],
    )
    problems = check_download(tmp_path, labels_from_names([TT, SS]))
    text = "\n".join(problems)
    assert "index says ts-order-service/mem" in text  # the name is not the label
    assert "not in cases.parquet" in text  # SS labelled but not indexed
    assert "not labelled" in text  # an index row we have no label for


def test_audit_finds_injector_traces_and_case_names(tmp_path: Path) -> None:
    d = make_case(tmp_path, TT)
    pq.write_table(
        pa.table(
            {
                "timestamp": [ONSET_S * 10**9] * 3,
                "container_name": ["chaos-daemon", "frontend", "frontend"],
                "message": ["stress-ng: dispatching hogs", "ok", f"loaded from /data/{TT}"],
            }
        ),
        d / "logs.parquet",
    )
    result = AuditResult()
    audit_case(d, result)
    out = result.render()
    assert "chaos" in out and "logs.container_name" in out
    assert "stress tool" in out and "logs.message" in out
    assert "case name" in out


def test_clean_case_has_no_audit_hits(tmp_path: Path) -> None:
    result = AuditResult()
    audit_case(make_case(tmp_path, TT), result)
    assert "no hits" in result.render()


def test_audit_ignores_the_false_positives_of_the_first_real_audit(tmp_path: Path) -> None:
    d = make_case(tmp_path, TT)
    lines = [
        "at com.mongodb.connection.DefaultConnectionPool$PooledConnection.receiveMessage",
        "JSR-330 'javax.inject.Inject' annotation found and supported for autowiring",
        "TokenServiceImpl : eyJhbGciOiJIUzI1NiJ9.eyJ0YyI6InRjIiwiZGVsYXkiOjF9.sig tc delay",
        "fault injected into the payment path",  # a real hit
    ]
    pq.write_table(
        pa.table(
            {
                "timestamp": [ONSET_S] * len(lines),
                "container_name": ["ts-auth-service"] * len(lines),
                "message": lines,
            }
        ),
        d / "logs.parquet",
    )
    result = AuditResult()
    audit_case(d, result)
    assert result.hits["fault"]["logs.message"] == [1, 1]
    assert result.hits["inject"]["logs.message"] == [1, 1]
    assert "network injector" not in result.hits
