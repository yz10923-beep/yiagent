"""RCAEval: case names, opaque ids, labels, and the download.

A case directory's name is its label (`re2tt_ts-order-service_cpu_1`: suite re2, Train
Ticket, root cause ts-order-service, fault cpu, repetition 1). Everything in this module
is harness-side: names and labels never reach the agent, only `case_id(name)` does.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

HF_REPO = "phamquiluan/RCAEval"
_REPO_ROOT = Path(__file__).resolve().parents[3]
LABELS_PATH = _REPO_ROOT / "evals" / "grader" / "labels.json"  # harness and graders only
SPLIT_PATH = _REPO_ROOT / "evals" / "split.json"
SUITES = ("re2", "re3")  # RE1 is metric-only and out of scope
SYSTEMS = {"ob": "Online Boutique", "ss": "Sock Shop", "tt": "Train Ticket"}
FAULTS = {
    "re2": ("cpu", "mem", "disk", "delay", "loss", "socket"),
    "re3": ("f1", "f2", "f3", "f4", "f5"),
}
# Changing the salt changes every id, so it is part of the frozen split.
ID_SALT = "yi-rcaeval-v1"

_NAME = re.compile(
    r"(?P<suite>re[123])(?P<system>ob|ss|tt)_(?P<service>[a-z0-9-]+)_"
    r"(?P<fault>cpu|mem|disk|delay|loss|socket|f[1-5])_(?P<rep>[1-9][0-9]*)"
)


class CaseLabel(BaseModel):
    """What the grader knows about a case. Never shown to the agent."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    suite: str
    system: str
    service: str
    fault: str
    rep: int

    @property
    def group(self) -> tuple[str, str, str, str]:
        """Repetitions of one injection: they always land on the same side of the split."""
        return (self.suite, self.system, self.service, self.fault)


def case_id(name: str) -> str:
    return "c" + hashlib.sha256(f"{ID_SALT}:{name}".encode()).hexdigest()[:10]


def parse_case_name(name: str) -> CaseLabel:
    m = _NAME.fullmatch(name)
    if m is None:
        raise ValueError(f"not an RCAEval case name: {name!r}")
    if m["fault"] not in FAULTS.get(m["suite"], ()):
        raise ValueError(f"fault {m['fault']!r} is not a {m['suite']} fault: {name!r}")
    return CaseLabel(
        id=case_id(name),
        name=name,
        suite=m["suite"],
        system=m["system"],
        service=m["service"],
        fault=m["fault"],
        rep=int(m["rep"]),
    )


def labels_from_names(names: list[str]) -> list[CaseLabel]:
    """RE2 and RE3 cases only, sorted by name; fails on an id collision."""
    labels = sorted((parse_case_name(n) for n in names if n[:3] in SUITES), key=lambda c: c.name)
    ids = [c.id for c in labels]
    if len(set(ids)) != len(ids):
        raise ValueError("case id collision; change ID_SALT")
    return labels


def write_labels(labels: list[CaseLabel], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [c.model_dump() for c in labels]
    path.write_text(json.dumps(rows, indent=1) + "\n")


def read_labels(path: Path) -> list[CaseLabel]:
    return [CaseLabel.model_validate(r) for r in json.loads(path.read_text())]


def fetch(data_dir: Path) -> Path:
    """Download RE2, RE3 and the case index (about 3 GB) into data_dir. Resumable."""
    from huggingface_hub import snapshot_download  # imported here: only the VM needs it

    data_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=HF_REPO,
        repo_type="dataset",
        allow_patterns=[f"{s}*" for s in SUITES] + ["cases.parquet", "README.md"],
        local_dir=data_dir,
    )
    return data_dir
