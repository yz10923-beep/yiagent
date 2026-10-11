# CLAUDE.md

Guidance for Claude Code when working in this repository. It is also the handoff from
the session that started the project (2026-10-11): who Yi is, the goal, what is done,
what is not, and what to do next.

## Who I am
I'm Yi Zhou, an MSIS student at NYU (graduating December 2026), with an undergraduate
degree from the University of Washington. I'm working toward a role as an LLM application
engineer (LLM应用工程师), ideally building AI agents for finance.

## What I can already do
- Python and SQL (MySQL) are my working languages. I can read Java and C++ and make
  targeted changes with AI assistance, but I'm not proficient in either, and I don't know Go.
- Background across NLP/ML coursework, distributed systems, database systems, and
  quantitative finance.
- Summer capstone with Citi (Robothon Global Markets, an algorithmic trading competition
  platform):
  - Diagnosed a RabbitMQ capacity ceiling as per-client connection cost rather than message
    throughput, disproving the initial hypothesis with a controlled load test. Collapsing
    connections across the Python, Java and C++ bot clients roughly doubled sustainable bot
    count under a fixed connection budget.
  - Built an operator console (Flask, Chart.js) showing live order flow and order-book depth,
    derived by parsing component logs; integrated Elasticsearch, Kibana and Filebeat.
  - Found silent production failures through end-to-end verification, including a logging
    pipeline that dropped 100% of events while every service reported healthy.
- Built kama (github.com/yz10923-beep/project-1), a staged local coding-agent runtime in
  the style of Claude Code, deployed on my own Linux server: S0-S6 done and measured
  with A/B evals; S7 paused (see "Where this came from").

## Where I'm weak
- Building an agent end to end from a blank repo: architecture, tool design, memory,
  failure handling.
- Retrieval engineering: chunking, hybrid search, reranking, vector stores (e.g. pgvector).
- Evaluation and observability for LLM systems: eval sets, metrics, tracing
  (Langfuse / LangSmith).
- Async Python and production deployment patterns for LLM services.

## How I want you to help
- Treat me as a capable engineer who learns fastest by building. Prefer concrete code,
  architecture sketches and trade-offs over general explanations.
- Default to Python. Only suggest another language if a specific role or task requires it.
- Teach fundamentals that won't churn — raw LLM API mechanics, retrieval, evals,
  observability — before frameworks (LangGraph, LlamaIndex).
- Push me toward finished, deployed, measured work. If I start scattering across new
  languages or tools without finishing, say so directly.
- Be honest when my approach is weak or my understanding is wrong. Point out gaps an
  interviewer would notice.
- When relevant, connect advice to finance use cases and to how it would read on a resume
  or in an interview.

---

## This project

**Goal.** An incident-triage agent: given an alert, it investigates a system's logs,
metrics and traces in Elasticsearch and reports the root-cause service, the fault type
and the evidence (the exact log lines, spans and metrics it used). It is measured, not
demoed: a frozen eval harness grades every report against hidden labels from public
benchmarks, and compares it with the benchmarks' classical (non-LLM) baselines.

**Why this project, now.** It is the project kama existed to feed, and it covers what
kama doesn't: an agent designed end to end in a blank repo (architecture, tools, eval),
retrieval over large telemetry, and an eval with real metrics. Yi graduates in December
2026, so a finished, deployed, measured result matters more than more stages. The domain
is general microservice operations, so it applies beyond finance; the finance angle is
OpenRCA's Bank system (S5), and triaging trading or payment systems is the same problem.

**Metrics.**
- Fault type: macro-F1 (RE2: cpu, mem, disk, delay, loss, socket; RE3: f1-f5, reported
  separately).
- Root-cause service: top-1 and top-3 accuracy, plus RCAEval's own Avg@5 so numbers
  compare with the paper.
- Evidence grounding: every citation must exist in that case's data and support the
  claim. The grader checks it, never the agent.
- Trajectory: steps, tokens, dollars, wall time per case.
- The bar: RCAEval's classical baselines (BARO first) on the same split. "Does the agent
  beat the non-LLM baseline?" is the first interview question; report it either way.

