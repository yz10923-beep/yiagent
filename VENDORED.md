# Vendored code

These modules were copied from kama (github.com/yz10923-beep/project-1, branch
`claude/eager-ptolemy-6ta3lk`, commit `6071a7b`, the closed S6 state). They are owned
here from now on; kama is not a dependency. Record every change below, so a fix can be
ported in either direction.

| Here | kama | Changes |
|---|---|---|
| `src/yi_agent/llm/types.py` | `core/llm/types.py` | none |
| `src/yi_agent/llm/anthropic_provider.py` | `core/llm/anthropic_provider.py` | import paths; refusal fallback (`fallbacks: "default"`) also on Sonnet 5.5 (2026-10-12) |
| `src/yi_agent/llm/pricing.py` | `core/llm/pricing.py` | rewritten 2026-10-11 from the live pricing page: per-model published rates instead of 0.1x/1.25x multipliers (Opus 5.5, Sonnet 5.5, Fable 5.1 read cache cheaper), Haiku 5.5 long-prompt card, exact/dated id match instead of prefix. **Port to kama**: its table has the same 0.1x and prefix bugs |
| `src/yi_agent/config.py` | `core/config.py` | `YI_` prefix, `~/.yi/.env`, only this project's fields, `~` expanded in `data_dir`, defaults validated |
| `src/yi_agent/llm/retry.py` | `core/llm/retry.py` | import paths |
| `src/yi_agent/trace/span.py` | `core/trace/span.py` | none |
| `src/yi_agent/trace/tracer.py` | `core/trace/tracer.py` | import paths; ContextVar renamed |
| `src/yi_agent/trace/analyze.py` | `core/trace/analyze.py` | import paths; `PLAN_TOOL_NAMES` defined locally (kama's plan module isn't here) so kama traces still read correctly |
| `src/yi_agent/context.py` | `core/context.py` | import paths |
| `src/yi_agent/outputs.py` | `core/outputs.py` | none |
| `tests/unit/test_anthropic_provider.py` | same | import paths; the full-loop wire test removed (it needs kama's loop) |
| `tests/unit/test_trace.py`, `test_context.py` | same | only the tests of the vendored modules; loop-level tests stay in kama; the pricing test moved to `test_pricing.py` |
| `tests/unit/test_config.py` | same | rewritten for this project's fields |
| `tests/fakes.py` | same | import paths |

The span format is kept identical to kama's on purpose: `trace/analyze.py` reads traces
from either project.

## Ported from RCAEval

`src/yi_agent/eval/baro.py` re-implements BARO as RCAEval runs it
(github.com/phamquiluan/RCAEval, MIT, Copyright (c) 2024 Luan Pham; licence in
`LICENSES/LICENSE-RCAEval`): `RCAEval/e2e/baro.py` (`baro`), the preprocessing it calls in
`RCAEval/io/time_series.py`, and the data preparation and service mapping in `main.py`
(read 2026-10-11, shallow clone of `main`). Changes: numpy instead of pandas and
scikit-learn (same percentiles, same zero-spread rule); no other change in behaviour.
The check that it is faithful is `yi eval reproduce-baro` against the published RE2-TT
numbers (docs/ROADMAP.md).
