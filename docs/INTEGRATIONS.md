## Integrations (pinned patterns)

This document captures “known-good” integration patterns used in this repo so contributors don’t accidentally copy outdated examples.

### LLM providers

- **OpenRouter**: configured via `OPENROUTER_API_KEY`, `llm_provider: openrouter`, and `llm_model`.
- **Ollama** (air-gapped): `llm_provider: ollama`, `llm_base_url`, and `llm_model`.
- **OpenAPI-compatible** (OpenAI-style API): `llm_provider: openapi`, `llm_base_url`, `llm_model`, and an API key (provider specific).

### Forecaster (statsmodels ETS)

The Python forecaster uses `statsmodels.tsa.api.ETSModel`. ETS prediction results expose `summary_frame()` / `pred_int()` and **do not** expose regression-style `se_mean` in the same way.

If you change statsmodels usage, validate with:
- `make test-forecaster`
- `hack/e2e-verify.sh` (kind)

