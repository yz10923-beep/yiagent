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
- One case is a 24-30 minute window. RE2-OB (index rows 380-382): exactly 24 minutes,
  1441 one-second metric rows, `inject_time` = `time_start` + 720 s (the fault sits in
  the middle), about 175K log lines and 400K spans. RE3: about 40-90K log lines and
  35-250K spans. Metrics are 69-376 columns named `{service}_{metric}`.
- Some files are absent rather than empty: no `logs.parquet` for
  `re2tt_ts-auth-service_cpu_1`, and 13 RE3-SS cases had header-only CSVs in the source.
  A missing file means "source not available", never an error.
- **RCAEval's baselines are given the injection time.** `main.py` reads `inject_time.txt`
  (plus `--tdelta`, default 0) and passes it to every method, and cuts the window as
  `length/2` before and after it. Perfect anomaly detection is the benchmark's protocol.
- The index's `fault_description` for RE3 is only "code-level fault F1" etc. What F1-F5
  are must come from the paper before S2 defines classes.
- **There is no root-cause indicator column** in the Hugging Face copy (the paper
  mentions one). Only 8 RE3-SS cases ship a `root_cause.txt` with the root-cause log
  line. So S2 defines the expected evidence per fault type itself and says so.
- The Hugging Face download is blocked from the cloud dev container (proxy 403); fetch
  on the VM.

## S1 decisions (made 2026-10-11)

- **What the agent is told: the onset time, as the baselines are.** The alert gives the
  case's opaque id, the system's name, which sources exist, the telemetry window and the
  anomaly onset = `inject_time`. That is exactly what BARO gets, so "does the agent beat
  BARO" compares like with like. A late, noisy alert (onset + a seeded delay) is a harder
  condition for S4, the analogue of RCAEval's `--tdelta`, not the default. The alert names
  no service and no symptom: a symptom ("frontend p99 high") would have to be derived
  from the data and could point at the answer.
- **Opaque ids:** `c` + the first 10 hex of sha256(salt + case name). Deterministic, so
  the split and the labels are reproducible; not secret against someone holding the repo,
  which the agent never does (its tools are read-only telemetry queries).
- **Labels and split are committed** under `evals/`: `evals/grader/labels.json` (id ->
  case name, suite, system, service, fault, rep; read by the harness and graders only)
  and `evals/split.json` (ids only, sha256 pinned in a test). On the VM, `yi data check`
  verifies that the download agrees with them: same case names, and `cases.parquet`'s
  `root_cause_service` and `fault` equal to what the names say.
- **Split: grouped and stratified, seeded (20261011).** A group is (suite, system,
  service, fault); all its repetitions go to the same side, because two reps of the same
  injection are near-duplicates and tuning on one while testing on another overstates
  the result. RE2: one of the 5 groups of every (system, fault) goes to dev (18 groups,
  54 cases). RE3 (not a grid): 2 groups per system with distinct faults and distinct
  root-cause services, from faults with at least 2 groups so every fault keeps a test
  group (a first draw put both RE3-TT dev groups on ts-route-service; dev would have
  taught "the answer is always ts-route-service"). Result: **dev 77, dev_small 24, test
  283** cases (sha256 `4d25c2e8...`). dev_small (one rep per dev group) is for cheap
  iteration; full dev confirms; test is run once.
- **Load per trial.** Each trial loads its case into fresh indices `yi-<id>-{logs,traces,
  metrics}` and deletes them after. It keeps disk small (one case is about 1M documents),
  matches kama's fresh workspace, and means the agent's queries can only ever reach one
  case. A `--keep` option exists for iterating on one case. Loading checks every count:
  documents indexed must equal source rows, or the load fails (a silent drop would score
  as an agent failure).
- **Metrics in Elasticsearch too**, in long form (`@timestamp`, `metric`, `value`, one
  document per non-null cell). One store, one query language for the tools, and the ELK
  story; per-case metric volume (at most about 540K cells for Train Ticket) is small.
  `metric` keeps the column name whole: splitting `{service}_{metric}` safely needs the
  real column names, which S1 inspects on the VM first.
- **Timestamps are normalized to epoch milliseconds** in `@timestamp`, the unit detected
  per column from the values (s, ms, us or ns by magnitude; ISO strings parsed), and the
  detected unit is reported, because the README does not say what unit logs and traces
  use.
- **Never loaded:** `root_cause.txt` (it is the answer), file paths, the case name.
- **Decimals are mapped as `double`**, strings as `keyword` (log `message` also as
  `text`). Elasticsearch's default maps JSON decimals to 32-bit `float`, which would
  silently round large durations or times stored as doubles.

## S1 status

Built and tested in the cloud container (no data there): 452 unit tests and 6
Elasticsearch tests on synthetic cases in the real layout. Open until the VM run:
`yi data check` agreeing with the frozen labels, `inspect` on the real schemas, the
leak `audit` over all 360 cases, and timed loads of dev_small.

## Interview talking points

(Filled in as each stage closes, with numbers.)