**Data.** RCAEval RE2 + RE3 now (360 cases: Online Boutique, Sock Shop, Train Ticket;
MIT; Hugging Face `phamquiluan/RCAEval`). OpenRCA Bank later (S5). Facts checked about
the data, and the S1 decisions still open, are in `docs/ROADMAP.md`. The one that can
silently ruin every result: **RCAEval's case directory names are the labels**
(`re2tt_ts-order-service_cpu_1`).

Plan: `docs/ROADMAP.md` (S0-S5). Setup and the VM: `docs/SETUP.md`.

## Status (2026-10-11)

**Current stage: S1 code done in the cloud container (2026-10-11); next: S1 on the VM
(Yi runs the commands below and sends the output), then S1 phase 2 from what the real
data shows, then S2.**

Done in S0:
- Repo, `pyproject.toml` (uv, Python 3.12), Makefile, `make verify` (ruff, mypy --strict,
  pytest): 37 unit tests pass.
- Vendored from kama: LLM provider, retry, pricing, tracer, trace analyzer, context meter,
  output store, and their unit tests (`VENDORED.md`).
- `docker-compose.yml`: Elasticsearch 9.1.0 single node (+ optional Kibana), security off,
  bound to 127.0.0.1. `make es-up`, `make es-test` (stdlib smoke test: index, bulk,
  search, delete). Verified in the cloud dev container: ES started and es-test passed.
- **Checked on the VM** (DigitalOcean droplet, Ubuntu 24.04, 4 vCPU, 7.8 GiB RAM, no
  swap, 150 GB free; Docker 29.9.0, Compose v5.6.0; Yi works as root, repo at
  `/root/yiagent`): `make verify` 37 passed, `make es-up` + `make es-test` passed with the
  `ulimits` block as is, Kibana starts, Hugging Face reachable (HTTP 200). RAM is at the
  8 GB minimum: run Kibana only while using it.
