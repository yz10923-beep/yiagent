"""The frozen split. Changing it invalidates every dev number measured so far, so its
hash is pinned here; a change must be deliberate and recorded in docs/ROADMAP.md."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict

from yi_agent.data.rcaeval import LABELS_PATH, SPLIT_PATH, read_labels
from yi_agent.data.split import make_split, read_split, split_hash, split_json

SPLIT_SHA256 = "4d25c2e8a7cee1be6d4e26f1eea66f2cef98bebe4ae568add24c5ebae1f9e13f"
LABELS_SHA256 = "ac0dd19a0253bc5e14266e43e7ffe3af64f6f27f46b65689f3972e2706197857"


def test_split_file_is_the_frozen_one() -> None:
    assert split_hash(SPLIT_PATH.read_text()) == SPLIT_SHA256


def test_labels_file_is_the_frozen_one() -> None:
    assert hashlib.sha256(LABELS_PATH.read_bytes()).hexdigest() == LABELS_SHA256


def test_split_is_reproducible_from_the_labels() -> None:
    assert split_json(make_split(read_labels(LABELS_PATH))) == SPLIT_PATH.read_text()


def test_labels_cover_re2_and_re3() -> None:
    labels = read_labels(LABELS_PATH)
    assert len(labels) == 360
    counts = Counter(c.suite + c.system for c in labels)
    assert counts == {"re2ob": 90, "re2ss": 90, "re2tt": 90, "re3ob": 30, "re3ss": 30, "re3tt": 30}


def test_split_partitions_by_group() -> None:
    labels = {c.id: c for c in read_labels(LABELS_PATH)}
    s = read_split(SPLIT_PATH)
    dev, test = set(s.dev), set(s.test)
    assert dev.isdisjoint(test) and dev | test == set(labels)
    side = defaultdict(set)
    for i in labels:
        side[labels[i].group].add("dev" if i in dev else "test")
    assert all(len(v) == 1 for v in side.values()), "a group is on both sides"
    # dev_small: exactly one repetition of every dev group
    small_groups = [labels[i].group for i in s.dev_small]
    assert set(s.dev_small) <= dev
    assert sorted(small_groups) == sorted({labels[i].group for i in dev})


def test_every_stratum_is_in_dev_and_in_test() -> None:
    labels = {c.id: c for c in read_labels(LABELS_PATH)}
    s = read_split(SPLIT_PATH)
    dev = set(s.dev)
    re2_dev_groups = Counter(
        (c.system, c.fault)
        for c in {labels[i].group: labels[i] for i in dev}.values()
        if c.suite == "re2"
    )
    assert len(re2_dev_groups) == 18 and set(re2_dev_groups.values()) == {1}
    # every (suite, system, fault) keeps cases in test, so test can be reported per fault
    strata_in_test = {(labels[i].suite, labels[i].system, labels[i].fault) for i in s.test}
    assert strata_in_test == {(c.suite, c.system, c.fault) for c in labels.values()}
    # RE3 dev covers each system with distinct faults and distinct root-cause services
    for system in ("ob", "ss", "tt"):
        groups = {
            labels[i].group for i in dev if labels[i].suite == "re3" and labels[i].system == system
        }
        assert len(groups) == 2
        assert len({g[3] for g in groups}) == 2 and len({g[2] for g in groups}) == 2
