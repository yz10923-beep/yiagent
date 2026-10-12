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
