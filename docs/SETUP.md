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

7. **The data (S1)**, about 3 GB into `YI_DATA_DIR` (default `~/yi-data`). Each command
   prints what to send back; none of them costs money.

   ```bash
   cd ~/yiagent && git pull && uv sync && make verify
   make es-up
   uv run yi data fetch                  # resumable; rerun if interrupted
   uv run yi data check                  # download vs frozen labels; expect 0 problems
   uv run yi data inspect                # real schemas and time units, one case per system
   uv run yi data audit --split all      # anything in the telemetry that could leak the answer
   uv run yi data load --split dev_small --show-alert   # load, verify, unload, timed
   df -h ~ && docker system df           # disk after the load
   ```

8. **The eval harness (S2).** No Elasticsearch needed: the graders and BARO read the
   Parquet files. Nothing here costs money.

   ```bash
   cd ~/yiagent && git pull && uv sync && make verify
   uv run yi eval metric-kinds --split dev           # metric families per system
   uv run yi eval reproduce-baro --systems tt ob ss  # our BARO vs the published RE2-TT numbers
   ```

   Then review what you are approving (`git log -p -- src/yi_agent/eval`), and approve it
   by scoring the oracle with the flag. Only you pass `--approve-harness`:

   ```bash
   uv run yi eval baseline oracle --split dev --approve-harness   # must print 1.00s
   uv run yi eval baseline random --split dev
   uv run yi eval baseline baro --split dev
   uv run yi eval baseline shape-time --split dev
   uv run yi eval baseline shape-columns --split dev
   uv run yi eval evidence-coverage --split dev
   cat evals/harness.sha256
   ```

   Send the hash so it is committed with the code it approves; before the next `git pull`,
   `rm evals/harness.sha256` (the pulled copy replaces it).

9. **The agent (S3). This costs money.** Elasticsearch up, the cases loaded (dev_small
   was loaded in step 7 with `--keep`; a case that is not loaded is loaded first, which
   takes up to two minutes for Train Ticket), `ANTHROPIC_API_KEY` in `~/.yi/.env`.

   ```bash
   cd ~/yiagent && git pull && uv sync && make verify && make es-up
   # plumbing on real data, near free (Haiku 5.5); its scores are not results
   uv run yi agent run --split dev_small --model claude-haiku-5-5 --variant smoke-haiku
   # controls, before trusting any score: BARO on the same cases, the tool alone, placebo
   uv run yi eval baseline baro --split dev_small
   uv run yi agent run --split dev_small --model policy:metric-top --variant control-metric-top
   uv run yi agent run --split dev_small --model claude-haiku-5-5 --placebo --variant placebo-haiku
   uv run yi agent run --split dev_small --limit 1      # one Sonnet 5.5 case: its real cost
   uv run yi agent run --split dev_small                # the rest (resumes; 3 cases at a time)
   uv run yi agent run --split dev_small                # again: prints the scores beside BARO's
   ```

   A run resumes where it stopped (graded cases are kept, infra failures retried), and
   one results directory holds one condition: to change the model, effort or step budget,
   pass a new `--variant`. Results: `evals/results/<variant>/<split>/` (trials, scores,
   trajectories; the full transcripts in `events/`, spans in `traces/`, both git-ignored).

10. **S4 A/B runs (paid, about $2.40 and 5 minutes each on Sonnet 5.5).** One block per
    step; each prints a line per case as it finishes.

    ```bash
    uv run yi agent run --split dev --variant sonnet-repeat                          # noise floor
    uv run yi agent run --split dev --variant s4-prompt --prompt v2                  # + fault guide
    uv run yi agent run --split dev --variant s4-prompt-tools --prompt v2 --tools v2 # + error counts
    uv run yi agent compare --split dev sonnet sonnet-repeat s4-prompt s4-prompt-tools
    uv run yi eval confusion --split dev sonnet sonnet-repeat s4-prompt s4-prompt-tools
    ```

11. **The test split, once (paid: about $12.50 on Sonnet 5.5; 3-4 hours, mostly loading).**
    The 283 test cases are loaded, investigated and unloaded a few at a time
    (`--unload-after`), because loading them beside dev would pass Elasticsearch's
    1,000-shard limit. Run it inside `tmux`, so a dropped SSH connection doesn't stop it
    (`tmux new -s test`; detach with Ctrl-b then d; come back with `tmux attach -t test`).

    ```bash
    uv run yi eval baseline baro --split test       # free, a few minutes
    uv run yi agent run --split test --variant final --prompt v2 --tools v2 --unload-after --parallel 3 2>&1 | tee ~/test-run.txt
    ```

    Each case prints load progress, then its result line. A Train Ticket case can take
    two minutes to load; more than 5 minutes with no new line means something is stuck.
    Stopping is safe: rerun the same command and it resumes.

