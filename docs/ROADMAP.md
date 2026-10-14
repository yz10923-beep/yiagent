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
| **S5** | Generalize and deploy: OpenRCA Bank adapter, alert-webhook service, results dashboard | The same agent runs on OpenRCA Bank, measured against its baselines (RCAEval's test split was run once, at the end of S4) |

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

## Facts from the paper (RCAEval, WWW 2025, arXiv 2412.17015; checked 2026-10-11)

- **Faults.** Resource faults are injected with stress-ng into the container: CPU hog
  (cpu), memory leak (mem), disk stress (disk), **socket stress (socket)**. Network faults
  use tc: delay variation (delay), packet drop (loss). RE3's code-level faults are bugs
  planted in a service's source: **f1 incorrect parameter values, f2 missing parameters,
  f3 missing function call, f4 incorrect return values, f5 missing exception handler.**
- **Root-cause indicators** (what counts as the evidence): for a resource fault, the
  container's resource-usage metric; for delay, the latency metric; for loss, failed
  requests and/or error codes in traces; for code-level faults, stack traces in the
  root-cause service's logs, else its error logs or error response codes.
- **AC@k** = mean over cases of (true root causes in the top k) / min(k, |true set|); with
  one root cause, 1 if it is in the top k. **Avg@k** = mean of AC@1..AC@k.
- **BARO as RCAEval runs it** (`main.py --method baro`, `RCAEval/e2e/baro.py`): metrics only;
  drop `_latency-50` columns; for Train Ticket keep only `ts-` columns; inf to NaN,
  forward-fill, then 0; 10 min before and 10 min after `inject_time` (`--length 20`);
  drop constant columns in each half; per metric, RobustScaler fit on the before-half
  and score = max robust z over the after-half; rank metrics by score; services are
  the metric prefix before the first `_` with `-db` removed, first occurrence kept.
  **Published reference, RE2-TT, all 90 cases, Avg@5: cpu 0.72, mem 0.99, disk 1.0,
  socket 0.83, delay 0.63, loss 0.64** (RCAEval README).
- RCAEval's chance level for a case with n candidate services is AC@k = min(k, n)/n,
  with the candidates counted after the same preprocessing (`--report-chance`).

## S2 decisions (made 2026-10-11)

- **The report the agent (and every baseline) returns** is one typed object: a ranked
  list of root-cause services (graded on the top 5), a fault type from the suite's
  vocabulary, and evidence as typed citations: a log line (container, time in ms, a
  verbatim excerpt of at least 10 characters), a span (its `spanID`), or a metric (its
  name and the direction it moved after the onset: up or down). Typed citations make
  grounding checkable by code; free-text evidence would need a judge.
- **Graders read the source Parquet, not Elasticsearch,** so a loader bug can't make a
  citation look grounded.
- **Root cause:** AC@1, AC@3, Avg@5 exactly as RCAEval defines them, service names
  normalized the way RCAEval does (`-db` removed), so numbers compare with the paper.
  Chance per case is min(k, n)/n over the candidate services RCAEval would count.
- **Fault type:** macro-F1 per suite (RE2's 6 classes, RE3's 5 classes; never pooled);
  a missing or out-of-vocabulary answer is wrong, not skipped.
- **Grounding** (graded by code, never by the agent): *exists* (the cited line, span or
  metric is in this case's data), *holds* (a metric moved in the stated direction: max
  or min robust z after the onset beyond 3, BARO's statistic; a log line or span is
  after the onset), and *on target* (the cited item is about the true root-cause
  service). Plus **expected evidence**: did the report cite the paper's root-cause
  indicator for the true fault type (rules in `eval/evidence.py`, coverage checked on the
  real data with `yi eval evidence-coverage`).
- **Selftest inside `make verify`:** an oracle report scores 1.0 everywhere; random
  rankings over the candidates land at chance; an empty report scores 0; a confidently
  wrong report scores 0; fabricated citations fail grounding.
- **Baselines:** BARO (our numpy port of RCAEval's code, verified against the published
  RE2-TT numbers above), random, majority fault, and a *BARO fault heuristic* (the fault
  family of BARO's top metric: cpu, mem, latency -> delay), so the agent's fault-type
  score has a non-LLM floor too.
- **Exception to "test only at the end":** a baseline with no tunable parameters (BARO,
  random) may be run on all cases, to check the implementation against published numbers.
  The agent never is.
- **Harness approval as in kama:** `evals/harness.sha256` holds the approved hash of the
  graders, baselines, labels and split; `yi eval score` refuses to score if the hash
  changed, until Yi re-runs it with `--approve-harness`. Every scores file records the
  hash it was graded under.

## S2 status

Built and tested in the cloud container (2026-10-11), on synthetic cases with the answer
planted where the paper says to look: oracle 1.00 everywhere; random at chance (300
seeds x 9 cases); empty and confidently wrong reports 0; fabricated log lines, spans and
metrics fail *exists*; a stationary metric cited as "up" fails *holds*; a real metric of
another service is off target; a run missing a case, duplicating one, or carrying one
from outside the split is invalid; infra errors are counted, not scored; changing any
hashed file stops scoring until re-approved. On fake data under the real dev_small
names the whole `yi eval` pipeline runs (oracle 1.00, random Avg@5 0.49 against a chance
of 0.50). Open until the VM run: `reproduce-baro` against the published numbers, the
real metric families and indicator coverage, and the dev scores of BARO, random and the
leak detectors.

### First VM run (2026-10-12)

Yi approved harness `6ad6f3dc49fc...` (= commit d361773) by scoring the oracle with
`--approve-harness`. Dev, 77 cases:

| baseline | AC@1 (95% CI) | AC@3 | Avg@5 | F1 re2 | F1 re3 | evidence on target | expected evidence |
|---|---|---|---|---|---|---|---|
| oracle | 1.00 (0.95-1.00) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| random | 0.08 (0.04-0.16) | 0.21 | 0.22 | 0.07 | 0.20 | - | 0.00 |
| BARO | 0.17 (0.10-0.27) | 0.79 | 0.66 | 0.18 | 0.00 | 0.17 | 0.12 |
| shape-time (leak detector) | **0.90** (0.81-0.95) | 0.94 | 0.94 | - | - | - | - |
| shape-columns (leak detector) | **0.68** (0.56-0.77) | 0.90 | 0.87 | - | - | - | - |

Chance Avg@5 over dev is 0.18 (Train Ticket 0.04: about 60 candidate services). Metric
families on the VM: cpu, mem, diskio, socket, workload, error, latency-50, latency-90
(error is missing in a few SS and TT cases). The paper's indicator exists and holds in
74 of 77 dev cases (missing: 2 re2-ob delay, 1 re2-ob loss: the root cause's latency did
not move by a robust z of 3).

**Finding 1 (resolved below): BARO did not reproduce at first.** RE2-TT, all 90 cases, Avg@5 ours vs
published: cpu 0.56 vs 0.72, mem 0.87 vs 0.99, disk 1.00 vs 1.00, socket 0.53 vs 0.83,
delay 0.49 vs 0.63, loss 0.45 vs 0.64. The port follows RCAEval's code step by step, so
the leading suspect is the input: `main.py` reads each case's `data.csv` from the Zenodo
zips, while the Hugging Face Parquet is a conversion of `metrics.json`. Our columns
include `workload` and `error` families, which jump in many services during any
incident and can outrank the root cause. Being checked with `scripts/zenodo_peek.py`
(reads one `data.csv` out of the zip by HTTP range requests) and `reproduce-baro
--show-misses` / `--exclude`. Until it reproduces, BARO's dev numbers are provisional.

Diagnosis (VM, 2026-10-12):
- **A port bug: memory units.** `--show-misses` showed nearly every miss led by a `_mem`
  metric scoring 4096, 8192, 135168: exact multiples of a 4 KB page. A memory series
  flat before the onset has an interquartile range of 0, the scaler's scale becomes 1,
  and the score is the raw change in bytes. RCAEval converts memory to MB first
  (`convert_mem_mb`), which the port skipped on the reasoning that robust z is unit-free:
  true, except exactly in the zero-spread case. Fixed in `prepare_columns`, so grounding
  (a "mem went up" citation) uses MB too; regression test in `tests/unit/test_eval.py`.
- **The input was not it.** The Zenodo RE2 cases have no `data.csv`, so `main.py` reads
  `simple_metrics.csv` (6 MB; the full `metrics.csv` is 27 MB). We suspected the Parquet
  was the full metrics, but `zenodo_peek.py peek RE2-TT ts-order-service_cpu 1` showed the
  Hugging Face Parquet *is* `simple_metrics.csv`: the same 358 columns (cpu, mem, diskio,
  socket, workload, latency-50, latency-90) and 17,497 of 17,497 compared values equal.
- **Result: BARO reproduces.** With the memory fix, on our Parquet, RE2-TT, all 90 cases,
  Avg@5 ours vs published: cpu 0.72 vs 0.72, mem 0.99 vs 0.99, disk 1.00 vs 1.00, socket
  0.83 vs 0.83, delay 0.63 vs 0.63, loss 0.64 vs 0.64. Six of six equal to two decimals,
  so the port is faithful and one bug was the whole gap. The CSV route (`zenodo_peek.py
  extract`, `reproduce-baro --metrics-file`) was not needed; it stays for other datasets.
  (Its first VM run hung silently on Zenodo; it now times out reads after 30 s, retries
  visibly, and writes a case only once both files are in.)
- Interview point: the bug was found by reading BARO's misses, not by re-reading the code.
  The scores (4096, 8192, 135168) were multiples of a memory page, which pointed straight
  at units. "Scale-free" arguments break exactly at their degenerate cases.

**Finding 2: the recording order leaks the label.** Predicting a case's root cause as the
service of the nearest *other* injection in time is right 90% of the time on dev (chance
18% Avg@5): the authors recorded the injections into one service back to back. The
metric column set leaks too (68%), largely through the same deployment epochs. A
per-case agent cannot exploit either: it sees one case and no other case's label. But
anything that carries information across cases can: a lessons ledger (S4), few-shot
examples, fine-tuning on dev. **Rule from here: nothing that crosses cases may carry
absolute times, dates or case ids, and any such feature is re-checked with the
shape-time detector run through it.** Shifting every case to a common epoch would not
close the channel, since log messages print their own dates.

### Re-approved run (2026-10-12): S2 closed

Yi reviewed the diff since d361773 (the memory fix, the missing-input check) and
approved harness `3dae88a0b1ad...` (= commit 7474237, the same hash as in the cloud
container) by scoring the oracle with `--approve-harness`. Dev, 77 cases:

| baseline | AC@1 (95% CI) | AC@3 | Avg@5 | F1 re2 | F1 re3 | evidence on target | expected evidence |
|---|---|---|---|---|---|---|---|
| oracle | 1.00 (0.95-1.00) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| random | 0.08 (0.04-0.16) | 0.21 | 0.22 | 0.07 | 0.20 | - | 0.00 |
| **BARO (the bar)** | **0.25 (0.16-0.35)** | **0.87** | **0.74** | 0.22 | 0.00 | 0.25 | 0.14 |
| shape-time (leak detector) | 0.90 (0.81-0.95) | 0.94 | 0.94 | - | - | - | - |
| shape-columns (leak detector) | 0.68 (0.56-0.77) | 0.90 | 0.87 | - | - | - | - |

BARO moved from the provisional 0.17 / 0.79 / 0.66 with the memory fix; oracle, random,
the leak detectors and coverage (74/77) are unchanged, as expected.

BARO per fault on dev (AC@1 / Avg@5): cpu 0.11 / 0.76, delay 0.00 / 0.33, disk 0.33 /
0.87, loss 0.22 / 0.76, mem 0.78 / 0.93, socket 0.11 / 0.67; RE3 f1 0.25 / 0.85, f3 0.44
/ 0.89, f4 0.00 / 0.71, f5 0.00 / 0.53. Per system AC@1: OB 0.17, SS 0.06, TT 0.56 (RE2).

What this says about where the agent can win (S3's hypotheses, not results):
- **Top-1, not top-3.** BARO has the root cause in its top 3 in 87% of cases but first
  in 25%: a caller or a neighbour on the same node moves more. Separating cause from
  symptom among a few candidates is what an investigation is for.
- **Delay and loss.** BARO's worst faults (delay Avg@5 0.33): latency propagates up the
  call graph, so every caller's latency rises too. Traces separate a span's own time from
  its children's; metrics cannot.
- **Fault type.** BARO names no RE3 fault (F1 0.00: f1-f5 are code faults visible only in
  logs and error spans) and few RE2 ones (0.22).
- **Evidence.** BARO's citations all exist and hold (its metrics really moved) but only
  25% are on the root cause: "something moved" is not "the cause moved". The grader
  scores the agent on the same three checks.
- **Hard to beat:** mem (AC@1 0.78, Avg@5 0.93) and disk (Avg@5 0.87).

## S3 decisions (made 2026-10-12)

- **The loop is ours, ~200 lines** (`agent/loop.py`): the Messages API's own message
  format, appended to and never edited, so every step extends the cached prefix and
  thinking blocks go back exactly as they came. It ends on a valid `submit_report` call
  (the S2 `Report`, validated; an invalid one goes back to the model as a tool error), a
  refusal, a second turn without a tool call (one nudge first), or the step budget (25
  model calls by default; warned with 2 left). No framework, no tool runner: the stop
  rules and failure classes are the part to explain.
- **Failure classes.** API errors are retried with backoff (`llm/retry.py`); past the
  policy, and any Elasticsearch failure, the trial is an infra error (never scored, retried
  on resume). A bad tool call (unknown metric, bad argument) is the model's to fix: it
  sees the message. A run that ends without a report is the agent's failure and scores
  as an empty report.
- **Seven read-only tools plus submit** (`agent/tools.py`), bound to one case id; the
  client wrapper only searches. `overview`; `metric_changes` (each metric scored as BARO
  scores it, memory in MB, approximate percentiles from Elasticsearch: the same signal
  the baseline has, so a gain must come from the investigation); `metric_series`;
  `search_logs` (lines grouped by exact text with their count before the onset; full
  text plus a substring match, since `java.lang.NullPointerException` is one token to
  the analyzer); `trace_stats` (p50/p90 and errors per operation, before -> after);
  `find_spans`; `trace_tree` (own time per span = duration minus children's: where a slow
  request spent its time). Outputs are text tables capped at 8,000 characters, and print
  every citable value exactly as the grader looks it up.
- **The prompt** (`agent/prompt.py`) is identical for every case and names no service,
  case or suite (tested); it describes the 11 fault types as the paper injects them. The
  agent is not told the suite (RE2 or RE3), so it chooses among all 11; BARO's fault
  guess is given the suite. The case enters only through the alert.
- **Default model `claude-sonnet-5-5` at effort high** (Yi, 2026-10-12; first planned as
  Opus 5.5). Sonnet 5.5 is exactly half Opus 5.5's price on every rate (input, cache
  write, cache read, output; pricing page checked 2026-10-12), and nothing S3 or S4 must
  prove depends on the strongest model: "agent vs BARO" is a claim about the agent *on
  model M*, and an S4 A/B compares two variants on the same model. What the cheaper
  model buys is statistical power: dev_small's 24 cases give AC@1 a 95% interval about
  +-0.2 wide, so real A/B effects need the 77-case dev split, affordable on Sonnet. The
  model is measured, not assumed: one Opus 5.5 variant on the same split is S4's model
  A/B ("does Opus buy enough accuracy for 2x the cost?"). Haiku 5.5 (1/20 of Sonnet)
  runs the plumbing on real data first; its scores are not results. Refusal fallback on
  (`fallbacks: "default"`, Opus 5.5 and Sonnet 5.5; a fallback's tokens are priced at the
  model that served them). Model, effort and step budget are flags; each combination is
  its own variant.
- **One condition per results directory** (`run.json`: model, effort, step budget,
  prompt and tool-table hashes, loop version); a mismatch stops the run. The harness hash
  is unchanged by S3 (the agent lives outside the hashed files), and an unapproved
  harness stops a run before it spends anything.
- **Recorded per case:** trajectory (outcome, steps, tool calls and errors, tokens,
  dollars, peak request size, serving models, wall time), the full transcript, spans.

## S3 status

Built and tested in the cloud container (2026-10-12): 14 loop tests with a scripted
model (ends, nudge, budget warning, retries, infra classes, append-only history, the
prompt names no service); 9 Elasticsearch tests on planted cases: the planted metric is
ranked first, a delay's own time lands on the root cause in `trace_tree`, code faults
show in logs and error spans, a citation copied from tool output passes the grader's
exists/holds checks, tools touch nothing but `search`, outputs carry no case name, path
or secret, and a scripted investigator through the real runner over all 24 dev_small
names scores, resumes, and refuses a changed condition.

First VM runs (2026-10-12), dev_small (24 cases):
- The first Haiku 5.5 run crashed on a log line with no message (a tool bug: fixed, and
  any crashing tool is now an infra failure for that case instead of the end of the run).
- Haiku 5.5, two full runs: AC@1 **0.92** and **1.00**, Avg@5 0.95 / 1.00, 7 steps and 15
  tool calls per case, **$0.005 per case** ($0.13 for all 24), 26 s per case, peak request
  54K tokens. Citations exist 0.99-1.00, hold 0.95-0.97, on target 0.79-0.81. Fault-type
  macro-F1 is weak: RE2 0.35, RE3 0.00 (socket and disk called cpu; code faults called
  cpu, mem or the wrong f-number).
- Sonnet 5.5, one case: 2 steps, $0.023, right (a 5.85e5-z disk jump; its summary says it
  checked nothing else).
- My cost estimate was 10-20x too high (I assumed long investigations at high effort).

**Not a result yet.** Top-1 0.92-1.00 against BARO's 0.25 on dev is too large a jump to
accept without controls, run before any paid Sonnet run:
1. **BARO on dev_small**, the same 24 cases (dev's 0.25 is another set).
2. **policy:metric-top**, free: the services of `metric_changes`' rows in order, no model.
   The tool is BARO's statistic computed from Elasticsearch, where missing cells are
   absent rather than zero-filled and a drop ranks like a rise. If this control scores
   near the agent, the gain is the tool's, not the investigation's; that is still worth
   knowing, and reporting.
3. **Placebo** (`--placebo`): the alert says the anomaly began 6 minutes before the real
   injection, and every query is cut off at the real one, so the agent sees only
   pre-fault data. It should land near chance (Avg@5 0.19 here). Above chance means
   something other than the fault points at the answer: a leak inside the case's data.

Controls (VM, 2026-10-12), dev_small, 24 cases:

| run | AC@1 (95% CI) | AC@3 | Avg@5 | F1 re2 | F1 re3 | on target | $/case |
|---|---|---|---|---|---|---|---|
| BARO | 0.17 (0.07-0.36) | 0.79 | 0.67 | 0.19 | 0.00 | 0.17 | 0 |
| policy:metric-top (the tool, no model) | 0.21 (0.09-0.40) | 0.83 | 0.69 | 0.19 | 0.00 | 0.21 | 0 |
| Haiku 5.5, placebo (pre-fault data only) | 0.21 (0.09-0.40) | 0.38 | 0.33 | 0.05 | 0.00 | 0.20 | 0.014 |
| Haiku 5.5, run 1 | 0.92 (0.74-0.98) | 0.96 | 0.95 | 0.35 | 0.00 | 0.79 | 0.005 |
| Haiku 5.5, run 2 | 1.00 (0.86-1.00) | 1.00 | 1.00 | 0.36 | 0.00 | 0.81 | 0.006 |

Chance Avg@5 is 0.19. Reading:
- **The tool is not the gain.** Its ranking alone scores like BARO (0.21 vs 0.17 top-1;
  0.83 vs 0.79 top-3): the same statistic, and dropping BARO's zero-fill changes little.
  Its first answer is usually shared infrastructure whose metrics barely move but were
  flat before (redis in Online Boutique, session-db in Sock Shop), with the root cause
  second (Avg@5 0.8 in those cases). Choosing among the top few is what the model adds,
  as S2's numbers predicted (BARO top-3 0.87, top-1 0.25 on dev).
- **No leak found by the placebo.** Without the fault's data the agent falls to Avg@5
  0.33, and works harder for it (15 steps, 35 tool calls, citations that hold only 27%
  of the time). What it keeps above chance comes almost all from Sock Shop (Avg@5 0.87
  against a uniform 0.27): it names carts, orders and payment, the business services
  faults are injected into, without evidence. That is a prior, not a leak, and it means
  uniform chance understates the null in a small system: **the placebo is the honest
  floor**, per system.
- *Correction, from dev below:* the "Sock Shop prior" was noise from 6 cases. On dev's
  18 Sock Shop cases the placebo is exactly at chance (Avg@5 0.27).
- dev loaded (2026-10-12): all 77 cases complete and verified (52.3M documents, 206
  indices, cluster green), 53 of them in about 40 minutes. The load looked frozen only
  because its output went through `| tail -3`, which prints nothing until the end.
- **Still to show:** the full dev split (77 cases; dev_small's intervals are wide),
  Sonnet 5.5, and the fault type, which no method gets (RE2 F1 0.35, RE3 0.00).

### S3 result: dev, 77 cases (VM, 2026-10-12). S3 closed.

| run | AC@1 (95% CI) | AC@3 | Avg@5 | F1 re2 | F1 re3 | on target | expected evidence | $/case | steps |
|---|---|---|---|---|---|---|---|---|---|
| BARO | 0.25 (0.16-0.35) | 0.87 | 0.74 | 0.22 | 0.00 | 0.25 | 0.14 | 0 | - |
| policy:metric-top (tool, no model) | 0.31 (0.22-0.42) | 0.91 | 0.78 | 0.22 | 0.00 | 0.31 | 0.19 | 0 | 2.0 |
| Haiku 5.5, placebo | 0.03 (0.01-0.09) | 0.14 | 0.11 | 0.18 | 0.00 | 0.04 | 0.01 | 0.014 | 14.9 |
| **Haiku 5.5** | **0.99 (0.93-1.00)** | 1.00 | 1.00 | 0.44 | 0.00 | 0.85 | 0.70 | 0.0054 | 6.7 |
| **Sonnet 5.5** | **1.00 (0.95-1.00)** | 1.00 | 1.00 | 0.46 | 0.00 | 0.90 | 0.65 | 0.031 | 3.7 |

Chance Avg@5 0.18. Per system top-1, BARO / tool / Haiku / Sonnet: Online Boutique 0.17 /
0.22 / 1.00 / 1.00, Sock Shop 0.06 / 0.17 / 1.00 / 1.00, Train Ticket 0.56 / 0.67 /
0.94 / 1.00. The four runs cost $3.88 in all (Sonnet $2.40, 10 s per case). Haiku's one
miss named the root cause's own database first (ts-order-mongo for ts-order-service).

What it shows:
- **Root-cause localization on RCAEval RE2/RE3 is solved by an LLM agent, and it is the
  model's doing:** the same tool output read by a fixed rule scores 0.31; read by Haiku,
  0.99. The placebo (only pre-fault data) falls to 0.03, at or below chance in every
  system, so no leak in the case's data carries the answer.
- **So top-1 no longer separates variants here; fault type and evidence still do.** S4
  measures those. Fault-type confusions are systematic, not noise (Sonnet, dev):
  socket -> cpu 9/9 (a socket stressor burns CPU, and the agent reads the larger move);
  the RE3 code faults -> mem 17/23 and f-numbers swapped (deploying the faulty code
  restarts the service, so memory moves at the onset); loss -> delay or mem 7/9. RE3
  F1 is 0.00 for every method: no run names an f-number correctly.
- Sonnet is more decisive (3.7 steps against Haiku's 6.7) and cites slightly better on
  target (0.90 vs 0.85), at 6x the price. Both are cheap enough to run every dev A/B.
- The test split stays untouched: it is run once, for the final agent, after S4.

## Facts about OpenRCA Bank (checked 2026-10-13, before any download)

From the file listing and the small files of the Hugging Face mirror
`cdreetz/OpenRCA` (the official copy is a Google Drive folder, unreachable from the
cloud container), and Microsoft's README:
- **Bank is 26.4 GB of CSV, not 80.** The ~70-80 GB is all three systems. Bank is 10 days
  (2021-03-04 to 03-25), each with `trace/trace_span.csv` (21.9 GB in all, ~146M spans:
  83% of the bytes), `log/log_service.csv` (3.6 GB, ~10M lines; many are JVM GC lines),
  `metric/metric_container.csv` (0.84 GB, ~15M rows, long form: timestamp, cmdb_id,
  kpi_name, value) and `metric/metric_app.csv` (6 MB). Row counts are estimated from
  sampled line lengths.
- **~136 faults** in `record.csv` (level, component, timestamp, datetime, reason), on 14
  components (Tomcat01-04, MG01-02, IG01-02, apache01-02, Mysql01-02, Redis01-02) and 8
  reasons (high CPU, high memory, high JVM CPU load, JVM OOM heap, disk I/O read, disk
  space, network latency, network packet loss). About 15 per recorded day.
- **~136 queries** in `query.csv`: each a 30-minute window in natural language, of 7
  task types (time only; reason only; component only; and their combinations), graded by
  `scoring_points` (component exact, reason exact, time within 1 minute). About 17 windows
  hold two failures. So the report needs an occurrence time and possibly two causes, and
  the grader is OpenRCA's, not ours: an adapter decision for S5.
- **Windows, not days.** A 30-minute window plus a 30-minute baseline is 1/24 of a day:
  roughly 0.8M documents a case (mostly spans), ~110M over all cases loaded one at a time
  with `--unload-after` (~2 h at the VM's 15K docs/s), or ~170M to load the ten days
  whole once (~3 h, with every query then bounded to its window, as the placebo's
  ceiling already is).
- **Licence:** OpenRCA's code is MIT; the README states no licence for the data, and the
  mirror has no dataset card. Use it for private evaluation, cite the paper, never
  redistribute it.
- **Known criticism:** some labels may not be recoverable from the data (one Bank case's
  memory is ~98% before, during and after the injection). Report per-task-type scores and
  look at failures case by case before trusting an aggregate.
- Model cost estimate: ~136 cases x $0.04-0.08 (more steps on a harder benchmark) =
  ~$6-11 a run.

## S5 decisions (made 2026-10-13; Yi: Bank yes, data for private evaluation only)

- **A case is one query** of `Bank/query.csv` (136), graded as OpenRCA grades it: its
  `scoring_points`, kept verbatim, read by a port of OpenRCA's `main/evaluate.py` (MIT;
  step 2). A query scores the share of its points matched (time within 1 minute,
  component and reason exact, best pairing over the failures); strict accuracy counts
  queries with every point. The prediction must name as many failures as the query has,
  or it scores 0, as in OpenRCA.
- **Labels**: per query its window, task, what is asked, the scoring points, and the
  `record.csv` rows inside the window (the full truth, for diagnosis). Built from the two
  small files, which match on all 136 queries (every window holds exactly as many
  records as it has failures, all 136 records used; scoring points are not always in
  time order, so they are compared as sets). **Only their hash is in the repo**: on the
  VM, `yi data bank check` verifies both files' sha256 and every telemetry file's size,
  builds the labels, checks their pinned hash and that the committed split is reproduced
  from them, and writes them next to the download (`openrca/bank-labels.json`).
- **Split** (`evals/split_bank.json`, pinned): grouped by window (9 windows are asked
  twice; both queries stay together), stratified by the failure's reason with the
  two-failure windows as their own stratum, a quarter of each stratum to dev. **Dev 34
  queries, test 102**; both hold all 8 reasons and some two-failure windows.
- **The cut:** each query's window plus the 30 minutes before it (a baseline; it may
  hold an earlier failure, as an on-call engineer's view would), never anything after
  the window, written as an RCAEval-shaped case (`logs/traces/metrics.parquet` with
  RCAEval's column names: `container_name`/`message`, `serviceName`/`spanID`/
  `parentSpanID`/`duration` in us, wide metrics `<cmdb_id>_<kpi_name>` and
  `<tc>_<kpi>`), so the loader, tools and grader readers are reused unchanged. Case
  directories are named by opaque id; `window.json` holds only the window. No onset
  file: in OpenRCA the time is part of the answer.
- **Assumption to check on the VM:** Bank's span durations are ms (its timestamps are).
  `yi data bank inspect` measures how often same-host child spans nest in their parents
  under ms and under us.
- **Data use:** the mirror `cdreetz/OpenRCA` (the official copy is a Google Drive
  folder the cloud container can't reach). Its `query.csv` equals the instructions of
  Microsoft's archived Bank run 136/136. Used for private evaluation only; never
  committed or redistributed; the paper is cited. OpenRCA's MIT licence is in
  `LICENSES/` for the copied candidate lists and grader patterns.
- **The bar:** Microsoft's archived RCA-agent run on Bank (Claude 3.5 Sonnet,
  `rca/archive/agent-Bank.csv`): strict 20/136 (14.7%), mean partial score 0.23, as
  their file records it. Step 2 re-grades it with our port.

### S5 step 1 on the VM (2026-10-14)

- **Fetch:** 42 files in 56 s (the mirror's CDN; ~470 MB/s). `check`: 0 problems, labels
  and split as pinned.
- **First cut (v1): 5.0 min, 1.91 GB, 15.2M log lines, 99.4M spans, 8,131 metric rows
  (~60 one-minute rows a case, 1,565-1,716 metric columns), 0 malformed lines, 0
  duplicate cells; audit over 136 cases: no hits.**
- **Span durations are ms**, as assumed: of 760,005 same-host parent/child pairs in a
  dev case, 100% nest under ms, 38% under us.
- **Found by `inspect`: a dev case with 0 spans.** Bank's trace files for 2021-03-23,
  -24 and -25 time spans in **seconds** (`1616515199`), the other seven days in ms
  (`1614873922611`), although OpenRCA's prompt says trace timestamps are ms. The v1 cut
  assumed ms for every trace file, so the 43 queries on those three days got no spans
  (the giveaway in the log: those files "parsed" at 170-210 MB/s, nothing to write).
  Cut v2 detects the unit per file from its values, stops on a file that mixes units,
  prints the units it found, warns on any case left without a source, and `inspect` now
  prints coverage over all cases. Spans on those three days are timed to the second
  only; the grader's tolerance is a minute.
- **Cut v2 (VM): 6.0 min, 2.59 GB, 15.2M log lines, 152.7M spans (+53.3M), 8,131
  metric rows; units found: logs and metrics in s (30 files), traces ms x7, s x3.**
  Per case: spans 10K / 1.0M / 2.6M (min / median / max), logs 0 / 103K / 276K, metric
  rows 31 / 60 / 60. Every case has spans and metrics. **Four test queries have no
  logs**, all on 2021-03-04 between 20:00 and 22:30 (the smallest day's log file, 0.21
  GB, has none there): missing in the data, not the cut; the Bank alert must say "no
  logs in this window" rather than imply they were not collected.

## S5 plan

1. **Data** (built and tested on a synthetic Bank, 2026-10-13): `yi data bank fetch |
   check | cut | inspect | audit | load` (SETUP step 12).
2. **The grader:** port `evaluate()`, replay Microsoft's archived predictions (each
   row's recorded score reproduced), oracle / empty / random baselines, Bank files added
   to the harness hash (Yi re-approves).
3. **The agent on Bank:** an alert with the window, the number of failures, what is
   asked and OpenRCA's candidate components and reasons; a report with an occurrence time
   and one or two root causes; tools that work without a given onset (compare the window
   with its baseline; minute-level series to pin the time); a Bank prompt. Tuned on dev.
4. **Test, once.**
5. **Deploy:** the alert webhook and a results dashboard.

## S4 plan (2026-10-13)

Root-cause top-1 is saturated (S3), so S4 is about the fault type, then the evidence.
Each change is one variant, run on dev against the S3 Sonnet run, and kept only if it
measurably helps. In order:
1. **Measure before changing** (free): `yi eval fault-signals --split dev --variant
   sonnet` prints, per dev case, what the fault did to the true root cause's telemetry
   (each metric family's rise and fall, metric gaps after the onset, log and error-log
   counts before and after, error spans) beside what the agent said and why. It tests
   the S3 guesses (a socket stressor moves cpu more than socket; deploying a code fault
   restarts the service and moves memory) and shows whether the data can separate the
   confused faults at all. It reads labels, so it lives outside `agent/` (tested).
2. **One change per measured cause**, built and run through the CLI here first.
3. **Noise first:** a second run of the unchanged Sonnet agent on dev, so a difference
   between variants can be told from run-to-run variation (S3 saw single cases flip
   between two identical Haiku runs).
4. The test split, once, with the final agent.

### S4 step 1: what the faults did (dev, `yi eval fault-signals`, 2026-10-13)

Medians for the true root cause (z = BARO's robust score of the 10 minutes after the
onset; err = error-looking log lines in the 10 minutes before / after), and how often
the S3 Sonnet agent named the fault right:

| fault | right | cpu up | mem up | disk up | socket up | latency-90 up | mem down | err logs b/a |
|---|---|---|---|---|---|---|---|---|
| cpu | 9/9 | 78 | 18 | 1.5 | 3 | 119 | 0.45 | 0 / 0 |
| socket | 0/9 | 39 | 34 | 1.5 | **17** | 21 | 0.53 | 0 / 0 |
| mem | 7/9 | 97 | **4.8e3** | 2 | 4.5 | 469 | 0.24 | 0 / 0 |
| disk | 6/9 | 65 | 337 | **9.4e9** | 3 | 62 | 0.43 | 0 / 0 |
| delay | 9/9 | 2.7 | 4.6 | 1.1 | 6 | 40 | 0.6 | 0 / 0 |
| loss | 0/9 | 5.1 | 7.2 | 1.2 | 7 | **3.8e3** | 0.5 | 0 / 1 |
| f1 | 0/4 | 109 | 94 | - | 12 | 18 | **-1.6** | 0 / 14 |
| f3 | 0/9 | 35 | 318 | 0.5 | 10 | 577 | **-5.7** | 0 / 26 |
| f4 | 0/7 | 13 | 114 | - | 15 | 274 | **-3.5** | 0 / 40 |
| f5 | 0/3 | -0.2 | 95 | - | 0 | 0.2 | **-1240** | 0 / 14 |

What the measurement says (and what it overturns):
- **socket -> cpu is right about the cause:** a socket stressor raises cpu (z 39) more
  than sockets (17), but sockets do rise: on dev every socket case has socket z >= 7.7
  and every cpu case <= 6. The data separates them; the agent read only the larger move.
- **Code faults are redeploys, and the agent mistakes the restart for a memory hog**: 17
  of 23 RE3 cases were called mem. In 20 of the 23, memory falls *below* its pre-onset
  level at some point (mem down -0.8 to -1420: a fresh process), which a memory
  stressor almost never does (mem down >= -0.6 in 8 of 9 mem cases), plus new error lines
  (0 before, 14-40 after) and, in the transcripts, Spring Boot start-up banners. Several
  of the agent's own summaries mention the restart and still conclude "memory hog". In
  f1, the agent wrote "no error logs" four times while 14 error lines were there.
- **f-numbers are partly readable:** the three f4 cases called f5 are adservice throwing
  NullPointerException ~2,900 times (a wrong return value, null, crashing its callers);
  the three f5 cases called f1 are emailservice crashing on start with "non-dummy mode
  not implemented yet" (an escaped exception). The agent reasons from the errors; the
  definitions it was given are too thin to choose well.
- **disk <-> mem:** disk stress fills the page cache (mem z 337), and memory stress spills
  to disk in Train Ticket; the ratio to normal separates them (disk z 5.7e5 to 1.9e10 in
  disk cases, at most 1.7e3 in mem cases).
- **loss -> delay (4), mem (3):** loss shows as latency (z 3.8e3) with almost no error
  spans; whether its tail moves more than its median, as delay's does not, is not
  measured here (only latency-90 is). The v2 guide states it as a mechanism; the A/B
  will tell whether it helps.

### S4 step 2: two changes, as versions (the S3 agent stays runnable)

- **prompt v2** (`agent/prompt.py`, `FAULT_GUIDE_V2`): how each fault is injected and
  what separates the look-alikes above (socket vs cpu, disk vs mem, restart vs memory
  hog, loss vs delay, and fuller f1-f5 definitions). Mechanisms only, no service or case
  (tested), and phrased qualitatively, not with thresholds fitted to dev.
- **tools v2** (`agent/tools.py`, `TOOL_SPECS_V2`): `overview` also counts error-looking
  log lines per container before / after, and `search_logs` takes `errors_only`. Aimed
  at "no error logs" when there were 14.
- `--prompt v1|v2 --tools v1|v2` select them; v1 is byte-identical to S3 (system sha
  63709d65265a, tool table 9b93a315c6f2, both pinned in tests), and each combination is
  its own condition in run.json.
- `yi eval confusion VARIANT...` (free) prints true vs named fault per variant, and
  how often the agent named at least the right family (resource, network, code): a
  diagnostic, not a graded score.

Runs (Sonnet 5.5, dev, about $2.40 and 5 minutes each): `sonnet-repeat` (v1/v1, the
noise floor), `s4-prompt` (v2/v1), `s4-prompt-tools` (v2/v2).

### S4 step 3: the A/B result (dev, 77 cases, Sonnet 5.5, VM 2026-10-13)

| variant | prompt / tools | fault right | family right | F1 re2 | F1 re3 | expected evidence | on target | $/case | steps |
|---|---|---|---|---|---|---|---|---|---|
| sonnet (S3) | v1 / v1 | 31/77 | 55/77 | 0.46 | 0.00 | 0.65 | 0.90 | 0.031 | 3.7 |
| sonnet-repeat | v1 / v1 | 30/77 | 56/77 | 0.44 | 0.00 | 0.65 | 0.88 | 0.031 | 3.7 |
| s4-prompt | v2 / v1 | 43/77 | 73/77 | 0.71 | 0.12 | 0.85 | 0.90 | 0.045 | 4.9 |
| **s4-prompt-tools** | **v2 / v2** | **49/77** | **75/77** | **0.80** | **0.24** | **0.88** | 0.91 | 0.044 | 4.7 |

Top-1 stays at 0.99-1.00 in every run. The three runs cost $9.27.

Reading:
- **The noise floor is small:** two runs of the same agent differ by one case on fault
  type (31 vs 30) and on family (55 vs 56).
- **Prompt v2 is a real effect, +12 fault types and +18 families**, an order of
  magnitude past the noise. Socket goes 0/9 -> 9/9 with cpu still 9/9; the code faults
  stop being called mem (family right on RE3: 0/23 -> 21/23); loss 0/9 -> 3/9.
- ~~Tools v2 add +6 more~~ **Not established** (corrected below): the repeat of v2/v2
  scored 43, the same as prompt v2 alone. I had judged the +6 against the noise of v1,
  whose errors were systematic and therefore stable; v2's are not (step 4).
- **What is left:** the f-number among code faults (RE3 exact 6/23, F1 0.24: f3 is
  read as f5 or f4, f5 as f1, f1 as f3) and mem <-> disk (5 of 18). Whether the f-number
  is recoverable from telemetry at all is doubtful: the f5 cases crash on
  `start(dummy_mode=False)`, which reads as an incorrect parameter value (f1). With 23
  dev cases, further tuning on them would be fitting dev, not diagnosis.
- **Cost of the gain:** +42% per case ($0.031 -> $0.044) and one more step: the agent
  now checks a second family before naming the fault.

Before the test split (the one look), two things to build and check here first:
- **Elasticsearch's shard limit:** the node allows 1,000 shards; dev uses about 250, and
  the 283 test cases would need about 760 more. Test cases will be loaded, run and
  unloaded a few at a time (`yi agent run --unload-after`, to be built), which also
  keeps disk and heap flat.
- **The final agent:** v2/v2, after the repeat, and after the model A/B (Opus 5.5 and
  Haiku 5.5 on v2/v2), so the test run reports the agent chosen on dev.

### S4 step 4: the model A/B, and the noise it exposed (dev, v2/v2, VM 2026-10-13)

| run | model | fault right | family right | F1 re2 | F1 re3 | on target | $/case | steps | s/case |
|---|---|---|---|---|---|---|---|---|---|
| s4-prompt-tools | Sonnet 5.5 | 49/77 | 75/77 | 0.80 | 0.24 | 0.91 | 0.044 | 4.7 | 21 |
| s4-repeat | Sonnet 5.5 | 43/77 | 73/77 | 0.70 | 0.17 | 0.91 | 0.045 | 4.9 | 14 |
| opus-s4 | Opus 5.5 | **54/77** | 74/77 | 0.82 | **0.39** | 0.84 | 0.114 | 5.8 | 24 |
| haiku-s4 | Haiku 5.5 | 44/77 | 71/77 | 0.74 | 0.12 | 0.87 | **0.006** | 7.5 | 31 |

Top-1 1.00 in all four. The three new runs cost $12.70.

- **The runs are not deterministic, and v2 is noisier than v1.** The same Sonnet
  condition scored 49 and 43. v1's two runs differed by one because its mistakes were
  systematic (socket always called cpu); v2 replaced them with close calls (which
  f-number, loss or delay) that flip from run to run. Sampling can't be pinned: these
  models reject temperature and top_p. So a single run per condition can't separate
  conditions closer than about 6 cases; repeats, compared case by case, can
  (`yi eval agreement`, free: per case, right in every run, wrong in every run, or
  flipping).
- **Haiku (44) sits inside Sonnet's range (43-49)** at 1/7 of the cost, but scores lower on
  RE3 (0.12) and evidence on target (0.87 vs 0.91), and takes longer (7.5 steps, 31 s).
  One run: plausible, not shown.
- **Opus (54) is above both Sonnet runs**, mostly on code faults (f1 3/4, f3 4/9; RE3 F1
  0.39), at 2.5x Sonnet's cost; its citations are less often on target (0.84). Also one
  run.
- Decision pending: two more Haiku runs and one more Sonnet run (and, if Opus is a
  candidate for the final agent, one more Opus run), then `yi eval agreement` per model.

### S4 step 5: repeats and the model decision (dev, v2/v2, VM 2026-10-13)

The rule, written before these runs: Haiku replaces Sonnet if its 3-run mean fault-type
score is within 3 cases of Sonnet's and its evidence on target within 0.05.

| model | fault right, 3 runs | mean | on target (mean) | flip between runs | right in all / wrong in all | $/case |
|---|---|---|---|---|---|---|
| Sonnet 5.5 | 49, 43, 46 | **46.0** | 0.90 | 18/77 | 39 / 23 | 0.044 |
| Haiku 5.5 | 44, 44, 37 | 41.7 | 0.85 | 31/77 | 33 / 28 | 0.0065 |
| Opus 5.5 | 54 (one run) | - | 0.84 | - | - | 0.114 |

- **Haiku fails the rule** (4.3 cases behind, on target 0.047 behind) and is the less
  stable model (31 cases flip vs 18). **Sonnet stays.** Cost was not part of the rule
  on purpose: Haiku is 1/7 of the price, and the rule says what that saving costs in
  diagnosis.
- **Prompt v2's gain is robust:** v1 scored 31 and 30, v2 46 +- 3 over three runs, about
  +15 cases. The tools' share of it is not separable from noise (prompt v2 alone: 43,
  one run); tools v2 stay in the final agent at no cost, with no gain claimed.
- **Opus's 54 is 5 above Sonnet's best of three**, likely real but one run, at 2.6x
  Sonnet's cost. It stays the measured ceiling, not the final agent, unless Yi chooses
  to pay for it.
- **What stays wrong in every Sonnet run (23 cases):** mostly f-numbers (f5 always read
  as f1, f3 as f5) and mem <-> disk. These are the diagnosis limits of this agent on
  this benchmark.

**Final agent: Sonnet 5.5, prompt v2, tools v2.** Next: the test split, once.

### S4 result: the test split, once (283 cases, VM 2026-10-13). S4 closed.

The final agent (Sonnet 5.5, prompt v2, tools v2, chosen on dev in step 5) and BARO, on
the 283 test cases nothing was tuned on. One run, as planned; `--unload-after` kept
Elasticsearch under its shard limit (no unload failed). Logs: `~/test-run.txt`,
`~/test-baro.txt` on the VM.

| | AC@1 (95% CI) | AC@3 | Avg@5 | fault right | family right | F1 re2 | F1 re3 | on target | $/case |
|---|---|---|---|---|---|---|---|---|---|
| BARO | 0.29 (0.24-0.35) | 0.92 | 0.79 | - | - | 0.15 | 0.00 | 0.29 | free |
| **agent** | **0.98 (0.96-0.99)** | 0.99 | 0.99 | **189/283** | 269/283 | **0.75** | **0.40** | 0.93 | 0.041 |

283/283 submitted, 0 infra failures, 0 tool errors; 4.5 steps, 8.1 tool calls, 26 s per
case (max 16 steps, $0.17); $11.68 in all. Citations: exists 1.00, holds 1.00, the
paper's root-cause indicator cited in 0.88 of the cases that have one.

- **Dev held up on test.** Fault type right 67% on test against 60% on dev (46 +- 3 of
  77); RE2 161/216, RE3 28/67, family 269/283. Nothing suggests dev was overfit: the
  changes were mechanisms (step 1), not case-specific rules.
- **Top-1 misses (5):** four are network faults on Train Ticket's auth service, where
  the agent named its database (`ts-auth-mongo`) or a caller first; all but one have
  the true service in the top 3.
- **Fault type, true -> named (test):** socket 33/36 and cpu 32/36 (S3's systematic
  socket -> cpu is gone on held-out cases); delay 34/36; disk 26/36. What remains:
  **mem -> disk 15 of 36** (worse than dev's 5/18), **loss -> delay 11 of 36**, and the
  f-number among code faults (exact 28/67, family 61/67: f3 read as f4/f5, f4 as f5).
- **BARO on test** is AC@1 0.29, close to the paper's level and to dev's 0.25; it has no
  fault-type model beyond the top metric's family (F1 0.15 / 0.00).
- **Not on test, on purpose:** Opus (one dev run, 54/77) and the S3 v1 agent. Their test
  numbers would be a second look; dev is where they were compared.

## Ideas for S2 from S1

- **A metadata-only baseline as a leak detector.** The metric column set differs by case
  (OB 69-77 columns). If a model given only the column names, row counts and file sizes
  beats chance on the root cause, the data's shape leaks the label and every score needs
  that baseline next to it. (Built in S2 as `shape`: leave-one-out nearest neighbour on
  the metric column set, and separately on the injection time, since cases were likely
  recorded one service after another. A column that vanishes because a container died is
  a real symptom, not a leak; the time channel is the dangerous one, and matters as soon
  as anything carries memory across cases, like S4's lessons ledger.)

## S1 status

**S1 closed (2026-10-11).** The done-criteria hold: cases load into fresh indices with
opaque names, the leakage tests pass, the split's hash is pinned.

dev_small on the VM (DigitalOcean, 4 shared vCPU, 7.8 GB, ES heap 2 GB):
`yi data load --split dev_small --keep` loaded all 24 cases with every count verified and
no failures, in **17 min 27 s** (17.7M documents, 2.2 GB on disk, all indices green;
3.6 GB RAM still available with Elasticsearch up; disk 9% used).

| suite-system | cases | documents per case | MB per case | sources |
|---|---|---|---|---|
| re2-ob | 6 | 656K-691K | 76-80 | logs, traces, metrics |
| re2-ss | 6 | 195K-205K | 18-22 | logs, metrics |
| re2-tt | 6 | 1.41M-2.10M | 189-298 | logs, traces, metrics |
| re3-ob | 2 | 330K-342K | 33-34 | logs, traces, metrics |
| re3-ss | 2 | 196K-199K | 18-19 | logs, metrics |
| re3-tt | 2 | 641K-813K | 44-71 | logs, traces, metrics |

The VM indexes at about 15-17K documents/s (the cloud container did 41K): a Train Ticket
case takes about 110 s. Extrapolated, all 360 cases would be about 4.4 h and 33 GB, so
loading once and keeping (the revised decision) is the right one; full dev (77 cases) is
about 1 h and 7 GB once.

First VM run (2026-10-11): `check` 0 problems, `inspect` gave the real schemas (the
traces' 'HH:MM' `time` crashed the first reader; the time column is now chosen from the
values), `audit` clean. The dev_small load looked stuck: it printed nothing until a case
finished, and a Train Ticket case is 1.7M documents; it now prints progress and loads
about 1.9x faster. Open: timed loads of dev_small on the VM, with disk use.

## Interview talking points

(Filled in as each stage closes, with numbers.)

**S4 (fault type, and one look at test).**
- *The result, on 283 held-out test cases:* root cause first 98% (BARO 29%), Avg@5 0.99
  (BARO 0.79); fault type right 67%, macro-F1 0.75 on RE2 and 0.40 on RE3 (BARO 0.15 and
  0.00); every citation exists and holds; $0.041 and 26 s a case. One run, chosen on dev
  before test was touched.
- *Measure before changing:* a free per-case table of what each fault did to its root
  cause showed why the S3 agent erred (a socket stressor burns CPU, but raises sockets
  far more; deploying a code fault restarts the service and drops memory). The prompt
  changes describe those mechanisms; socket went 0/9 -> 9/9 on dev and 33/36 on test.
- *Noise is a measurement, not a footnote:* the models reject temperature, and the same
  condition scored 49 and 43. Three repeats per model and a case-by-case agreement table
  (right in all, wrong in all, flipping) turned "Haiku is about as good" into a rule set
  before the runs: Haiku failed it (41.7 vs 46.0), so the cheaper model was not taken.
  A claimed +6 from new tools did not survive its repeat and was withdrawn.
- *What isn't solved:* mem vs disk (a memory stressor's page-cache traffic looks like
  disk I/O), loss vs delay, and the exact code-fault number, which may not be
  recoverable from telemetry at all. Localization is saturated on RCAEval; OpenRCA (S5)
  is where it can still discriminate.

**S3 (the agent).**
- *The result:* on 77 held-out dev cases the agent names the root-cause service first in
  99-100% of cases (Haiku 5.5 at half a cent a case, Sonnet 5.5 at 3 cents), against 25%
  for BARO, RCAEval's classical baseline, on the same cases.
- *Is the gain the model's? An ablation:* a fixed rule reading the same tool output
  scores 31%. The tool is BARO's statistic; the model is what chooses the cause among the
  services that moved (BARO had it in its top 3 in 87% of cases).
- *Is it a leak? A placebo:* the same agent shown only pre-fault data, with a false onset,
  scores 3% (chance 18% Avg@5). The two controls cost about $1 and answer the two
  questions an interviewer should ask about a 0.25 -> 1.00 jump.
- *What isn't solved:* fault type (macro-F1 0.46 on RE2, 0.00 on RE3), with systematic
  confusions that have mechanical explanations (socket stress burns CPU; a code fault is
  deployed by a restart that moves memory). That is the S4 target, and the honest
  framing: localization on this benchmark is saturated; diagnosis is not.
- *The loop is ~200 lines, no framework:* append-only history for caching, typed report
  as the final tool call, API errors retried and classed as infra, a placebo enforced in
  the one class every query passes through.

**S2 (eval harness, before the agent).**
- *Every grader has an oracle, a null and a wrong answer.* On the real dev split the
  oracle scores 1.00 everywhere and random lands at chance (Avg@5 0.22 against 0.18), so
  a score means what it says before any agent runs. Fabricated log lines, spans and
  metrics fail the grounding check in the selftest.
- *Reproduce the baseline before using it as the bar.* Our BARO first missed the paper
  by 0.12-0.30 on five of six faults. The misses' top scores (4096, 8192, 135168) were
  multiples of a 4 KB memory page: the port skipped RCAEval's bytes-to-MB conversion on
  the reasoning that robust z is unit-free, which fails exactly when a series is flat
  before the onset (spread 0, scale 1, z = raw bytes). After the fix: six of six equal.
  A second hypothesis (a different input file) was tested and ruled out the same day.
- *Leak detectors as baselines.* A "model" that predicts the service of the nearest
  other injection in time scores 0.90 top-1: the benchmark was recorded one service at a
  time. A per-case agent can't use it, but any memory across cases could, so the rule
  became: nothing that crosses cases carries times, dates or case ids.
- *The bar, honestly stated:* BARO on dev is AC@1 0.25, AC@3 0.87, Avg@5 0.74. Beating
  its top-3 will be hard; its top-1, delay and fault type are the openings.
- *A lock on the harness.* Scores are refused unless the hash of the graders, labels and
  split matches the one Yi approved; every scores.json records it.

**S1 (data).**
- *The labels are in the file names.* RCAEval's case directories are named
  `re2tt_ts-order-service_cpu_1`; one leaked name and every score is meaningless. The
  agent only ever sees opaque ids (`c944ccf91d8`); the alert is checked for every
  service and fault name over all 360 cases; the loaded documents are checked for paths
  and for `root_cause.txt`; and a scan of all 360 cases' telemetry for fault-injector
  traces (chaos tools, stress-ng, case names) came back clean.
- *A grouped split.* Repetitions of the same injection are near-duplicates, so all of
  them sit on one side; tuning on rep 1 and testing on rep 2 would inflate the score.
  A first draw also put both RE3 Train Ticket dev groups on the same root-cause service,
  which would have taught the prompt "the answer is ts-route-service"; the split now
  requires distinct services.
- *Same information as the baseline.* RCAEval hands its baselines the injection time,
  so the agent's alert carries it too; a late alert is a separate, harder condition.
- *Verify end to end, measure before deciding.* Every load must count exactly the
  documents it produced, or it fails (the Citi lesson: a pipeline that dropped 100% of
  events while every service was green). The README's schema was wrong (traces' `time`
  is 'HH:MM'), which a real-data `inspect` caught before any agent ran. "Load per trial"
  was the plan until a Train Ticket case measured 1.7M documents and about 110 s on the
  VM; at 33 GB for all cases, loading once with a verified completion marker won.
