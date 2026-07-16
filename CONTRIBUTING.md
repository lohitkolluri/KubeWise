# Contributing to KubeWise

Thanks for helping improve KubeWise. Please read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before participating.

This document covers repo layout, development workflow, and quality expectations.

## Repository layout

```
cmd/                      # Binaries
  agent/                  # In-cluster prediction loop agent
  kwctl/                  # CLI client (Cobra)
  benchmark/              # Anomaly detection benchmark tool
internal/
  agent/                  # Core agent pipeline
    agent.go              # Orchestration: collect → detect → gate → remediate
    bootstrap/            # Startup: store init, config load, LLM validation, auth
    collector/            # Prometheus metrics + K8s resource/event collectors
    predictor/            # Statistical anomaly detection (Z-score, changepoint, ROC)
    gate/                 # Noise filter with score-cooldown-per-entity dedup
    llm/                  # LLM client facade (OpenRouter, Ollama, OpenAI)
    remediator/           # Remediation orchestration, K8s executor, verification
    forecaster/           # gRPC client for Python forecaster sidecar
    outcome/              # Prediction outcome tracking, health/accuracy computers
    notify/               # Slack, PagerDuty, Alertmanager, webhook notifier
    store/                # BoltDB-backed persistent store (18 buckets)
    featureflags/         # 7 feature gates (env-var based)
    semcache/             # Hash-based semantic incident cache (LLM dedup)
  api/                    # HTTP API with ~20 endpoints
  cli/                    # kwctl commands, TUI, and install wizard (Cobra)
    wizard/               # Interactive BubbleTea setup wizard
  engine/                 # Deterministic rule engine (8 built-in rules, fast path)
  llmrouter/              # Multi-model LLM router with fallback chains
  promptctx/              # Compact context builder for LLM prompts (80% reduction)
  tools/                  # Tool plugin system (kubectl, helm, argocd, github, terraform)
  version/                # Release version via ldflags
  logx/                   # Structured logging (charmbracelet/log)
pkg/
  models/                 # Shared types: anomaly, prediction, remediation, config, health, accuracy
  k8s/                    # K8s client, observability detection, install helpers
  namespace/              # Namespace scoping with wildcard glob support
charts/kubewise/          # Production Helm chart (preferred install method)
manifests/                # Kustomize bases + overlays (dev, install, prod)
docker/
  agent/                  # Multi-stage distroless Dockerfile
forecaster-sidecar/       # Python gRPC forecasting service (statsmodels ETS)
hack/                     # Local dev bootstrap scripts (kind, test workloads)
proto/                    # gRPC/protobuf definitions
docs/                     # Architecture, migration, integration, and research docs
data/                     # Training datasets (KubeWatch, anomaly detection benchmarks)
.github/                  # CI workflows, issue templates, PR template
```

Install paths: use `kwctl install --helm` or `helm install` for production; Kustomize overlays remain for dev and one-click remote apply.

## Prerequisites

- Go 1.26+ (`go.mod`)
- `kubectl` + cluster access for integration testing
- Optional: `helm`, `kind`, `docker`, `golangci-lint`

## Development workflow

```bash
make build          # bin/agent + bin/kwctl + bin/benchmark
make test           # go test ./... with race detection
make lint           # go vet + gofmt check
make golangci       # full linter suite (recommended before PR)
make kind-up        # local kind cluster with Prometheus
make deploy-dev     # build images, load into kind, apply dev overlay
make e2e            # e2e verification
make docker-agent   # build agent Docker image
```

Run the agent locally (outside cluster):

```bash
KUBEWISE_DATA_DIR=/tmp/kw go run ./cmd/agent
```

## Code standards

- Match existing package boundaries: agent logic stays in `internal/agent`, shared types in `pkg/models`.
- Prefer focused changes; avoid drive-by refactors.
- Add tests for behavior changes (especially store, API, remediation).
- Use `pkg/namespace.InScope` for namespace filtering — do not duplicate.
- API probe endpoints: `/health` (liveness), `/readyz` (store ready), `/metrics` (Prometheus).
- Version strings come from `internal/version` only.
- Feature-gated packages follow these conventions:
  - `internal/engine/` — deterministic rule engine (no LLM deps).
  - `internal/promptctx/` — context builder for prompt compression (not `internal/context/`).
  - `internal/llmrouter/` — multi-model LLM routing layer.
  - `internal/agent/semcache/` — hash-based semantic incident cache (LLM dedup).
  - `internal/tools/` — tool plugin system (kubectl, helm, argocd, github, terraform).
  - `internal/cli/wizard/` — interactive setup wizard (bubbletea TUI).
  - All feature gates are in `internal/agent/featureflags.Flags`, default to `true` since v2. Set env var to `false` to disable.
  - New API routes for feature-gated capabilities are registered conditionally based on flag state.

## Integrating external APIs and libraries

Research first, implement second. Do not copy API examples from the wrong model class or an outdated blog post.

1. Read [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) for pinned versions and verified call patterns.
2. Confirm against official docs for that exact version (statsmodels, OpenRouter SDK, client-go, etc.).
3. Use `parallel-cli search "..."` or Context7 when unsure.
4. Run a minimal repro (`make test-forecaster`, `go test ./internal/agent/llm/...`, or a one-off in Docker).
5. Update `docs/INTEGRATIONS.md` and pin versions when adding or changing a dependency.

Forecaster changes: `make test-forecaster` (requires Docker). Go changes: `make test`.

## Pull requests

1. Fork and branch from `main`.
2. Ensure `make test` and `make lint` pass.
3. Update docs if you change install paths, config keys, or API contracts.
4. Describe why in the PR summary; link issues when applicable.

## Releases

Tagged `v*` pushes trigger [GoReleaser](.goreleaser.yaml) and npm publish ([release workflow](.github/workflows/release.yml)). npm publish runs automatically via CI after GoReleaser.

Bump versions together when cutting a release:

- `charts/kubewise/Chart.yaml` (`version` + `appVersion`)
- `manifests/overlays/install/kustomization.yaml` image tags

## Security

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## Project docs

- [GOVERNANCE.md](GOVERNANCE.md) — how decisions are made today
- [MAINTAINERS.md](MAINTAINERS.md) — who to contact
- [ADOPTERS.md](ADOPTERS.md) — public users (add yours via PR)
