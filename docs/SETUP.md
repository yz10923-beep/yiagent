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

Tested on a DigitalOcean droplet: Ubuntu 24.04, 4 vCPU, 8 GB, 160 GB disk (2026-10-11).

1. **Base packages and Docker Engine** (skip Docker if `docker compose version` works).
   Docker Desktop on your laptop is no help here; the VM needs its own Docker Engine
   (official steps: https://docs.docker.com/engine/install/ ). On Ubuntu or Debian:

   ```bash
   sudo apt-get update && sudo apt-get install -y git make curl ca-certificates
   . /etc/os-release   # $ID is ubuntu or debian
   sudo install -m 0755 -d /etc/apt/keyrings
   sudo curl -fsSL https://download.docker.com/linux/$ID/gpg -o /etc/apt/keyrings/docker.asc
   sudo chmod a+r /etc/apt/keyrings/docker.asc
   echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
     | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
   sudo apt-get update
   sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   sudo systemctl enable --now docker   # the daemon may not be running after install
   docker run --rm hello-world
   ```

   If you are not root, let your user run Docker without sudo, then log out and back in:
   `sudo usermod -aG docker "$USER"`.

2. **Kernel setting Elasticsearch needs** (mmap count), now and after reboots:

   ```bash
   echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-elasticsearch.conf
   sudo sysctl --system
   ```

3. **uv, Python 3.12 and the repo.** Work happens on one branch (see CLAUDE.md):

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh && source $HOME/.local/bin/env
   uv python install 3.12
   git clone https://github.com/yz10923-beep/yiagent.git && cd yiagent
   git checkout claude/jolly-euler-45xyyc
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
   docker compose stop kibana                     # when done: it uses about 1 GB of RAM
   ```

   Run the `ssh -L` on the laptop, not in the VM's shell: inside the VM it tries to SSH
   to itself and fails with `Permission denied (publickey)`.

Security is off in this compose file (no TLS, no auth) because nothing is exposed:
both ports bind to 127.0.0.1. **Never open 9200 or 5601 in the firewall** or change
the binding to 0.0.0.0.

The data download (`yi data fetch`) arrives with S1; nothing to download yet.

## Troubleshooting

- **Health stays red, or index creation times out:** the disk is over the 90% high
  watermark. Free space. On a throwaway dev box only, you can switch the check off:
  `curl -XPUT 127.0.0.1:9200/_cluster/settings -H 'Content-Type: application/json'
  -d '{"persistent":{"cluster.routing.allocation.disk.threshold_enabled":false}}'`
- **`Cannot connect to the Docker daemon`:** the daemon isn't running:
  `sudo systemctl enable --now docker`.
- **`error setting rlimit type 7: operation not permitted`:** the VM is a restricted
  container (some VPS types) that can't raise the open-file limit. Delete the
  `ulimits:` block from `docker-compose.yml`.
- **Elasticsearch exits right after start:** `docker compose logs elasticsearch`.
  Usually `vm.max_map_count` (step 2) or too little RAM for the heap (lower
  `YI_ES_HEAP`, e.g. `YI_ES_HEAP=1g make es-up`).
- **Stop or reset:** `make es-down` stops it and keeps the data volume;
  `docker compose down -v` also deletes the indexed data.
