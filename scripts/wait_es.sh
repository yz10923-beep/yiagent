#!/usr/bin/env bash
# Wait until Elasticsearch answers with a usable cluster health, or fail after 120s.
set -euo pipefail
url="${YI_ES_URL:-http://127.0.0.1:9200}"
for _ in $(seq 1 60); do
  if status=$(curl -fs "$url/_cluster/health" 2>/dev/null); then
    case "$status" in
      *'"status":"green"'*|*'"status":"yellow"'*) echo "elasticsearch up: $url"; exit 0 ;;
    esac
  fi
  sleep 2
done
echo "elasticsearch not healthy at $url after 120s (docker compose logs elasticsearch)" >&2
exit 1