- S0 gaps closed: `config.py` (kama's loader: `~/.yi/.env`, then `./.env`, then env vars;
  expands `~`). `llm/pricing.py` rewritten from the live pricing page (2026-10-11): every
  rate stored as published, because cache reads are 0.05x input on Opus 5.5 and Sonnet 5.5
  and 0.025x on Fable 5.1 (kama's flat 0.1x overpriced them); Haiku 5.5's second rate card
  over a 100K-token prompt; exact id (or id + snapshot date) matching, because prefix
  matching priced any unlisted `claude-sonnet-5-*` as Sonnet 5. 54 unit tests.
- Docs: `README.md`, `docs/SETUP.md` (what runs where, VM sizing and steps,
  troubleshooting), `docs/ROADMAP.md` (stages, data facts, open decisions).

Done in S1 so far (decisions in `docs/ROADMAP.md`, "S1 decisions"):
- `yi data fetch|check|inspect|audit|load|unload` (`src/yi_agent/cli.py`).
- Labels frozen in `evals/grader/labels.json` (360 cases, opaque ids `c` + 10 hex), the
  grouped stratified split in `evals/split.json` (dev 77, dev_small 24, test 283); both
  sha256-pinned in `tests/unit/test_split.py`.
- Telemetry reader (time unit detected per column, metrics in long form, NaN/null
  counted), loader into `yi-<id>-{logs,traces,metrics}` with every count verified and
  cleanup on failure; alert (onset = inject_time, as the baselines get).
- Leakage tests: the alert over all 360 real cases, index names, loaded documents
  (no name, no path, no root_cause.txt). 452 unit + 6 Elasticsearch tests.

Not done yet, in order:
1. **S1 on the VM (Yi):** done so far: fetch, `check` (0 problems), `inspect` (real
   schemas, in ROADMAP), `audit` (clean). Left: `yi data load --split dev_small --keep`
   timed on the VM, with disk use; then record the numbers and close S1. Loading is now
   load-once-and-keep (`ensure_loaded`, ROADMAP "S1 decisions").
2. **S2 (eval harness before the agent):** graders, oracle/null/wrong baselines and a
   selftest inside `make verify`; BARO scored on dev. Find out what RE3's F1-F5 mean
   (from the paper; the data only says "code-level fault F1"). The Hugging Face copy has
   no root-cause indicator column (only 8 RE3 Sock Shop cases ship `root_cause.txt`), so
   S2 writes its own expected-evidence rules per fault type and documents them.
3. S3 agent, S4 A/B improvements, S5 OpenRCA Bank + deploy: see ROADMAP.

## Where this came from (decisions already made; don't re-litigate)

- **kama is paused, not cut.** kama (project-1) S0-S6 are closed and measured. S7 (MCP
  client, subagents, skills) was paused after part 1/5 on 2026-10-11 to build this.
  kama's own rule: all its work goes to branch `claude/eager-ptolemy-6ta3lk` only.
- **New repo, copied modules (not built on kama-core, not importing kama).** kama's loop
  is shaped for a coding agent (workspace, approvals, bash, policy, sandbox, a fixed
  "coding agent" system prompt). This agent is a different shape: alert in, read-only
  telemetry tools, structured report out, batch-evaluated. So the domain-free layers are
  copied (from stages that are closed and stable) and the loop (~200 lines), tools and
  evals are written here. The loop is the part Yi should be able to explain line by line
  in an interview.
- **Datasets considered:** RCAEval (chosen: MIT, labelled, multi-source, has baselines).
  OpenRCA (S5: hardest, has the Bank system; ~80 GB, data licence unstated, some labels
  criticised as unrecoverable). SREGym / AIOpsLab / ITBench (live Kubernetes, includes
  mitigation; heavy; a 2026 paper reports reward hacking in AIOpsLab and ITBench). Loghub
  HDFS/BGL (anomaly flags, not incidents); Loghub-2.0 is useful only as ground truth for
  a log template parser.
- **Ideas taken from reviewing JINX (github.com/machinegpt/agent):**
  1. Log template grouping: mask volatile tokens (paths, hex ids, long numbers,
     durations) but keep short numbers like thresholds, group lines by canonical cause,
     show counts plus a few examples. It must earn its place in an S4 A/B: in kama's
     big-log-triage task the model narrowed every query itself with grep/awk and never
     neared the context budget. Component-test it on Loghub-2.0 templates.
  2. Citation grounding check: a grader (S2), not agent self-assessment.
  3. Later, as A/B experiments only: a lessons ledger with credit assignment (credit from
     graded outcomes, not self-reports), and grounded no-progress detection (same tool,
     input and error repeated N times).
  Not copied: self-graded exit criteria, structured state parsed out of free text
  (use tool calls), a fixed minimum number of rounds, a fixed sliding window instead of
  measured context management.
- **Lessons from kama's measurements worth reusing:** with prompt caching a long context
  is cheap to keep (cache reads cost 0.1x input, 0.05x on Opus 5.5 and Sonnet 5.5), so compaction costs more than it saves
  unless the context is large relative to the summary; chars/3 token estimates ran
  11-21% low on dense JSON, so calibrate from real usage; a variant is one condition,
  and infra failures are never scores.

## Working with Yi

- Yi runs everything that costs money or needs the VM (paid eval runs, the data
  download, Elasticsearch with real data). Give exact commands and say what to send
  back. Only Yi approves a harness hash; never pass an approval flag on Yi's behalf.
- The cloud dev container: Hugging Face downloads are blocked (proxy 403); Docker works
  after `sudo dockerd &`; Elastic's image registry is blocked but Docker Hub is not
  (hence the `elasticsearch:9.1.0` image); its disk looks >90% used to ES, so disable
  the disk threshold there (`docs/SETUP.md`), never on the VM. It also can't raise
  rlimits: start ES with a scratch override `services: {elasticsearch: {ulimits: !reset
  {}}}` (`docker compose -f docker-compose.yml -f <override> up -d elasticsearch`); never
  change the repo's compose file for it. The Hugging Face connector can read the
  dataset's README, file listing and `cases.parquet` rows, though downloads are blocked.
- Git: one long-lived working branch for everything (Yi's rule, 2026-10-11, as in kama):
  all work goes to branch `claude/jolly-euler-45xyyc` only. The first two commits are
  also on `main`. On the VM: `git fetch origin && git checkout claude/jolly-euler-45xyyc`.

## Commands

```bash
uv sync                  # install (Python 3.12)
make verify              # ruff lint + format check, mypy --strict, pytest (no network)
make es-up / es-down     # local Elasticsearch in Docker (127.0.0.1:9200)
make es-test             # Elasticsearch integration tests (marker `es`)
make live                # real-API tests (needs ANTHROPIC_API_KEY; costs money)
uv run yi data --help    # fetch, check, inspect, audit, load, unload (data commands run on the VM)
```

Settings (`config.load_settings()`): `~/.yi/.env`, then `./.env`, then env vars (later
wins): `ANTHROPIC_API_KEY`, `YI_DATA_DIR` (benchmark data, outside the repo), `YI_ES_URL`.
`YI_ES_HEAP` is read by docker compose only (shell or `./.env`, not `~/.yi/.env`).

## Layout

```
src/yi_agent/
  llm/        provider protocol, Anthropic provider (streamed), pricing, retry   (vendored)
  trace/      spans, tracer (ContextVar parents), analyze/render/chrome export     (vendored)
  config.py   settings: ~/.yi/.env -> ./.env -> env vars                           (from kama)
  context.py  ContextMeter: request sizes exact from usage, the tail estimated     (vendored)
  outputs.py  OutputStore and cut(): results over the cap saved whole              (vendored)
  cli.py      `yi data ...`
  data/       rcaeval (names, ids, labels, fetch), split, telemetry (case dir -> docs),
              es_load (fresh indices per case, counts verified), alert, check (VM audits)
evals/grader/labels.json  id -> label; harness and graders only, never the agent
evals/split.json          frozen dev / dev_small / test ids (hash pinned)
tests/unit/          fast, no network
tests/integration/   need Elasticsearch (marker `es`)
tests/fakes.py       ScriptedProvider etc. for loop tests (from kama)
tests/rcaeval_fixture.py  tiny synthetic cases in the real on-disk layout
docs/                ROADMAP.md, SETUP.md
scripts/wait_es.sh   readiness wait for make es-up
```

## Invariants (keep these true)

- **No label reaches the agent.** Case names encode the answer. The agent sees opaque
  case ids only; labels live where only the grader reads them; a leakage test checks
  prompts, index names and tool output.
- **Grade the outcome against hidden labels, never the agent's own claims.** Every
  grader has an oracle that passes, a null at chance and a wrong answer that fails,
  checked inside `make verify`.
- **Tune on dev, report on test.** The split is frozen (hash pinned in a test); the test
  split is run only for a stage's final report.
- **Infra failures are not scores.** API errors, timeouts and Elasticsearch failures go
  to `errors.jsonl`; a variant is one condition, and a trial that differs stops the run.
- **The agent's tools are read-only.** It queries telemetry; it never writes to it or
  runs shell commands.
- Every run writes events and a trace; the span format stays compatible with kama's.
- Unknown prices are None, never $0. Secrets never reach a trace, a log or the repo.
- Tests never touch the real home directory (autouse fixture sets HOME).
- Changes to vendored files are recorded in VENDORED.md.

## Conventions

- Python 3.12, `mypy --strict`, ruff (line length 100). `make verify` passes before commit.
- Comments and docstrings in English, short, and only where the *why* isn't obvious.
- LLM calls go through the raw Anthropic SDK behind our own `LLMProvider`; no agent
  frameworks.
- Tests use real services where cheap (Elasticsearch in Docker), fakes for the LLM
  (`tests/fakes.py`), `httpx2.MockTransport` for provider HTTP (the anthropic SDK is
  built on httpx2). Wait on readiness, never a fixed sleep.
- Secrets live in `~/.yi/.env` (git-ignored); never commit keys or benchmark data.
- Keep `docs/ROADMAP.md` current: each stage's decisions, results with numbers, and
  interview talking points when it closes. Update "Status" above at every stage change.
