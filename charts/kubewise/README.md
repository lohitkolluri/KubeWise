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

## Key values

| Value                      | Default             | Description                       |
| -------------------------- | ------------------- | --------------------------------- |
| `agent.prometheusAddress`  | kube-prometheus URL | Prometheus HTTP endpoint          |
| `agent.watchNamespaces`    | `[]` (all)          | Limit observation scope           |
| `agent.remediation.mode`   | `dry-run`           | `dry-run`, `auto`, `off`, `semi`  |
| `rbac.clusterScoped`       | `true`              | `false` for namespace-scoped Role |
| `persistence.enabled`      | `false`             | PVC for BoltDB (recommended prod) |
| `security.requireApiToken` | `false`             | Fail closed if API token missing  |

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

PVCs are retained when `persistence.enabled=true` — delete manually if needed.
