# External integrations

KubeWise depends on several third-party APIs and libraries. **Before changing any integration, read the official docs for the exact version we pin** and add or update a row in the table below.

Research workflow (required for new/changed integrations):

1. **Identify the canonical source** — official docs, not Stack Overflow alone.
2. **Verify the API for our pinned version** — run a minimal repro in Docker or `go test` / `python -m unittest`.
3. **Document the correct call pattern** in this file (with links).
4. **Pin versions** in `go.mod`, `requirements.txt`, or Helm `Chart.yaml`.
5. **Add a regression test** that would have caught the mistake.

Use [parallel-cli search](https://parallel.ai) (Exa-backed web search) or [Context7](https://context7.com) when docs are unclear.

## Integration catalog

| Component                    | Version pin                                           | Canonical docs                                                                                                                                                                                                                                                                     | Verified API pattern                                                                                                                                                                                                                                                                                                                                               |
| ---------------------------- | ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **statsmodels ETS**          | `requirements.txt` → `statsmodels>=0.14.4,<0.15`      | [ETS get_prediction](https://www.statsmodels.org/stable/generated/statsmodels.tsa.exponential_smoothing.ets.ETSResults.get_prediction.html), [ETS notebook](https://www.statsmodels.org/stable/examples/notebooks/generated/ets.html)                                              | `fit.get_prediction(start=n, end=n+h-1)` returns **ETS `PredictionResults`**, not regression `PredictionResults`. Use `summary_frame()` → `pi_lower` / `pi_upper`, or `pred_int(alpha=0.05)`. **Do not use `se_mean`** — that exists on regression/GLM results and on `tsa.base.prediction.PredictionResults`, but **not** on the ETS wrapper returned at runtime. |
| **OpenRouter Go SDK**        | `go.mod` → `github.com/OpenRouterTeam/go-sdk v0.5.12` | [Go SDK overview](https://openrouter.ai/docs/client-sdks/go/overview), [Chat.Send](https://github.com/OpenRouterTeam/go-sdk/blob/main/docs/sdks/chat/README.md), [JSON schema format](https://github.com/OpenRouterTeam/go-sdk/blob/main/docs/models/components/responseformat.md) | Default: `google/gemini-2.0-flash-001`. Auto-fallback on timeout: DeepSeek v3, Qwen 2.5 7B, Llama 3.2 3B. `WithTimeout(180s)`, `MaxTokens` 1024, remediation 4m. Override with `llm_model` in config.                                                                                                                                                              |
| **gRPC (Python forecaster)** | `grpcio>=1.60,<2`                                     | [grpc.io Python](https://grpc.io/docs/languages/python/)                                                                                                                                                                                                                           | `grpc.aio.server` + `ForecasterServicer`; health on separate HTTP `:8081`.                                                                                                                                                                                                                                                                                         |
| **gRPC (Go agent)**          | `google.golang.org/grpc v1.82`                        | [grpc.io Go](https://grpc.io/docs/languages/go/)                                                                                                                                                                                                                                   | Client in `internal/agent/forecaster`; insecure channel to sidecar on `localhost:50051`.                                                                                                                                                                                                                                                                           |
| **Prometheus client (Go)**   | `prometheus/client_golang v1.23`                      | [client_golang](https://github.com/prometheus/client_golang)                                                                                                                                                                                                                       | Agent exposes custom metrics via `prometheus/client_golang` on `/metrics`. Scrapes use PromQL against cluster Prometheus.                                                                                                                                                                                                                                          |
| **client-go**                | `k8s.io/client-go v0.30`                              | [client-go](https://github.com/kubernetes/client-go)                                                                                                                                                                                                                               | Remediation executor uses typed clients; RBAC in chart/manifests.                                                                                                                                                                                                                                                                                                  |
| **bbolt**                    | `go.etcd.io/bbolt v1.5`                               | [bbolt](https://github.com/etcd-io/bbolt)                                                                                                                                                                                                                                          | Agent state; `/readyz` calls `store.Ping()`.                                                                                                                                                                                                                                                                                                                       |
| **Helm chart**               | `charts/kubewise/Chart.yaml`                          | [Helm docs](https://helm.sh/docs/)                                                                                                                                                                                                                                                 | `helm lint` + `helm template` in CI.                                                                                                                                                                                                                                                                                                                               |
| **kube-prometheus-stack**    | dev bootstrap                                         | [prometheus-community](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack)                                                                                                                                                                 | Bootstrap patches `prometheus_address` in agent ConfigMap.                                                                                                                                                                                                                                                                                                         |

## statsmodels ETS — RCA (forecast `se_mean` bug)

**Symptom:** Agent logs flooded with:

```text
forecast error: forecaster error: ETS model failed: 'PredictionResults' object has no attribute 'se_mean'
```

**Root cause:** `forecaster-sidecar/server.py` assumed all `get_prediction()` results expose `se_mean` (copied from regression examples). At runtime, `ETSModel.fit().get_prediction()` returns `statsmodels.tsa.exponential_smoothing.ets.PredictionResults`, which provides:

- `predicted_mean`
- `summary_frame(alpha=0.05)` with columns `mean`, `pi_lower`, `pi_upper`
- `pred_int(alpha=0.05)` method

See [statsmodels ETS prediction internals](https://www.statsmodels.org/stable/_modules/statsmodels/tsa/exponential_smoothing/ets.html) (`pred_int` builds intervals from `forecast_variance` or simulation).

**Fix:** `_prediction_intervals()` in `forecaster-sidecar/server.py` — prefer `summary_frame()` / `pred_int()`, with fallbacks for other result types.

**Regression test:** `forecaster-sidecar/test_server.py` (run via `make test-forecaster` or CI).

## OpenRouter — structured remediation plans

**Symptom:** `remediation error: llm correlation: chat completion: context deadline exceeded`.

**Root cause:** Older default (`meta-llama/llama-3.1-8b-instruct`) was too slow for strict JSON-schema remediation.

**Model tiers:**

| Model                         | Cost         | Context | Best for                                                |
| ----------------------------- | ------------ | ------- | ------------------------------------------------------- |
| `openai/gpt-oss-120b`         | Paid         | 131K    | **Local dev** — strong reasoning, extensive RCA prompts |
| `openai/gpt-oss-120b:free`    | Free         | 131K    | Local dev (free tier) — rate-limited, relaxed JSON schema |
| `poolside/laguna-m.1:free`    | Free         | 262K    | Coding-style tasks, largest free context                |
| `google/gemini-2.0-flash-001` | ~$0.10/1M in | 1M      | **Production** — fastest structured JSON                |

Free models use `strict: false` on JSON schema (better compatibility). Paid fallbacks use `strict: true`.

**Dev overlay** (`manifests/overlays/dev`) defaults to `openai/gpt-oss-120b`. Production base/install use Gemini Flash.

**Rich context:** Investigator sends more logs (8KB/container, 150 lines), events (25), metric history (15 points).

**Override:** `kwctl config set llm_model poolside/laguna-m.1:free` or edit ConfigMap `llm_model`.

**Verify:** `agent: LLM provider openrouter validated`; logs show `remediator: plan steps=…` or `llm: openrouter fallback model … succeeded`.

## Adding a new integration

1. Add a row to the table above.
2. Pin the version in the appropriate manifest.
3. Add a minimal test or smoke script.
4. Link this doc from the PR description.
