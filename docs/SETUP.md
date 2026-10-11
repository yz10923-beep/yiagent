# Setup

## What runs where

| Work | Laptop or cloud session | VM |
|---|---|---|
| Writing code, `make verify` (unit tests on fakes and tiny fixtures) | yes | yes |
| `make es-test` (Elasticsearch integration) | if Docker is there | yes |
| The RCAEval data (about 3 GB of Parquet for RE2 + RE3) | no | yes |
| Eval runs (paid, long, need the API key and Elasticsearch) | no | yes |

Develop anywhere. The VM is where Elasticsearch, the data and the paid runs live.

## VM requirements

- 64-bit Linux with Docker Engine and the compose plugin.
- **RAM: 8 GB minimum, 16 GB comfortable.** Elasticsearch takes its heap (2 GB by
  default, `YI_ES_HEAP`) plus OS page cache; Kibana adds about 1 GB.
- **Disk: 60 GB free.** About 3 GB of data, a few GB of Docker images, and the indexed
  telemetry. One case is about 50-90K log lines and 100-250K spans. Elasticsearch
  stops placing data when its disk is **90% full** (high watermark), so free space
  matters more than total size.
- 2+ vCPUs.

Check yours:

```bash
nproc; free -h; df -h ~; head -2 /etc/os-release; docker --version; docker compose version
```

## One-time setup

1. **Docker Engine** (skip if `docker compose version` works). Follow the official
   steps for your distro: https://docs.docker.com/engine/install/ . Then let your user
   run it without sudo, and log out and back in:

   ```bash
   sudo usermod -aG docker "$USER"
   ```

2. **Kernel setting Elasticsearch needs** (mmap count), now and after reboots:

   ```bash
   echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-elasticsearch.conf
   sudo sysctl --system
   ```

3. **The repo** (uv and Python 3.12 are already on the VM from kama):

   ```bash
   git clone https://github.com/yz10923-beep/Yi-agent.git && cd Yi-agent
   uv sync && make verify
   ```

4. **Config.** Put the API key in `~/.yi/.env` (mode 600), from `.env.example`:

   ```bash
   mkdir -p ~/.yi && cp .env.example ~/.yi/.env && chmod 600 ~/.yi/.env
   # then set ANTHROPIC_API_KEY (the same key as ~/.kama/.env is fine)
   ```

5. **Elasticsearch**, then the smoke test:

   ```bash
   make es-up        # docker compose up -d elasticsearch, waits for health
   make es-test      # create an index, bulk-load, search, delete
   ```

6. **Kibana (optional, for looking at the data yourself).** It binds to 127.0.0.1
   only, so reach it through an SSH tunnel from your laptop:

   ```bash
   docker compose --profile kibana up -d          # on the VM
   ssh -L 5601:127.0.0.1:5601 <you>@<vm>          # on the laptop, then open localhost:5601
   ```

Security is off in this compose file (no TLS, no auth) because nothing is exposed:
both ports bind to 127.0.0.1. **Never open 9200 or 5601 in the firewall** or change
the binding to 0.0.0.0.

The data download (`yi data fetch`) arrives with S1; nothing to download yet.

## Troubleshooting

- **Health stays red, or index creation times out:** the disk is over the 90% high
  watermark. Free space. On a throwaway dev box only, you can switch the check off:
  `curl -XPUT 127.0.0.1:9200/_cluster/settings -H 'Content-Type: application/json'
  -d '{"persistent":{"cluster.routing.allocation.disk.threshold_enabled":false}}'`
- **`error setting rlimit type 7: operation not permitted`:** the VM is a restricted
  container (some VPS types) that can't raise the open-file limit. Delete the
  `ulimits:` block from `docker-compose.yml`.
- **Elasticsearch exits right after start:** `docker compose logs elasticsearch`.
  Usually `vm.max_map_count` (step 2) or too little RAM for the heap (lower
  `YI_ES_HEAP`, e.g. `YI_ES_HEAP=1g make es-up`).
- **Stop or reset:** `make es-down` stops it and keeps the data volume;
  `docker compose down -v` also deletes the indexed data.
