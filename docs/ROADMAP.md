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
- **Real schemas** (`yi data inspect` on the VM, 2026-10-11, one case per suite x system):
  logs are `timestamp` (int64, **seconds**: many lines share one value; messages carry
  their own ms times), `container_name`, `message`. Traces (OB, TT) are `time` (a string
  'HH:MM', useless), `traceID`, `spanID`, `serviceName`, `methodName`, `operationName`,
  `parentSpanID`, `startTimeMillis`, `startTime` (int64 **us**), `duration` (int64 us),
  `statusCode` (null in TT, gRPC code 0 in OB). Metrics: `time` (s) plus
  `{service}_{metric}` columns, `metric` in cpu, mem, latency-50, latency-90, ..., with
  databases and queues as services too (`ts-auth-mongo`, `redis`, `rabbitmq`); TT has
  316-360 columns, OB about 70-75, SS about 77-82.
- **Sizes are larger than first assumed:** one RE2-TT case is 274K log lines, 898K spans
  and 1441 x 360 metric cells, about 1.7M documents. RE2 windows are 24 min with the
  onset at +720 s; RE3-TT's is 30 min with the onset at +900 s.
- **`yi data check` (VM): 360 cases, 0 problems.** Every directory exists and
  `cases.parquet` agrees with every case name (the name is the label, confirmed).
- **Leak audit over all 360 cases (VM): no injector or label traces.** No `chaos`, no
  `stress-ng`, no network-injector tool, no case name in any log line, span field or
  metric name. Its only hits were false positives ("fault" in `DefaultConnectionPool`,
  `javax.inject`, a loose pattern inside a JWT), now excluded and tested.
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
- **Load once, keep, verify** (revised 2026-10-11 from "load per trial", after measuring).
  A Train Ticket-sized case (1.7M documents) loads in 71 s with one bulk stream and
  41 s with 3 parallel bulk threads and an async translog during the load (4-core cloud
  container; expect slower on the VM), and takes about 140 MB on disk. Reloading it for
  every trial would cost more than the trial; keeping all of dev costs 10-25 GB of 150.
  So each case is loaded once into `yi-<id>-{logs,traces,metrics}`; each index records
  `_meta` {loader version, complete, docs}, written last, and `ensure_loaded` reuses an
  index only if the marker, the version and the live count all match, otherwise it
  reloads. Isolation between cases comes from binding the agent's tools to one case id
  (S3), not from deleting indices. Every load still checks that documents indexed equal
  documents produced from the source rows (a silent drop would score as an agent
  failure).
- **Metrics in Elasticsearch too**, in long form (`@timestamp`, `metric`, `value`, one
  document per non-null cell). One store, one query language for the tools, and the ELK
  story; per-case metric volume (at most about 540K cells for Train Ticket) is small.
  `metric` keeps the column name whole: splitting `{service}_{metric}` safely needs the
  real column names, which S1 inspects on the VM first.
- **The time column is chosen from the values, not the name** (first VM run,
  2026-10-11): in `re2tt_ts-travel-service_disk_1` the logs' `timestamp` is a string
  holding a time of day (`'12:02'`), not the int64 epoch the README promises. Per source,
  candidates are tried in order (logs: `timestamp`, `time`, `@timestamp`, `ts`; traces:
  `time`, `startTime`, `timestamp`) and a typed column that reads as an epoch wins over a
  string; no usable column is an error naming every candidate and its values.
- **Timestamps are normalized to epoch milliseconds** in `@timestamp`, the unit detected
  per column from the values (s, ms, us or ns by magnitude; ISO strings parsed), and the
  detected unit is reported, because the README does not say what unit logs and traces
  use.
- **Never loaded:** `root_cause.txt` (it is the answer), file paths, the case name.
- **Decimals are mapped as `double`**, strings as `keyword` (log `message` also as
  `text`). Elasticsearch's default maps JSON decimals to 32-bit `float`, which would
  silently round large durations or times stored as doubles.

## Ideas for S2 from S1

- **A metadata-only baseline as a leak detector.** The metric column set differs by case
  (OB 69-77 columns). If a model given only the column names, row counts and file sizes
  beats chance on the root cause, the data's shape leaks the label and every score needs
  that baseline next to it.

## S1 status

First VM run (2026-10-11): `check` 0 problems, `inspect` gave the real schemas (the
traces' 'HH:MM' `time` crashed the first reader; the time column is now chosen from the
values), `audit` clean. The dev_small load looked stuck: it printed nothing until a case
finished, and a Train Ticket case is 1.7M documents; it now prints progress and loads
about 1.9x faster. Open: timed loads of dev_small on the VM, with disk use.

## Interview talking points

(Filled in as each stage closes, with numbers.)
