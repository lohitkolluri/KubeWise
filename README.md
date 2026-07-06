<div align="center">

# KubeWise

**Know before it breaks.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Go](https://img.shields.io/badge/Go-1.26+-00ADD8?logo=go&logoColor=white)](go.mod)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-in--cluster-326CE5?logo=kubernetes&logoColor=white)](manifests/base)
[![CI](https://github.com/lohitkolluri/KubeWise/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/lohitkolluri/KubeWise/actions/workflows/ci.yml)

</div>

KubeWise lives in your cluster. It pulls metrics from Prometheus, watches your workloads, and spots trouble early. Out of the box it only observes: nothing in the cluster changes until you flip remediation on.

## Architecture

<p align="center">
  <img src="docs/architecture.svg" alt="KubeWise system architecture" width="960"/>
</p>

Roughly every 30 seconds the agent scrapes, scores predictions, filters noise, and writes to a local store. If remediation is enabled, it digs into describe/events/logs, asks an LLM for a runbook, runs it, and checks that the fix actually stuck.

## Install

You need `kubectl` and a cluster with Prometheus. The installer looks for Prometheus on its own.

### npm (macOS / Linux)

```bash
npm install -g kwctl
# or
npm install -g kubewise-cli

kwctl version
kwctl install --yes
# production: kwctl install --helm --yes
```

Same binary either way. Node 16+.

### Helm

```bash
helm install kubewise ./charts/kubewise \
  --namespace kubewise --create-namespace \
  --set secrets.openrouterApiKey=$OPENROUTER_API_KEY \
  --set persistence.enabled=true \
  --set security.requireApiToken=true \
  --set secrets.apiToken=$(openssl rand -hex 24)
```

Namespace-scoped RBAC (no ClusterRole):

```bash
helm install kubewise ./charts/kubewise \
  --set rbac.clusterScoped=false \
  --set agent.watchNamespaces="{demo,staging}"
```

### curl

```bash
curl -fsSL https://raw.githubusercontent.com/lohitkolluri/KubeWise/main/hack/bootstrap.sh | bash
```

Or install `kwctl` from GitHub Releases first:

```bash
curl -fsSL https://raw.githubusercontent.com/lohitkolluri/KubeWise/main/hack/install.sh | bash
kwctl install --yes
```

### Local dev (kind)

```bash
git clone https://github.com/lohitkolluri/KubeWise.git && cd KubeWise
./hack/bootstrap.sh --local
```

### LLM remediation (optional)

```bash
export OPENROUTER_API_KEY="sk-or-..."
kwctl install --yes
```

No API key? Predictions and the HTTP API still work. You just won't get LLM runbooks.

### Ollama (local / air-gapped)

```bash
# set in agent config.yaml or env
export OLLAMA_BASE_URL="http://ollama:11434"
# llm_provider: ollama
# llm_model: llama3.1:8b
```

### Slack / webhook notifications

Enable in agent `config.yaml`:

```yaml
notifications:
  enabled: true
  slack_webhook_url: https://hooks.slack.com/services/...
  min_score: 0.7
```

## Use it

```bash
# terminal 1
kubectl -n kubewise port-forward svc/kubewise-agent 8080:8080

# terminal 2
kwctl connect
kwctl status
kwctl          # opens the TUI
```

Running `kwctl` with no arguments opens the dashboard: predictions, anomalies, audit trail, approvals, config, logs. `Ctrl+P` opens the command palette. `?` lists shortcuts.

A few you'll use often: `kwctl predict`, `kwctl anomalies`, `kwctl remediation`, `kwctl stats`, `kwctl logs -f`, `kwctl watch`. Config is at `~/.config/kwctl/config.yaml`.

## API

After port-forwarding to `:8080`:

```bash
curl localhost:8080/status
curl localhost:8080/api/v1/predictions
curl localhost:8080/api/v1/anomalies
curl localhost:8080/api/v1/stats
```

Also: `/api/v1/audit`, `/api/v1/approvals`, `/api/v1/config`, `/api/v1/stats`, `/api/v1/remediation/mode` (toggle live vs observe).

## Remediation

Actions are grouped by how risky they are:

| Tier | Examples                         | What happens         |
| ---- | -------------------------------- | -------------------- |
| T1   | restart/delete pod               | auto in live mode    |
| T2   | scale, rollback, patch resources | auto, 5 min cooldown |
| T3   | escalate                         | needs your approval  |
| T4   | unknown / cluster-wide           | rejected             |

## Develop

```bash
make kind-up        # kind + prometheus + deploy
make deploy-dev     # rebuild and rollout
make test
make port-forward
```

Go 1.26+, Docker. Local clusters: [kind](https://kind.sigs.k8s.io/) and Helm.

### Releasing

Push a `vX.Y.Z` tag on `main`. GoReleaser cuts GitHub Release binaries and publishes `kwctl` and `kubewise-cli` to npm. Set `NPM_TOKEN` in repo secrets ([npm automation token](https://docs.npmjs.com/about-access-tokens)).

```bash
goreleaser release --snapshot --clean
DRY_RUN=true ./scripts/npm-publish.sh v0.0.0-snapshot dist
```

## License

MIT. See [LICENSE](LICENSE).
