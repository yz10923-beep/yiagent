# Roadmap

Each stage ends with something runnable and a test that proves it. **Done** means
`make verify` passes and the stage's demo works on the VM, not "the code is written".
Evals come before the agent: S2 grades a null and an oracle answer before S3 exists.

| Stage | Build | Done when |
|---|---|---|
| **S0** | Repo, kama's LLM/trace/context layers vendored, Elasticsearch in Docker, setup doc | `make verify` green; `make es-up && make es-test` green on the VM |
| **S1** | RCAEval adapter: fetch, one case schema, label blinding, frozen dev/test split, per-trial loading into Elasticsearch | A case loads into fresh indices with opaque names; a leakage test proves no label reaches what the agent can see; the split's hash is pinned in a test |
| **S2** | Eval harness: graders, baselines, selftest | Oracle scores 1.0, null scores at chance, a wrong answer fails, all inside `make verify`; the classical baseline (BARO) is scored on the dev split |
| **S3** | The agent: a loop written here, read-only telemetry tools, a structured final report | First paid dev run, compared with BARO per fault type; every run has a trace |
| **S4** | Improvements, each one an A/B: log template grouping, grounding feedback, model and effort | Each change has a measured effect (or a measured non-effect) on dev |
| **S5** | Generalize and deploy: OpenRCA Bank adapter, alert-webhook service, results dashboard | One test-split run, reported once; the same agent runs on OpenRCA Bank |

## S0 result (2026-10-11)

Done criteria met on the VM (DigitalOcean, Ubuntu 24.04, 4 vCPU, 8 GB, 150 GB free):
`make verify` green (37 unit tests), `make es-up && make es-test` green, Kibana up,
Hugging Face reachable. Then the two gaps, closed in the cloud container: the config
loader, and a pricing table rewritten from the live pricing page (per-model cache-read
rates, Haiku 5.5's long-prompt card, exact model-id matching); 54 unit tests.

Talking point: a cost number is only as good as its price table. Checking it against
the published page found the vendored table overpriced Opus 5.5 cache reads 2x (a flat
0.1x multiplier where the real rate is 0.05x) and priced unknown newer models by prefix.
In an agent loop most input tokens are cache reads, so that error lands on the biggest
line item.

## Facts about the data (checked 2026-10-11)

- RCAEval on Hugging Face (`phamquiluan/RCAEval`, MIT): one directory per case with
  `metrics.parquet`, `logs.parquet`, `traces.parquet` and `inject_time.txt`, plus a
  735-row index `cases.parquet` (root-cause service, fault, inject time, sizes).
- **The directory name is the label** (`re2tt_ts-order-service_cpu_1`). Case names
  must never reach the agent: not in prompts, index names, tool output or file paths.
- RE2: 270 cases, 6 fault types (cpu, mem, disk, delay, loss, socket), 5 services per
  system, 3 reps, balanced. RE3: 90 cases, code-level faults f1-f5, not a uniform grid
  (RE3-TT has 2 root-cause services, RE3-OB 4; reps vary 3-6; F5 only in OB).
- Sock Shop (SS) has metrics and logs but **no traces**. `re2tt_ts-auth-service_cpu_1`
  has no logs.
- One case is a 24-30 minute window: about 40-90K log lines and 35-250K spans (RE3).
- The index's `fault_description` for RE3 is only "code-level fault F1" etc. What F1-F5
  are must come from the paper before S2 defines classes.
- **There is no root-cause indicator column** in the Hugging Face copy (the paper
  mentions one). Only 8 RE3-SS cases ship a `root_cause.txt` with the root-cause log
  line. So S2 defines the expected evidence per fault type itself and says so.
- The Hugging Face download is blocked from the cloud dev container (proxy 403); fetch
  on the VM.

## S1 decisions to make

- **What the agent is told.** An alert with a time window, not `inject_time` itself
  (a real alert fires some time after the fault). Window and delay: decide, write down.
- **Split.** Stratified by dataset x system x fault, seeded. Dev small enough for cheap
  iteration (about 50 cases), test the rest, touched only in S5.
- **Load per trial or once.** Per trial (fresh indices, deleted after) mirrors kama's
  fresh workspace and keeps disk small; loading all 360 once is faster for repeat runs.
- **Metrics.** Elasticsearch too (one store, the ELK story) or DuckDB over Parquet.

## Interview talking points

(Filled in as each stage closes, with numbers.)
