# Production readiness backlog

Tracked improvements before roadmap features (PagerDuty, ReAct, multi-cluster, HA, web UI).

## Completed (this pass)

- [x] Centralized version (`internal/version`) wired to API + GoReleaser ldflags
- [x] Agent bootstrap extracted from `cmd/agent/main.go` (`internal/agent/bootstrap`)
- [x] `/readyz` probe (store ping) + `/metrics` Prometheus exposition
- [x] Auth bypass for probe/observability paths (`/health`, `/readyz`, `/metrics`)
- [x] golangci-lint config + CI job
- [x] Helm lint + template validation in CI
- [x] CONTRIBUTING.md, SECURITY.md, chart README
- [x] Readiness probes use `/readyz` in Helm + Kustomize manifests
- [x] Removed unused `pkg/llm` placeholder

## P0 — next hardening

- [ ] Structured logging (`log/slog`) with consistent component fields
- [ ] Agent `/status` and `/metrics` expose gate + remediation counters as gauges
- [ ] Pin `forecaster-sidecar/requirements.txt` versions + multi-stage Dockerfile hardening
- [ ] Commit + document npm/release pipeline (`.goreleaser.yaml`, `release.yml`, `npm/`)
- [ ] Single install source-of-truth doc: when to use Helm vs Kustomize vs `kwctl install`
- [ ] Integration test: `helm install` smoke in CI (kind job)

## P1 — industrial quality

- [ ] `golangci-lint` strict mode: enable `gosec`, `revive` incrementally
- [ ] API OpenAPI spec (`docs/openapi.yaml`) generated or hand-maintained
- [ ] Config validation schema (reject invalid remediation modes at bootstrap)
- [ ] Leader election RBAC → actual HA implementation (stubs exist in manifests)
- [ ] Forecaster gRPC health wired into agent `/readyz` (optional dependency)
- [ ] SBOM / image signing in release pipeline (cosign)

## P2 — before multi-cluster / web UI

- [ ] ADR folder (`docs/adr/`) for architectural decisions
- [ ] Load testing harness for BoltDB at scale
- [ ] E2E test suite against kind (predict → anomaly → dry-run remediation)
- [ ] Operator pattern evaluation (CRD vs Helm-only)

## Intentionally deferred (roadmap)

- PagerDuty / incident.io integrations
- ReAct investigation loop
- Multi-cluster federation
- Web UI (beyond kwctl TUI)
