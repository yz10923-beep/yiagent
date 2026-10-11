"""Load one case into fresh Elasticsearch indices, and delete them after the trial.

Index names carry only the opaque case id. Every load is verified end to end: the
documents Elasticsearch counts must equal the documents produced from the source rows,
or the load fails. A silent drop would otherwise score as an agent mistake.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from elasticsearch import Elasticsearch
from elasticsearch.helpers import parallel_bulk

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
# One node: replicas could never be placed. While loading, refresh is off and the translog
# is not fsynced per request (a crash mid-load means a reload anyway); both are restored.
_LOAD_SETTINGS = {
    "number_of_shards": 1,
    "number_of_replicas": 0,
    "refresh_interval": "-1",
    "translog.durability": "async",
}
_AFTER_LOAD = {"refresh_interval": None, "translog.durability": "request"}
CHUNK_DOCS = 5_000
BULK_THREADS = 3  # one Python thread builds documents; these keep the node busy
PROGRESS_EVERY = 250_000


# Bump when documents change shape: indices from another version are reloaded, not reused.
LOADER_VERSION = 1


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
    reused: bool = False  # ensure_loaded found a complete load from this loader version

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


def load_case(
    es: Elasticsearch,
    case_id: str,
    case_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> LoadReport:
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
            indexed, t_kind = 0, time.monotonic()
            for ok, item in parallel_bulk(
                es,
                actions,
                thread_count=BULK_THREADS,
                chunk_size=CHUNK_DOCS,
                raise_on_error=False,
                raise_on_exception=True,
            ):
                if not ok:
                    raise LoadError(f"{name}: document rejected: {_short(item)}")
                indexed += 1
                if progress and indexed % PROGRESS_EVERY == 0:
                    rate = indexed / max(time.monotonic() - t_kind, 1e-9)
                    progress(f"  {kind}: {indexed:,} docs ({rate:,.0f}/s)")
            es.indices.put_settings(index=name, settings=_AFTER_LOAD)
            es.indices.refresh(index=name)
            counted = int(es.count(index=name)["count"])
            if not (stats.docs == indexed == counted):
                raise LoadError(
                    f"{name}: produced {stats.docs} documents, indexed {indexed}, counted {counted}"
                )
            # The marker is written last: an index without it is a load that did not finish.
            es.indices.put_mapping(
                index=name, meta={"yi_loader": LOADER_VERSION, "complete": True, "docs": counted}
            )
            report.indices[name] = counted
            report.stats[kind] = stats
    except BaseException:
        with contextlib.suppress(Exception):  # never leave a half-loaded case behind
            unload_case(es, case_id)
        raise
    report.seconds = time.monotonic() - t0
    return report


def ensure_loaded(
    es: Elasticsearch,
    case_id: str,
    case_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> LoadReport:
    """Reuse a complete load of this case from this loader version, or load it afresh.

    Loading a large case takes about a minute, so cases stay loaded across trials; one
    case's indices are only ever queried through tools bound to that case's id."""
    t0 = time.monotonic()
    report = LoadReport(case_id=case_id, reused=True)
    for kind in source_paths(case_dir):
        name = index_name(case_id, kind)
        meta = _complete_meta(es, name)
        if meta is None or int(es.count(index=name)["count"]) != meta["docs"]:
            return load_case(es, case_id, case_dir, progress)
        report.indices[name] = int(meta["docs"])
    report.seconds = time.monotonic() - t0
    return report


def _complete_meta(es: Elasticsearch, name: str) -> dict[str, Any] | None:
    if not es.indices.exists(index=name):
        return None
    meta = es.indices.get_mapping(index=name)[name]["mappings"].get("_meta") or {}
    if meta.get("yi_loader") != LOADER_VERSION or meta.get("complete") is not True:
        return None
    return dict(meta)


def _short(item: Any, limit: int = 300) -> str:
    s = str(item)
    return s if len(s) <= limit else s[:limit] + "..."
