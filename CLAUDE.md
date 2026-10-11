# CLAUDE.md

Guidance for Claude Code when working in this repository.

## This project

An incident-triage agent over logs, metrics and traces in Elasticsearch, with a frozen
evaluation harness on public benchmarks (RCAEval RE2 + RE3 now, OpenRCA Bank later).
Raw Python on the Anthropic SDK, no agent frameworks. The LLM, trace and context layers
are vendored from kama (VENDORED.md); the loop, tools and evals are written here.

Stage plan and the facts about the data: `docs/ROADMAP.md`. Setup and the VM:
`docs/SETUP.md`. Current stage: **S0 built; next: S0 check on the VM, then S1 (data)**.

### Commands

```bash
uv sync                  # install (Python 3.12)
make verify              # ruff lint + format check, mypy --strict, pytest (no network)
make es-up / es-down     # local Elasticsearch in Docker (127.0.0.1:9200)
make es-test             # Elasticsearch integration tests
make live                # real-API tests (needs ANTHROPIC_API_KEY; costs money)
```

Settings come from `~/.yi/.env`, then `./.env`, then env vars: `ANTHROPIC_API_KEY`,
`YI_DATA_DIR` (benchmark data, outside the repo), `YI_ES_URL`, `YI_ES_HEAP`.

### Layout

```
src/yi_agent/
  llm/        provider protocol, Anthropic provider (streamed), pricing, retry   (vendored)
  trace/      spans, tracer (ContextVar parents), analyze/render/chrome export     (vendored)
  context.py  ContextMeter: request sizes exact from usage, the tail estimated     (vendored)
  outputs.py  OutputStore and cut(): results over the cap saved whole              (vendored)
tests/unit/          fast, no network
tests/integration/   need Elasticsearch (marker `es`)
docs/                ROADMAP.md, SETUP.md
```

### Invariants (keep these true)

- **No label reaches the agent.** Benchmark case names encode the answer. The agent sees
  opaque case ids only; labels live where only the grader reads them; a leakage test
  checks prompts, index names and tool output.
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

### Conventions

- Python 3.12, `mypy --strict`, ruff (line length 100). `make verify` passes before commit.
- Comments and docstrings in English, short, and only where the *why* isn't obvious.
- Tests use real services where cheap (Elasticsearch in Docker), fakes for the LLM
  (`tests/fakes.py`), `httpx2.MockTransport` for provider HTTP. Wait on readiness, never
  a fixed sleep.
- Secrets live in `~/.yi/.env` (git-ignored); never commit keys or benchmark data.
