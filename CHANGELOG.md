# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/2.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- CNN classifier training pipeline with KubeWatch dataset download
- ONNX model training pipeline and Go inference runtime
- Multi-dimensional HalfSpaceTrees with engineered features for ML anomaly detection
- ML model infrastructure: HalfSpaceTrees, adaptive thresholding, failure predictor
- Isolation Forest ML detector (experimental)
- OwnerReference-based entity tracking for pod restart resilience
- Password-based auth exchange with token persistence
- Design spec for `kwctl install` release-on-update flow
- 52 tests for auth handler, HTTP client, profile, and install

### Changed
- Third-party library upgrades and code cleanup
- Replaced custom algorithms with proven Go libraries
- Comprehensive anomaly detection optimization for >=95% accuracy via metric-family routing
- Switched pod_memory_usage from RZ to Changepoint strategy
- Professionalised CI pipeline: matrix builds, vulncheck, Trivy, coverage, Docker cache, concurrency
- Removed dead code, experimental files, unused imports
- CI no longer blocks on CVE scanning during early development

### Fixed
- Resolved 50 bugs across agent pipeline: race conditions, data integrity, LLM safety, API hardening
- Resolved bottlenecks preventing 95% detection accuracy
- pod_memory_usage excluded from statistical detection (gradual trends poorly detected)
- Bbolt lock uses Recreate strategy; improved `--pass` auth diagnostics
- `--pass` takes precedence over cached token in agent request
- Removed blue background from TUI footer status bar
- API token saved to profile during install with `--pass`
- Linter issues in test files

## [1.0.2] - 2026-07-11

### Added
- TUI mouse mode support
- Password-based auth exchange flow

### Fixed
- Production kind image loading in install
- Interactive API token prompt
- Auto-generated API token in wizard, removed background colors
- Manifest path detection uses DetectAll; added PatchConfigMapObservability

### Changed
- Fixed all golangci-lint issues across the codebase
- Fixed gofmt formatting in 13 Go files

## [1.0.1] - 2026-07-11

### Added
- Alloy+Tempo manifest resources and overlay updates
- Multi-backend observability detection engine
- Database backup API endpoint
- Circuit breaker for Ollama and OpenAI LLM providers
- Senior SRE-level kubectl capabilities (unblocked commands, expanded RBAC)
- `view_logs` remediation action

### Changed
- TUI v2 upgrade with mouse support
- Simplified Helm observability install with subchart auto-detection
- Migrated remaining packages from log to log/slog
- Enabled additional golangci-lint linters
- Updated persistence defaults and Loki configuration
- Updated Prometheus PromQL queries for kube-state-metrics v2.16.0

### Fixed
- RBAC, API auth, and CLI/tool safety hardening
- Tool/API security and remediation reliability
- Auth defaults and chart wiring alignment
- Plan validation demotion, TUI scrolling, UTF-8 truncation, pod prefix fallback
- Prometheus service address in Helm values
- Agent service name aligned with Helm chart default

### Security
- Bound forecaster sidecar to localhost only

## [1.0.0] - 2026-07-08

### Added
- Tool plugin interface, registry, executor, and config
- Tool plugins: kubectl, helm, argocd, GitHub REST API, Terraform binary
- Observability stack: Loki, Tempo, event store, Grafana dashboards
- Hash-based semantic cache for LLM deduplication
- Feature flags, rule engine, and pipeline integration
- Tiered LLM routing layer with per-task model dispatch
- Database backup endpoint
- Pipeline and cache stats endpoints
- PagerDuty integration
- Interactive setup wizard

### Changed
- Production-optimized Dockerfiles with multi-stage builds and BuildKit cache
- Migrated all packages to log/slog

### Fixed
- Helm chart defaults and service name alignment

## [0.4.0] - 2026-07-07

### Added
- Shared domain types for anomaly, prediction, and remediation
- bbolt embedded store with metrics ring buffer
- Kubernetes client helpers (in-cluster and kubeconfig modes)
- Prometheus metrics collector with 17 PromQL queries
- K8s event delta watcher
- Statistical predictor with EWMA, Z-score, and rate-of-change
- Adaptive median + Hoeffding bounds + BOCPD predictor
- Static pattern matchers: OOMRisk, CrashLoopRisk, Degradation
- LLM-powered remediation system with risk-tiered gating
- Tier-2 Python forecasting sidecar with gRPC
- kwctl CLI: status, config, predict commands
- kwctl control center TUI and remediation runtime APIs
- HTTP API: health, status, predictions, anomalies, config
- Kubernetes deployment manifests for in-cluster agent
- Helm chart with store indexes and namespace-scoped operations
- Multi-LLM support, notifications, outcome tracking
- OpenRouter Go SDK integration
- Docker image and Helm chart publishing to GHCR
- Audit, accuracy, and health API endpoints and CLI commands
- Audit CLI fetch, theme colors, and UI dashboard improvements
- Development and deployment scripts
- README with mermaid architecture and pipeline diagrams

### Changed
- Switched from Python v1 codebase to Go v2
- Go module scaffold and project directory structure
- Enhanced gate/scorer tests with NaN-safe clamp and config validation
- Bumped golangci-lint from v2.1.6 to v2.12.2 for Go 1.26 compat
- Fixed all golangci-lint issues and gofmt formatting

### Fixed
- End-to-end agent pipeline: gate, patterns, correlator, K8s wiring
- Agent pipeline hardening: dedup, gate sustainment, real rollback
- Codebase audit findings across gate, forecast, correlator, API, CLI
- Predictor data race and cp.Add double-count
- Predictor verification tests and changepoint blind spot
- Docker agent CI tracking
- Go formatting for CI compliance

[Unreleased]: https://github.com/lohitkolluri/KubeWise/compare/v1.0.2...HEAD
[1.0.2]: https://github.com/lohitkolluri/KubeWise/releases/tag/v1.0.2
[1.0.1]: https://github.com/lohitkolluri/KubeWise/releases/tag/v1.0.1
[1.0.0]: https://github.com/lohitkolluri/KubeWise/releases/tag/v1
[0.4.0]: https://github.com/lohitkolluri/KubeWise/releases/tag/v0.4.0
