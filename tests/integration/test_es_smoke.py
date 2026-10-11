"""The VM's Elasticsearch works end to end: create an index, bulk-load log lines,
search them, delete the index. Stdlib HTTP only, so it checks the server, not a client
library. Run with `make es-test` (needs `make es-up`)."""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request
from typing import Any

import pytest

pytestmark = pytest.mark.es

ES = os.environ.get("YI_ES_URL", "http://127.0.0.1:9200").rstrip("/")


def es(method: str, path: str, body: Any = None, ndjson: str | None = None) -> Any:
    data, ctype = None, "application/json"
    if ndjson is not None:
        data, ctype = ndjson.encode(), "application/x-ndjson"
    elif body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(
        ES + path, data=data, method=method, headers={"Content-Type": ctype}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.URLError as e:
        pytest.fail(f"Elasticsearch not reachable at {ES} ({e}); run `make es-up`")
    except TimeoutError:
        pytest.fail(
            f"{method} {path} timed out: shards can't be placed. Usually the disk is over "
            "ES's 90% high watermark (docs/SETUP.md, troubleshooting)"
        )


def test_index_bulk_search_delete_roundtrip() -> None:
    index = f"yi-smoke-{secrets.token_hex(4)}"
    es(
        "PUT",
        f"/{index}",
        {
            "mappings": {
                "properties": {
                    "ts": {"type": "date", "format": "epoch_second"},
                    "service": {"type": "keyword"},
                    "message": {"type": "text"},
                }
            }
        },
    )
    try:
        lines = [
            ("cartservice", "GET /cart 200"),
            ("checkoutservice", "rpc error: code = Unavailable desc = connection refused"),
            ("checkoutservice", "GET /checkout 200"),
        ]
        bulk = "".join(
            json.dumps({"index": {"_index": index}})
            + "\n"
            + json.dumps({"ts": 1_700_000_000 + i, "service": s, "message": m})
            + "\n"
            for i, (s, m) in enumerate(lines)
        )
        result = es("POST", "/_bulk?refresh=true", ndjson=bulk)
        assert result["errors"] is False
        hits = es(
            "POST",
            f"/{index}/_search",
            {
                "query": {
                    "bool": {
                        "must": [{"match": {"message": "connection refused"}}],
                        "filter": [{"term": {"service": "checkoutservice"}}],
                    }
                },
            },
        )["hits"]["hits"]
        assert [h["_source"]["message"] for h in hits] == [lines[1][1]]
    finally:
        es("DELETE", f"/{index}")
