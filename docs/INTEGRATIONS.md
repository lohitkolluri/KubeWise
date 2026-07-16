# Integrations

## LLM Providers

- **OpenRouter**: Configured via `OPENROUTER_API_KEY`, agent config `llm_provider: openrouter`, `llm_model`. Default model: `deepseek/deepseek-v4-flash`.
- **Ollama** (air-gapped): `llm_provider: ollama`, `llm_base_url`, `llm_model`. No API key needed.
- **OpenAI-compatible**: `llm_provider: openapi`, `llm_base_url`, `llm_model`, and API key.

## Notifiers

- **Slack**: Set `notifications.slack_webhook_url` in agent config. Triggers on new prediction, remediation execution, pending approval.
- **PagerDuty**: Set `notifications.pagerduty_routing_key`. Events map to PagerDuty incidents by severity.
- **Alertmanager**: Agent exposes `/metrics` for Prometheus; Alertmanager can alert on `kubewise_anomaly_score` metrics.
- **Generic Webhook**: Set `notifications.webhook_url`. POST JSON payload with event type, score, entity, and details.
- All notifiers respect `notifications.min_score` threshold (default 0.7).

## Tool Plugins

- **kubectl**: Execute kubectl commands against the cluster. Restricted to read-only commands by default.
- **helm**: Helm chart operations (install, upgrade, rollback). Arg-allowlist enforced.
- **ArgoCD**: ArgoCD Application sync and health checks. Requires ArgoCD credentials.
- **Terraform**: Terraform plan/apply for infrastructure management. Restricted to specific workspaces.
- **GitHub CLI**: GitHub operations (create PR, comment on issue). Requires `GITHUB_TOKEN`.
- Each tool has runtime configuration in `internal/tools/config.go` with per-tool allowlists, timeouts, and environment overrides.

## Observability Stack

- **Prometheus** / **VictoriaMetrics**: Primary metrics source. Auto-detected at runtime. Can be provided by the cluster or deployed via the Helm chart subchart.
- **VictoriaLogs** / **Loki**: Log storage for pod log queries. Agent queries logs via Loki HTTP API for LLM context.
- **Grafana Tempo**: Trace storage. Agent queries traces for LLM context via Tempo HTTP API.
- **Grafana Alloy**: DaemonSet that scrapes pod logs and forwards to VictoriaLogs/Tempo. Deployed automatically when observability feature is enabled.

## Forecaster (statsmodels ETS)

The Python forecaster uses `statsmodels.tsa.api.ETSModel`. ETS prediction results expose `summary_frame()` / `pred_int()` and do not expose regression-style `se_mean` in the same way.

If you change statsmodels usage, validate with:
- `make test-forecaster`
- `hack/e2e-verify.sh` (kind)
