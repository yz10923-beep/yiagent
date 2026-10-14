"""Harness integrity, as in kama: scores are comparable only if what decides them did not
change between runs. Any edit to the graders, baselines, labels, split or the data
reading they rely on changes the hash, and scoring stops until Yi re-approves it; an
agent tuning prompts must never be able to quietly make the grader easier."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from yi_agent.data.openrca import SPLIT_PATH as BANK_SPLIT_PATH
from yi_agent.data.rcaeval import LABELS_PATH, SPLIT_PATH

_SRC = Path(__file__).resolve().parents[1]  # src/yi_agent
REPO_ROOT = _SRC.parents[1]
APPROVAL_FILE = REPO_ROOT / "evals" / "harness.sha256"
RESULTS_DIR = REPO_ROOT / "evals" / "results"


class HarnessNotApproved(Exception):
    pass


def hashed_files() -> list[Path]:
    files = sorted((_SRC / "eval").glob("*.py"))
    files += [_SRC / "data" / "rcaeval.py", _SRC / "data" / "telemetry.py"]
    files += [LABELS_PATH, SPLIT_PATH]
    # OpenRCA Bank (S5): its labels are not in the repo; their pinned hash is in openrca.py
    files += [_SRC / "data" / "openrca.py", BANK_SPLIT_PATH]
    return files


def harness_sha() -> str:
    h = hashlib.sha256()
    for p in hashed_files():
        h.update(p.relative_to(REPO_ROOT).as_posix().encode())
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def check_approval(approve: bool) -> str:
    """The current hash, if approved. `approve` is Yi's flag and only Yi passes it."""
    sha = harness_sha()
    if approve:
        APPROVAL_FILE.write_text(sha + "\n")
        return sha
    approved = APPROVAL_FILE.read_text().strip() if APPROVAL_FILE.exists() else ""
    if approved != sha:
        raise HarnessNotApproved(
            f"the harness changed since it was approved (approved={approved[:12] or 'none'}, "
            f"now={sha[:12]}). Review `git diff -- src/yi_agent/eval src/yi_agent/data evals/`, "
            "run `make verify` (the grader selftest is in it), then re-run with "
            "--approve-harness."
        )
    return sha


def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        dirty = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")


def provenance(sha: str) -> dict[str, Any]:
    return {
        "harness_sha256": sha,
        "git_commit": git_commit(),
        "graded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, default=str) + "\n")