12. **OpenRCA Bank data (S5; free, about an hour, mostly the download).** 26.4 GB into
    `YI_DATA_DIR/openrca`, plus about 3-6 GB of cut cases. Run inside `tmux` (step 11).
    One block per step; send the output of each before the next.

    ```bash
    uv run yi data bank fetch 2>&1 | tee ~/bank-fetch.txt     # resumable; progress bars per file
    uv run yi data bank check 2>&1 | tee ~/bank-check.txt     # seconds; expect "0 problems"
    uv run yi data bank cut 2>&1 | tee ~/bank-cut.txt         # 10-30 min; refuses unless check passes
    uv run yi data bank inspect 2>&1 | tee ~/bank-inspect.txt # seconds
    uv run yi data bank audit --split all 2>&1 | tee ~/bank-audit.txt  # a few minutes
    uv run yi data bank load --split dev --keep 2>&1 | tee ~/bank-load.txt  # 34 cases, ~30 min
    ```

    The fetch shows a progress bar per file (a trace file is about 2.5 GB); if no bar
    moves for 5 minutes, stop it (Ctrl-C) and rerun: it resumes. The cut prints each of
    its 40 files, and every 0.5 GB within the large ones; it is all or nothing, so an
    interrupted cut is simply rerun. The load prints progress per case.

13. **The Bank grader (S5 step 2; free, a minute).** It changes the harness hash (Bank's
    split and data code are now in it), so review the diff, then approve once:

    ```bash
    git diff --stat 39fb49f -- src/yi_agent/eval src/yi_agent/data evals/   # 39fb49f: the last approval (S2)
    uv run yi data bank fetch 2>&1 | tee ~/bank-fetch2.txt     # seconds: also fetches Microsoft's archived run
    uv run yi eval bank-replay --approve-harness 2>&1 | tee ~/bank-replay.txt
    uv run yi eval bank-baseline oracle --split test 2>&1 | tee ~/bank-baselines.txt
    for b in empty wrong random; do uv run yi eval bank-baseline $b --split test 2>&1 | tee -a ~/bank-baselines.txt; done
    ```

    The replay must print `recorded scores reproduced: 136/136`.

14. **One-call baseline smoke (S4 addendum; paid, about $1 and 5-10 minutes).** Needs
    the harness approved (step 13), no Elasticsearch (it reads the case files).

    ```bash
    uv run yi agent run --split dev_small --variant one-call-smoke --mode one-call --prompt v2 --parallel 3 2>&1 | tee ~/one-call-smoke.txt
    ```

    Each case prints one result line (1 step, 1 tool call). Done: a score table and the
    BARO comparison. More than 3 minutes without a new line means a stuck call.

    Then the comparison (summary v2; about $2 and 5-10 minutes per dev run), and the
    free tables beside the agent's three dev runs:

    ```bash
    uv run yi agent run --split dev --variant one-call-dev --mode one-call --prompt v2 --parallel 3 2>&1 | tee ~/one-call-dev.txt
    uv run yi agent run --split dev --variant one-call-dev-2 --mode one-call --prompt v2 --parallel 3 2>&1 | tee ~/one-call-dev-2.txt
    uv run yi agent compare --split dev s4-prompt-tools s4-repeat s4-repeat-2 one-call-dev one-call-dev-2 2>&1 | tee ~/one-call-compare.txt
    uv run yi eval agreement --split dev one-call-dev one-call-dev-2 2>&1 | tee -a ~/one-call-compare.txt
    uv run yi eval confusion --split dev one-call-dev one-call-dev-2 2>&1 | tee -a ~/one-call-compare.txt
    ```

15. **The agent on Bank (S5 step 3; paid).** The dev queries must be loaded (step 12).
    A smoke on 3 queries first (about $0.50, 5 minutes), then all 34 dev queries:

    ```bash
    uv run yi agent bank-run --split dev --variant bank-smoke --limit 3 2>&1 | tee ~/bank-smoke.txt
    uv run yi agent bank-run --split dev --variant bank-v1 2>&1 | tee ~/bank-dev.txt
    ```

    Each query prints one line (steps, cost, what was asked, truth, answer, score). More
    than 5 minutes without a new line means a stuck call; rerunning resumes.

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
