"""No label reaches the agent (CLAUDE.md, Invariants).

What the agent can see today is the alert, the index names and the loaded documents.
Each is checked here: the alert over all 360 real cases, the documents over fixture cases
written under real case names. The agent's tools (S3) add their own checks."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import pytest

from tests.rcaeval_fixture import SECRET, make_case
from yi_agent.data.alert import make_alert
from yi_agent.data.es_load import index_name
from yi_agent.data.rcaeval import FAULTS, LABELS_PATH, CaseLabel, parse_case_name, read_labels
from yi_agent.data.telemetry import KINDS, CaseWindow, FileStats, iter_docs, source_paths

LABELS = read_labels(LABELS_PATH)
SERVICES: dict[str, set[str]] = defaultdict(set)  # every root-cause service, per system
for _c in LABELS:
    SERVICES[_c.system].add(_c.service)


def label_tokens(c: CaseLabel) -> list[str]:
    """Strings whose presence would give the answer away (or narrow it)."""
    return [c.name, f"{c.suite}{c.system}", *SERVICES[c.system], *FAULTS[c.suite]]


def leaks(text: str, c: CaseLabel) -> list[str]:
    return [
        t for t in label_tokens(c) if re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", text, re.I)
    ]


@pytest.mark.parametrize("c", LABELS, ids=lambda c: c.id)
def test_alert_names_no_service_no_fault_no_case(c: CaseLabel) -> None:
    window = CaseWindow(
        onset_ms=1_705_935_125_000,
        start_ms=1_705_934_405_000,
        end_ms=1_705_935_845_000,
        sources=KINDS,
    )
    alert = make_alert(c, window)
    text = alert.render() + alert.model_dump_json()
    assert leaks(text, c) == []
    assert c.id in text


def test_the_leak_check_itself_catches_leaks() -> None:
    c = parse_case_name("re2tt_ts-order-service_cpu_1")
    assert leaks("root cause is ts-order-service", c) == ["ts-order-service"]
    assert leaks("injected cpu stress", c) == ["cpu"]
    assert leaks("see re2tt_ts-order-service_cpu_1", c)


def test_index_names_carry_only_the_opaque_id() -> None:
    for c in LABELS:
        for kind in KINDS:
            name = index_name(c.id, kind)
            assert re.fullmatch(r"yi-c[0-9a-f]{10}-(logs|traces|metrics)", name)
            assert leaks(name.replace(kind, ""), c) == []


@pytest.mark.parametrize(
    "name", ["re2tt_ts-order-service_cpu_1", "re3ss_carts_f1_1", "re2ob_emailservice_socket_2"]
)
def test_loaded_documents_carry_no_path_name_or_root_cause_file(tmp_path: Path, name: str) -> None:
    case_dir = make_case(tmp_path, name)
    docs = [
        d
        for kind, path in source_paths(case_dir).items()
        for d in iter_docs(kind, path, FileStats())
    ]
    blob = json.dumps(docs)
    assert name not in blob and str(tmp_path) not in blob
    assert SECRET not in blob  # root_cause.txt is never read
    assert re.search(r"re[123](ob|ss|tt)", blob) is None
