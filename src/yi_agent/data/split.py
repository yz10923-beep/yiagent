"""The frozen dev/test split: grouped, stratified, seeded (docs/ROADMAP.md, S1 decisions).

A group is every repetition of one injection (suite, system, service, fault). Groups,
not cases, are assigned, so no injection is in both dev and test.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel

from yi_agent.data.rcaeval import ID_SALT, CaseLabel

SPLIT_SEED = 20261011
RE3_DEV_GROUPS_PER_SYSTEM = 2

type Group = tuple[str, str, str, str]


class Split(BaseModel):
    seed: int
    salt: str
    dev: list[str]
    dev_small: list[str]  # one repetition per dev group
    test: list[str]


def _groups(labels: list[CaseLabel]) -> dict[Group, list[CaseLabel]]:
    out: dict[Group, list[CaseLabel]] = defaultdict(list)
    for c in labels:
        out[c.group].append(c)
    return dict(sorted(out.items()))


def _dev_groups(groups: dict[Group, list[CaseLabel]], rng: random.Random) -> set[Group]:
    strata: dict[tuple[str, str, str], list[Group]] = defaultdict(list)
    for g in groups:  # sorted, so the draw below is reproducible
        suite, system, _, fault = g
        strata[(suite, system, fault)].append(g)
    dev: set[Group] = set()
    # RE2 is a full grid: one of the five groups of every (system, fault).
    for (suite, _, _), gs in sorted(strata.items()):
        if suite == "re2":
            dev.add(rng.choice(gs))
    # RE3 is not: per system, a few groups with distinct faults, only from faults that
    # have another group to leave in test.
    for system in sorted({s for (suite, s, _) in strata if suite == "re3"}):
        eligible = [
            g
            for (suite, s, _), gs in sorted(strata.items())
            if suite == "re3" and s == system and len(gs) >= 2
            for g in gs
        ]
        rng.shuffle(eligible)
        # Distinct faults always; distinct root-cause services where the system has them,
        # so dev does not teach "the answer in RE3-TT is always ts-route-service".
        picked: list[Group] = []
        for need_new_service in (True, False):
            for g in eligible:
                if len(picked) == RE3_DEV_GROUPS_PER_SYSTEM:
                    break
                if g in picked or g[3] in {p[3] for p in picked}:
                    continue
                if need_new_service and g[2] in {p[2] for p in picked}:
                    continue
                picked.append(g)
        dev.update(picked)
    return dev


def make_split(labels: list[CaseLabel], seed: int = SPLIT_SEED) -> Split:
    groups = _groups(labels)
    dev_groups = _dev_groups(groups, random.Random(seed))
    dev, small, test = [], [], []
    for g, cases in groups.items():
        if g in dev_groups:
            dev += [c.id for c in cases]
            small.append(min(cases, key=lambda c: c.rep).id)
        else:
            test += [c.id for c in cases]
    return Split(
        seed=seed, salt=ID_SALT, dev=sorted(dev), dev_small=sorted(small), test=sorted(test)
    )


def split_json(split: Split) -> str:
    """Canonical bytes: the pinned hash is over exactly this."""
    return json.dumps(split.model_dump(), indent=1) + "\n"


def split_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def read_split(path: Path) -> Split:
    return Split.model_validate_json(path.read_text())
