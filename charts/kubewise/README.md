# KubeWise Helm Chart

Install the in-cluster agent + forecaster sidecar.

## Quick start

```bash
helm install kubewise ./charts/kubewise \
  --namespace kubewise --create-namespace \
  --set secrets.openrouterApiKey="$OPENROUTER_API_KEY" \
  --set persistence.enabled=true \
  --set security.requireApiToken=true \
  --set secrets.apiToken="$(openssl rand -hex 24)"
```

Or via CLI:

```bash
kwctl install --helm --yes
```

## Dependencies

This chart depends on VictoriaMetrics (`victoria-metrics-single`), VictoriaLogs (`victoria-logs-single`), and Grafana Tempo subcharts. Set `agent.observability.vm.enabled=false`, `agent.observability.vl.enabled=false`, or `agent.observability.tempo.enabled=false` to skip subcharts if you already have these backends.

## Configuration

### Image settings

| Value | Default | Description |
|-------|---------|-------------|
| `image.agent.repository` | `ghcr.io/lohitkolluri/kubewise-agent` | Agent container image |
| `image.agent.tag` | `latest` | Agent image tag |
| `image.agent.pullPolicy` | `IfNotPresent` | Agent image pull policy |
| `image.forecaster.repository` | `ghcr.io/lohitkolluri/kubewise-forecaster` | Forecaster container image |
| `image.forecaster.tag` | `latest` | Forecaster image tag |
| `image.forecaster.pullPolicy` | `IfNotPresent` | Forecaster image pull policy |

### RBAC

| Value | Default | Description |
|-------|---------|-------------|
| `rbac.create` | `true` | Create RBAC resources |
| `rbac.clusterScoped` | `true` | ClusterRole (true) or namespace-scoped Role (false) |
| `rbac.extraNamespaces` | `[]` | Additional namespaces for Role bindings when clusterScoped=false |

### Agent config

| Value | Default | Description |
|-------|---------|-------------|
| `agent.scrapeInterval` | `30s` | Prometheus scrape interval |
| `agent.prometheusAddress` | `""` (auto-detect) | Prometheus-compatible endpoint |
| `agent.llmProvider` | `openrouter` | LLM provider (openrouter/ollama/openapi) |
| `agent.llmModel` | `deepseek/deepseek-v4-flash` | LLM model identifier |
| `agent.llmBaseURL` | `""` | Custom LLM base URL |
| `agent.watchNamespaces` | `[]` (all) | Limit observation to specific namespaces |
| `agent.remediation.mode` | `dry-run` | `dry-run`, `auto`, `off`, `semi` |
| `agent.remediation.dryRun` | `true` | When true, log actions without executing |
| `agent.remediation.rateLimit` | `3` | Max concurrent remediations |
| `agent.remediation.minConfidence` | `0.7` | Minimum confidence for remediation |
| `agent.remediation.namespaceDenylist` | `[kube-system, kubewise]` | Protected namespaces |

### Feature flags (all default `true` since v2)

| Value | Default | Description |
|-------|---------|-------------|
| `agent.features.ruleEngine` | `true` | Deterministic rules before LLM path |
| `agent.features.contextBuilder` | `true` | Prompt compression for token reduction |
| `agent.features.llmRouter` | `true` | Multi-model tiered LLM routing |
| `agent.features.semanticCache` | `true` | Incident dedup hash cache |
| `agent.features.eventsV2` | `true` | Shared-informer K8s event collector |
| `agent.features.toolPlugins` | `true` | Tool plugin system |
| `agent.features.observability` | `true` | Loki + Tempo + Alloy stack |

### Observability subcharts

| Value | Default | Description |
|-------|---------|-------------|
| `agent.observability.namespace` | `kubewise` | Observability stack namespace |
| `agent.observability.vm.enabled` | `true` | Deploy VictoriaMetrics |
| `agent.observability.vl.enabled` | `true` | Deploy VictoriaLogs |
| `agent.observability.tempo.enabled` | `true` | Deploy Tempo (tracing) |
| `agent.observability.alloy.enabled` | `true` | Deploy Grafana Alloy DaemonSet |

### Notifications

| Value | Default | Description |
|-------|---------|-------------|
| `agent.notifications.enabled` | `false` | Enable notification dispatch |
| `agent.notifications.webhookURL` | `""` | Generic webhook URL |
| `agent.notifications.slackWebhookURL` | `""` | Slack webhook URL |
| `agent.notifications.minScore` | `0.7` | Minimum score to notify |
| `agent.notifications.onPrediction` | `true` | Notify on predictions |
| `agent.notifications.onRemediation` | `true` | Notify on remediation |
| `agent.notifications.onApproval` | `true` | Notify on pending approvals |

### Secrets

| Value | Default | Description |
|-------|---------|-------------|
| `secrets.openrouterApiKey` | `""` | OpenRouter API key |
| `secrets.apiToken` | `""` | KubeWise API token for CLI auth |
| `secrets.clientPasswordHash` | `""` | bcrypt password hash for CLI auth |
| `secrets.existingSecret` | `""` | Existing secret name (alternative to inline) |

### Security

| Value | Default | Description |
|-------|---------|-------------|
| `security.requireApiToken` | `true` | Fail closed if API token missing |
| `security.enableExecPrimitives` | `false` | Enable pod exec/port-forward RBAC |

### Persistence

| Value | Default | Description |
|-------|---------|-------------|
| `persistence.enabled` | `true` | PVC for BoltDB (recommended) |
| `persistence.size` | `10Gi` | PVC size |
| `persistence.storageClass` | `""` | Storage class |

### Resources

| Value | Default | Description |
|-------|---------|-------------|
| `resources.agent.requests` | `100m CPU, 128Mi` | Agent pod requests |
| `resources.agent.limits` | `500m CPU, 512Mi` | Agent pod limits |
| `resources.forecaster.requests` | `200m CPU, 256Mi` | Forecaster pod requests |
| `resources.forecaster.limits` | `1 CPU, 512Mi` | Forecaster pod limits |

## Namespace-scoped install

```bash
helm install kubewise ./charts/kubewise \
  --set rbac.clusterScoped=false \
  --set agent.watchNamespaces="{demo,staging}" \
  --set rbac.extraNamespaces="{staging}"
```

## Probes

| Container  | Liveness       | Readiness     |
| ---------- | -------------- | ------------- |
| agent      | `GET /health`  | `GET /readyz` |
| forecaster | `GET /healthz` | `GET /readyz` |

## Uninstall

```bash
helm uninstall kubewise -n kubewise
```

PVCs are retained when `persistence.enabled=true` -- delete manually if needed. **Data loss warning**: disabling persistence causes loss of all anomaly history, predictions, audit trail, and health scores on pod restart.
