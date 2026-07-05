---
slug: kubewise-v2
status: awaiting-approval
intent: clear
pending-action: write .omo/plans/kubewise-v2.md
approach: Phase 1 (Foundation) — incremental build from scaffolding through deploy
---

# Draft: kubewise-v2

## Components (topology ledger)
| id | outcome | status |
|----|---------|--------|
| C1 | Go project scaffolding (go.mod, cmd/, internal/ structure) | active |
| C2 | Shared domain types (pkg/models) | active |
| C3 | bbolt store with metrics/anomalies/config buckets | active |
| C4 | K8s client helpers (client-go wrapper) | active |
| C5 | Prometheus metrics collector (PromQL queries) | active |
| C6 | K8s event watcher (delta watch) | active |
| C7 | K8s resource informers (pods, nodes, deployments) | active |
| C8 | Statistical predictor (EWMA, Z-score, rate-of-change) | active |
| C9 | Pattern matchers (OOMRisk, CrashLoopRisk) | active |
| C10 | Agent HTTP API (REST routes) | active |
| C11 | CLI commands (status, config, predict) | active |
| C12 | K8s deployment manifests (namespace, SA, RBAC, ConfigMap, Deployment) | active |
| C13 | kind dev environment scripts | active |

## Open assumptions (announced defaults)
| assumption | adopted default | rationale | reversible? |
|------------|----------------|-----------|-------------|
| Logging library | go.uber.org/zap | Standard Go structured logging | Yes |
| Config management | viper + ConfigMap | Cobra companion, reads YAML + env vars | Yes |
| Metric ring buffer size | 30 samples (15min @ 30s interval) | Matches DESIGN.md baseline_window | Yes |
| bbolt bucket structure | metrics, anomalies, remediations, config | Clean separation, easy iteration | Yes |
| CLI↔agent protocol | HTTP REST (no gRPC initially) | Simpler, debuggable, sufficient for alpha | Yes |
| PromQL queries | 17 queries from DESIGN.md | Covers resource, failure, node, network signals | Yes |
| Pattern matcher interface | Go interface with Confidence()/Action() methods | Pluggable, testable | Yes |
| Agent discovery | Port-forward via k8s API | Works without cluster DNS, no service needed | Yes |

## Findings (cited - path:lines)
- Existing KubeWise v1 is Python/FastAPI — v2 is full rewrite in Go (DESIGN.md:1-10)
- Phase 1 focuses on scaffolding, store, collector, stats predictor, CLI, manifests (DESIGN.md Roadmap section)
- Zero CGO constraint applies to all dependencies (DESIGN.md Tech Stack section)
- bbolt bucket organization derived from store requirements (DESIGN.md Agent → Store)
- PromQL queries defined in DESIGN.md Agent → Collector section
- CLI commands defined in DESIGN.md CLI section

## Decisions (with rationale)
1. **Phase 1 only**: LLM, guardrails, remediation, Bubbletea watch deferred to Phase 2. Phase 1 builds the foundation that all subsequent phases depend on.
2. **Test-first for predictor**: Statistical algorithms (EWMA, Z-score) need unit tests with known datasets to verify correctness.
3. **internal/ over pkg/ for agent code**: Agent implementation is internal — no external consumers. Only shared domain types go in pkg/.
4. **Single main.go per binary**: cmd/kwctl/main.go and cmd/agent/main.go — clean entrypoints.
5. **ConfigMap YAML loaded by viper**: Agent reads config from mounted ConfigMap volume + env overrides.

## Scope IN (Phase 1)
- Go project scaffolding with module path `github.com/lohitkolluri/KubeWise/v2`
- Cobra CLI framework for kwctl + agent
- Shared models: AnomalyRecord, PredictionResult, RemediationAction, AgentConfig
- bbolt store with create/open, metrics ring buffer CRUD, anomaly CRUD, config CRUD
- K8s client wrapper (config loading, typed client creation)
- Prometheus collector: HTTP client, 17 PromQL queries, response parsing, metric storage
- K8s event delta watcher: list+watch with resourceVersion, event filtering, storage
- K8s resource informer: pods, nodes, deployments with field selectors
- Statistical predictor: EWMA tracker, Z-score calculator, rate-of-change detector, score combiner
- Pattern matcher interface + OOMRisk/CrashLoopRisk implementations
- Agent HTTP API: /status, /predictions, /config endpoints
- CLI: kwctl status (table output), kwctl config (show current), kwctl predict (list predictions)
- K8s manifests: namespace, serviceaccount, clusterrole (with rules), configmap, deployment, service, kustomization
- kind cluster creation + dev deploy script
- Unit tests for predictor, store, collector parsing
- hack/install.sh one-liner

## Scope OUT (Must NOT have)
- OpenRouter LLM client and Tier 3 prediction
- Guardrails engine (any layer)
- Remediator (any action executor)
- Bubbletea watch TUI
- Helm chart
- Multi-cluster support
- Notification webhooks
- gRPC API (HTTP only)
- CI/CD pipelines (future)
- Grafana dashboards (future)

## Open questions
None — Phase 1 scope is defined by the approved DESIGN.md.

## Approval gate
status: awaiting-approval
pending-action: write .omo/plans/kubewise-v2.md
approach: 14 todos across 6 parallel waves, Wave 1→2→3→4 are sequential, Wave 5+6 parallelize
