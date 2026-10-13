"""A tiny synthetic OpenRCA Bank download in the real layout, for tests without it.

Two days of one-minute telemetry in the real CSV schemas (OpenRCA's Bank prompt), and
queries that exercise what the real ones do: a window ending at midnight with no second
date, the same window asked twice, a window whose baseline lies in the previous day's
files, and a window with two failures whose scoring points are not in time order. The
files carry the hazards the cut must handle: a span id past int64, a quoted log line
with a comma and a newline, a malformed line, and a metric cell recorded twice."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from yi_agent.data import openrca
from yi_agent.data.openrca import local_ms

DAYS = ("2021_03_04", "2021_03_05")
BIG_ID = "21030400016232062871"  # > 2**63: must stay a string

QUERIES = [
    (
        "task_6",
        "On March 4, 2021, between 10:00 and 10:30, there was a single failure observed in "
        "the system. Your task is to identify the root cause component and the root cause "
        "reason for this failure.",
        "The only predicted root cause component is Tomcat01\n"
        "The only predicted root cause reason is high CPU usage\n",
    ),
    (
        "task_1",
        "During the specified time range of March 4, 2021, from 23:30 to 00:00, the system "
        "experienced one failure. Please pinpoint the root cause occurrence datetime.",
        "The only root cause occurrence time is within 1 minutes (i.e., <=1min) of "
        "2021-03-04 23:41:00\n",
    ),
    (
        "task_7",
        "During the specified time range of March 4, 2021, from 23:30 to March 5, 2021, at "
        "00:00, there was one failure observed. Identify the time, component and reason.",
        "The only root cause occurrence time is within 1 minutes (i.e., <=1min) of "
        "2021-03-04 23:41:00\n"
        "The only predicted root cause component is MG01\n"
        "The only predicted root cause reason is network latency\n",
    ),
    (
        "task_2",
        "On March 5, 2021, within the time range of 00:00 to 00:30, a single failure was "
        "detected in the system. Please identify the root cause reason.",
        "The only predicted root cause reason is high memory usage\n",
    ),
    (
        "task_5",
        "Within the time range of March 5, 2021, from 10:00 to 10:30, there were two "
        "failures. Please identify the occurrence datetimes and the components.",
        "The 1-th root cause occurrence time is within 1 minutes (i.e., <=1min) of "
        "2021-03-05 10:20:00\n"
        "The 2-th root cause occurrence time is within 1 minutes (i.e., <=1min) of "
        "2021-03-05 10:08:00\n"
        "The 1-th predicted root cause component is IG02\n"
        "The 2-th predicted root cause component is apache01\n",
    ),
    (
        "task_3",
        "On March 5, 2021, between 15:00 and 15:30, there was one failure. Please identify "
        "the root cause component.",
        "The only predicted root cause component is Tomcat02\n",
    ),
]
RECORDS = [
    ("Tomcat01", "2021-03-04 10:12:00", "high CPU usage"),
    ("MG01", "2021-03-04 23:41:00", "network latency"),
    ("Redis02", "2021-03-05 00:05:00", "high memory usage"),
    ("apache01", "2021-03-05 10:08:00", "network packet loss"),
    ("IG02", "2021-03-05 10:20:00", "high disk I/O read usage"),
    ("Tomcat02", "2021-03-05 15:17:00", "high CPU usage"),  # a second cpu window: one to dev
]


@dataclass
class BankFixture:
    root: Path  # what `fetch` downloads into: <root>/Bank/...
    sizes: dict[str, int]
    query_sha: str
    record_sha: str
    labels_sha: str
    split_text: str

    def pins(self) -> dict[str, object]:
        """check_raw's pins for this download, as the real ones are for the real one."""
        return {
            "sizes": self.sizes,
            "query_sha": self.query_sha,
            "record_sha": self.record_sha,
            "labels_sha": self.labels_sha,
            "split_text": self.split_text,
        }


def _csv_field(s: str) -> str:
    return '"' + s.replace('"', '""') + '"' if any(c in s for c in ',"\n') else s


def make_bank(root: Path) -> BankFixture:
    bank = root / "Bank"
    bank.mkdir(parents=True)
    q = "task_index,instruction,scoring_points\n" + "".join(
        f"{t},{_csv_field(i)},{_csv_field(p)}\n" for t, i, p in QUERIES
    )
    r = "level,component,timestamp,datetime,reason\n" + "".join(
        f"pod,{c},{local_ms(d) / 1000:.1f},{d},{why}\n" for c, d, why in RECORDS
    )
    (bank / "query.csv").write_text(q)
    (bank / "record.csv").write_text(r)
    sizes: dict[str, int] = {}
    for day in DAYS:
        y, m, d = day.split("_")
        day0 = local_ms(f"{y}-{m}-{d} 00:00:00") // 1000
        minutes = [day0 + 60 * i for i in range(24 * 60)]
        files = {
            "log/log_service.csv": ["log_id,timestamp,cmdb_id,log_name,value"]
            + [
                f"id{t},{t},Tomcat01,gc,"
                + _csv_field(f"[GC (Allocation Failure) {t}, 0.08 secs]\n  [Times: real=0.08]")
                for t in minutes
            ]
            + ["this line,is,malformed"],
            "metric/metric_container.csv": ["timestamp,cmdb_id,kpi_name,value"]
            + [
                f"{t},{c},{k},{v}"
                for t in minutes
                for c in ("Tomcat01", "Mysql02")
                for k, v in (
                    ("OSLinux-CPU_CPU_CPUCpuUtil", 20.5),
                    ("Mysql-MySQL_3306_Innodb data reads", 0.0),
                )
            ]
            + [f"{minutes[0]},Tomcat01,OSLinux-CPU_CPU_CPUCpuUtil,30.5"],  # recorded twice
            "metric/metric_app.csv": ["timestamp,rr,sr,cnt,mrt,tc"]
            + [f"{t},100.0,100.0,22,53.27,ServiceTest1" for t in minutes],
            "trace/trace_span.csv": ["timestamp,cmdb_id,parent_id,span_id,trace_id,duration"]
            + [
                row
                for t in minutes
                for row in (
                    f"{t * 1000 + 257},dockerB2,root-{t},{int(BIG_ID) + t},gw{t},67",
                    f"{t * 1000 + 259},dockerB2,{int(BIG_ID) + t},child-{t},gw{t},3",
                )
            ],
        }
        for rel, lines in files.items():
            p = bank / "telemetry" / day / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(lines) + "\n")
            sizes[f"Bank/telemetry/{day}/{rel}"] = p.stat().st_size
    labels = openrca.labels_from_csv(q, r)
    return BankFixture(
        root=root,
        sizes=sizes,
        query_sha=hashlib.sha256((bank / "query.csv").read_bytes()).hexdigest(),
        record_sha=hashlib.sha256((bank / "record.csv").read_bytes()).hexdigest(),
        labels_sha=hashlib.sha256(openrca.labels_json(labels).encode()).hexdigest(),
        split_text=openrca.split_json(openrca.make_split(labels)),
    )
