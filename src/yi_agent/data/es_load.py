"""Load one case into fresh Elasticsearch indices, and delete them after the trial.

Index names carry only the opaque case id. Every load is verified end to end: the
documents Elasticsearch counts must equal the documents produced from the source rows,
or the load fails. A silent drop would otherwise score as an agent mistake.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from elasticsearch import Elasticsearch
from elasticsearch.helpers import streaming_bulk

from yi_agent.data.telemetry import KINDS, FileStats, Kind, iter_docs, source_paths

_DATE = {"type": "date", "format": "epoch_millis"}
# Strings are exact-match keywords by default: service names, ids, status codes.
_STRINGS_AS_KEYWORDS = {
    "strings": {
        "match_mapping_type": "string",
        "mapping": {"type": "keyword", "ignore_above": 2048},
    }
}
# JSON decimals would otherwise map to 32-bit float: a microsecond time or a large
# duration stored as a double would silently lose precision.
_DECIMALS_AS_DOUBLE = {"decimals": {"match_mapping_type": "double", "mapping": {"type": "double"}}}
MAPPINGS: dict[Kind, dict[str, Any]] = {
    "logs": {
        "dynamic_templates": [_STRINGS_AS_KEYWORDS, _DECIMALS_AS_DOUBLE],
        "properties": {
            "@timestamp": _DATE,
            # full-text search, plus the exact line for grouping and counting
            "message": {
                "type": "text",
                "fields": {"raw": {"type": "keyword", "ignore_above": 2048}},
            },
        },
    },
    "traces": {
        "dynamic_templates": [_STRINGS_AS_KEYWORDS, _DECIMALS_AS_DOUBLE],
        "properties": {"@timestamp": _DATE},
    },
    "metrics": {
        "dynamic": "strict",
        "properties": {
            "@timestamp": _DATE,
            "metric": {"type": "keyword"},
            "value": {"type": "double"},
        },
    },
}
# One node: replicas could never be placed. Refresh is off while loading, on after.
_LOAD_SETTINGS = {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "-1"}
CHUNK_DOCS = 5_000


class LoadError(Exception):
    pass


def index_name(case_id: str, kind: Kind) -> str:
    return f"yi-{case_id}-{kind}"


@dataclass
class LoadReport:
    case_id: str
    indices: dict[str, int] = field(default_factory=dict)  # index -> documents
    stats: dict[str, FileStats] = field(default_factory=dict)  # kind -> source stats
    seconds: float = 0.0

    @property
    def docs(self) -> int:
        return sum(self.indices.values())


def unload_case(es: Elasticsearch, case_id: str) -> None:
    # Explicit names: wildcard deletes are refused by default (destructive_requires_name).
    es.indices.delete(
        index=[index_name(case_id, k) for k in KINDS],
        ignore_unavailable=True,
        allow_no_indices=True,
    )


def load_case(es: Elasticsearch, case_id: str, case_dir: Path) -> LoadReport:
    """Replace any earlier load of this case. Raises LoadError on any lost document."""
    t0 = time.monotonic()
    report = LoadReport(case_id=case_id)
    unload_case(es, case_id)
    try:
        for kind, path in source_paths(case_dir).items():
            name = index_name(case_id, kind)
            es.indices.create(index=name, settings=_LOAD_SETTINGS, mappings=MAPPINGS[kind])
            stats = FileStats()
            actions = ({"_index": name, "_source": d} for d in iter_docs(kind, path, stats))
            indexed = 0
            for ok, item in streaming_bulk(
                es, actions, chunk_size=CHUNK_DOCS, raise_on_error=False, max_retries=3
            ):
                if not ok:
                    raise LoadError(f"{name}: document rejected: {_short(item)}")
                indexed += 1
            es.indices.put_settings(index=name, settings={"refresh_interval": None})
            es.indices.refresh(index=name)
            counted = int(es.count(index=name)["count"])
            if not (stats.docs == indexed == counted):
                raise LoadError(
                    f"{name}: produced {stats.docs} documents, indexed {indexed}, counted {counted}"
                )
            report.indices[name] = counted
            report.stats[kind] = stats
    except BaseException:
        with contextlib.suppress(Exception):  # never leave a half-loaded case behind
            unload_case(es, case_id)
        raise
    report.seconds = time.monotonic() - t0
    return report


def _short(item: Any, limit: int = 300) -> str:
    s = str(item)
    return s if len(s) <= limit else s[:limit] + "..."
