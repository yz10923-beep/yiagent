# Vendored code

These modules were copied from kama (github.com/yz10923-beep/project-1, branch
`claude/eager-ptolemy-6ta3lk`, commit `6071a7b`, the closed S6 state). They are owned
here from now on; kama is not a dependency. Record every change below, so a fix can be
ported in either direction.

| Here | kama | Changes |
|---|---|---|
| `src/yi_agent/llm/types.py` | `core/llm/types.py` | none |
| `src/yi_agent/llm/anthropic_provider.py` | `core/llm/anthropic_provider.py` | import paths |
| `src/yi_agent/llm/pricing.py` | `core/llm/pricing.py` | none |
| `src/yi_agent/llm/retry.py` | `core/llm/retry.py` | import paths |
| `src/yi_agent/trace/span.py` | `core/trace/span.py` | none |
| `src/yi_agent/trace/tracer.py` | `core/trace/tracer.py` | import paths; ContextVar renamed |
| `src/yi_agent/trace/analyze.py` | `core/trace/analyze.py` | import paths; `PLAN_TOOL_NAMES` defined locally (kama's plan module isn't here) so kama traces still read correctly |
| `src/yi_agent/context.py` | `core/context.py` | import paths |
| `src/yi_agent/outputs.py` | `core/outputs.py` | none |
| `tests/unit/test_anthropic_provider.py` | same | import paths; the full-loop wire test removed (it needs kama's loop) |
| `tests/unit/test_trace.py`, `test_context.py` | same | only the tests of the vendored modules; loop-level tests stay in kama |
| `tests/fakes.py` | same | import paths |

The span format is kept identical to kama's on purpose: `trace/analyze.py` reads traces
from either project.
