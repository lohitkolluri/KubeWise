# Contributing to KubeWise

Thanks for helping improve KubeWise. Please read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before participating.

This document covers repo layout, development workflow, and quality expectations.

## Repository layout

```
cmd/                    # Binaries: agent (in-cluster), kwctl (CLI)
internal/
  agent/                # Agent pipeline: collect → predict → gate → remediate
    featureflags/       # feature gates (env-var-based, all default false)
    notify/             # Slack, PagerDuty, Alertmanager notifier
  api/                  # Agent HTTP API
  cache/                # Semantic incident cache for LLM dedup
  cli/                  # kwctl commands, TUI, and install wizard
    wizard/             # Interactive bubbletea setup wizard (9-step)
  engine/               # Deterministic rule engine (v2 fast-path)
  llmrouter/            # Multi-model LLM router (cheap vs capable models)
  promptctx/            # Context builder: prompt compression for token reduction
  tools/                # Tool plugin system (kubectl, helm, argocd, github, terraform)
  version/              # Release version (ldflags at build time)
charts/kubewise/        # Production Helm chart (preferred for installs)
manifests/              # Kustomize bases + overlays (dev, install, prod)
docker/                 # Container build definitions
forecaster-sidecar/     # Python gRPC forecasting service
pkg/                    # Shared libraries (models, k8s helpers, namespace)
proto/                  # gRPC/protobuf definitions
scripts/                # Build/install helpers (sourced by Makefile)
hack/                   # Local dev bootstrap (kind, test workloads)
npm/                    # npm wrapper scripts (published via GoReleaser)
docs/                   # Architecture, roadmap, and migration guides
```

**Install paths:** use `kwctl install --helm` or `helm install` for production; Kustomize overlays remain for dev and one-click remote apply.

## Prerequisites

- Go 1.26+ (`go.mod`)
- `kubectl` + cluster access for integration testing
- Optional: `helm`, `kind`, `docker`, `golangci-lint`

## Development workflow

```bash
make build          # bin/agent + bin/kwctl
make test           # go test ./...
make lint           # go vet + gofmt check
make golangci       # full linter (recommended before PR)
make helm-lint      # chart validation
make kind-up        # local kind cluster with Prometheus
make deploy-dev     # build images, load into kind, apply dev overlay
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
  - `internal/cache/` — semantic incident cache (hash-based, stores LLM responses).
  - `internal/tools/` — tool plugin system (kubectl, helm, argocd, github, terraform).
  - `internal/cli/wizard/` — interactive setup wizard (bubbletea TUI).
  - All feature gates are in `internal/agent/featureflags.Flags`, default off.
  - New API routes for feature-gated capabilities are registered conditionally based on flag state.

## Integrating external APIs and libraries

**Research first, implement second.** Do not copy API examples from the wrong model class or an outdated blog post.

1. Read [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) for pinned versions and verified call patterns.
2. Confirm against **official docs** for that exact version (statsmodels, OpenRouter SDK, client-go, etc.).
3. Use `parallel-cli search "…"` or Context7 when unsure.
4. Run a **minimal repro** (`make test-forecaster`, `go test ./internal/agent/llm/...`, or a one-off in Docker).
5. Update `docs/INTEGRATIONS.md` and pin versions when adding or changing a dependency.

Forecaster changes: `make test-forecaster` (requires Docker). Go changes: `make test`.

## Pull requests

1. Fork and branch from `main`.
2. Ensure `make test` and `make lint` pass.
3. Update docs if you change install paths, config keys, or API contracts.
4. Describe **why** in the PR summary; link issues when applicable.

## Releases

Tagged `v*` pushes trigger [GoReleaser](.goreleaser.yaml) and npm publish ([release workflow](.github/workflows/release.yml)).

Bump versions together when cutting a release:

- `charts/kubewise/Chart.yaml` (`version` + `appVersion`)
- `manifests/overlays/install/kustomization.yaml` image tags

## Security

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## Project docs

- [GOVERNANCE.md](GOVERNANCE.md) — how decisions are made today
- [MAINTAINERS.md](MAINTAINERS.md) — who to contact
- [ADOPTERS.md](ADOPTERS.md) — public users (add yours via PR)
