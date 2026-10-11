# yiagent

An incident-triage agent: given an alert, it investigates a system's logs, metrics and
traces in Elasticsearch and reports the root-cause service, the fault type and the
evidence, citing the exact log lines, spans and metrics it used.

It is measured, not demoed. A frozen evaluation harness grades every report against
hidden labels from public benchmarks:

- **[RCAEval](https://github.com/phamquiluan/RCAEval)** (WWW 2025, MIT): RE2 and RE3,
  360 fault-injected failure cases in three microservice systems.
- **[OpenRCA](https://github.com/microsoft/OpenRCA)** (ICLR 2025), the Bank system, later.

Metrics: macro-F1 on the fault type, top-k accuracy on the root-cause service, evidence
grounding (does every citation exist and support the claim), and trajectory cost (steps,
tokens, dollars, wall time), compared against the benchmark's classical baselines.

**Status:** S0, the scaffold. See [docs/ROADMAP.md](docs/ROADMAP.md).

```bash
uv sync && make verify        # lint, mypy --strict, unit tests
make es-up && make es-test    # local Elasticsearch (docs/SETUP.md)
```

Built in raw Python on the Anthropic SDK, no agent framework. The LLM, tracing and
context layers come from [kama](https://github.com/yz10923-beep/project-1), an earlier
project (see [VENDORED.md](VENDORED.md)).
