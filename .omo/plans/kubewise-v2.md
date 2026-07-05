# kubewise-v2 - Work Plan

## TL;DR (For humans)

**What you'll get:** A Go-based CLI (`kwctl`) + in-cluster agent that watches your Kubernetes cluster, predicts failures like OOMKills and CrashLoopBackoffs before they happen, and reports what it sees. Phase 1 is the foundation: the data pipeline (collect metrics from Prometheus, watch K8s events, track pod/node state), the prediction engine (statistical anomaly detection + pattern matching), a CLI to query it, and deployment manifests to run it in any cluster.

**Why this approach:** Split into 6 parallel waves — scaffolding, storage, collection, prediction, API+CLI, deployment — so each layer is built and tested before the next depends on it. The in-cluster agent pattern (deployment, not operator) keeps setup simple while supporting production clusters. bbolt embedded storage means zero external databases.

**What it will NOT do:** No LLM/AI analysis yet (Phase 2), no automatic remediation (Phase 2), no Bubbletea real-time dashboard (Phase 2), no Helm chart (Phase 3), no multi-cluster (Phase 4). Phase 1 builds the reliable foundation that everything else sits on.

**Effort:** Medium (14 todos, 6 waves)
**Risk:** Low — all components are well-understood Go patterns (cobra CLI, client-go, bbolt, stdlib HTTP)
**Decisions to sanity-check:** (1) 17 PromQL queries — are these the right signals for your failure types? (2) EWMA/Z-score/ROC weights (0.25/0.5/0.25) — tune after real-world testing

Your next move: **approve** this plan. Then run `$start-work` to begin execution.

---

> TL;DR (machine): Medium effort, Low risk. 14 todos across 6 parallel waves. Phase 1: Go scaffolding, bbolt store, Prometheus/K8s collector, statistical + pattern predictor, HTTP API, Cobra CLI, K8s manifests, kind env.

## Scope
### Must have
### Must NOT have (guardrails, anti-slop, scope boundaries)

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: <TDD | tests-after | none> + framework
- Evidence: .omo/evidence/task-<N>-kubewise-v2.<ext>

## Execution strategy
### Parallel execution waves
> Target 5-8 todos per wave. Fewer than 3 (except the final) means you under-split.

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->

### Wave 1 — Foundation (parallel)

- [ ] 1. Init Go module and project scaffolding
  What to do / Must NOT do:
  - Run `go mod init github.com/lohitkolluri/KubeWise/v2`
  - Create directory structure: cmd/kwctl/, cmd/agent/, internal/agent/{collector,predictor,store}, internal/cli/, internal/api/, pkg/models/, pkg/k8s/, pkg/llm/, manifests/, hack/
  - Write cmd/kwctl/main.go: empty `func main() { println("kwctl v2 — AI SRE"); os.Exit(0) }`
  - Write cmd/agent/main.go: empty `func main() { println("KubeWise agent starting..."); os.Exit(0) }`
  - Add placeholder .gitkeep files in empty dirs if Go doesn't track them
  - Must NOT add any business logic or dependencies beyond Go standard library
  - Must NOT import any third-party packages yet
  
  Parallelization: Wave 1 | Blocked by: none | Blocks: T2, T3, T4
  References: DESIGN.md Project Structure section, cmd/kwctl/main.go, cmd/agent/main.go
  Acceptance criteria: `go build ./cmd/kwctl/` exits 0, `go build ./cmd/agent/` exits 0, `ls internal/` shows all subdirectories
  QA scenarios:
    - Happy: `go build -o /dev/null ./cmd/kwctl/ && go build -o /dev/null ./cmd/agent/` — exit 0
    - Failure: `go vet ./...` — no errors
  Commit: Y | chore: scaffold Go module and project directories

- [ ] 2. Implement shared domain types (pkg/models)
  What to do / Must NOT do:
  - Create pkg/models/anomaly.go: struct AnomalyRecord{ID, Entity, Namespace, MetricName string, Score float64, Pattern string, DetectedAt, RemediatedAt *time.Time, Status string}
  - Create pkg/models/prediction.go: struct PredictionResult{Type, Entity, Namespace, Action string, Confidence float64, ETA time.Duration, Timestamp time.Time, Score float64}
  - Create pkg/models/remediation.go: struct RemediationAction{ID, Type, Target, Status string, Params map[string]string, ExecutedAt, VerifiedAt *time.Time, Error string}
  - Create pkg/models/config.go: struct AgentConfig{ScrapeInterval, PrometheusAddress string, LLMProvider, LLMModel string, Remediation RemediationConfig} and struct RemediationConfig{Mode string, DryRun bool, RateLimit int, NamespaceDenylist []string, Allowlist []string}
  - All structs use json tags for serialization
  - All time fields use time.Time (not string)
  - Must NOT include any business logic, validation, or methods beyond constructors
  - Must NOT import k8s client-go or any external packages
  
  Parallelization: Wave 1 | Blocked by: T1 | Blocks: T3, T8, T9, T10, T11
  References: DESIGN.md Agent → Predictor section (LLM response types), Agent → Remediator section (action types)
  Acceptance criteria: `go build ./pkg/models/` exits 0, `go test ./pkg/models/` passes
  QA scenarios:
    - Happy: create each struct with valid fields, marshal to JSON — verify keys match snake_case
    - Failure: zero-value struct does not panic, JSON omitempty works for optional fields
  Commit: Y | feat: add shared domain types for anomaly, prediction, remediation

### Wave 2 — Storage + K8s (parallel, depends on Wave 1)

- [ ] 3. Implement bbolt embedded store
  What to do / Must NOT do:
  - Create internal/agent/store/store.go: Store struct wrapping *bbolt.DB with Open(path) and Close() methods
  - Create internal/agent/store/migrations.go: Init() function creating buckets: metrics, anomalies, remediations, config
  - Create internal/agent/store/metrics.go: AppendMetric(name string, value float64, ts time.Time) for ring buffer, GetMetrics(name string, n int) []MetricPoint, TrimOlderThan(d time.Duration)
  - Create internal/agent/store/anomalies.go: SaveAnomaly(r *models.AnomalyRecord), ListAnomalies(limit int) ([]models.AnomalyRecord, error), GetAnomaly(id string) (*models.AnomalyRecord, error), UpdateAnomaly(r *models.AnomalyRecord) error
  - Create internal/agent/store/config.go: SaveConfig(cfg *models.AgentConfig), LoadConfig() (*models.AgentConfig, error)
  - Use go.etcd.io/bbolt as dependency (run `go get go.etcd.io/bbolt`)
  - Metric storage uses a sorted keys approach (timestamp-prefixed) within each metric bucket
  - Ring buffer: keep max N samples per metric, delete oldest when inserting beyond limit
  - Must NOT use CGO (verify bbolt is pure Go)
  - Must NOT include any collector, predictor, or API logic
  - Add unit tests with temp dir for store operations
  
  Parallelization: Wave 2 | Blocked by: T1, T2 | Blocks: T5, T6, T8, T10
  References: DESIGN.md Agent → Store section, bbolt bucket structure from draft findings
  Acceptance criteria: `go test ./internal/agent/store/ -v` passes, all CRUD operations work on temp bbolt file
  QA scenarios:
    - Happy: open store, create buckets, save anomaly, list anomalies, get by ID, delete old metrics
    - Failure: open non-existent path (should create), load config when none saved (should return zero-value, not error)
  Commit: Y | feat: implement bbolt embedded store with metrics ring buffer

- [ ] 4. Implement K8s client helpers
  What to do / Must NOT do:
  - Create pkg/k8s/client.go: Client struct wrapping kubernetes.Clientset with NewFromCluster() and NewFromKubeconfig(path string) constructors
  - NewFromCluster() uses rest.InClusterConfig() — for running inside cluster
  - NewFromKubeconfig() uses clientcmd.BuildConfigFromFlags — for CLI out-of-cluster
  - Create pkg/k8s/client.go with typed getters: GetPods(namespace string), GetNodes(), GetDeployments(namespace string), GetEvents(namespace string, since time.Duration)
  - All methods return typed client-go objects (v1.PodList, v1.NodeList, etc.)
  - Add context.Context as first parameter to all methods
  - Dependencies: run `go get k8s.io/client-go@v0.30.0 k8s.io/api@v0.30.0 k8s.io/apimachinery@v0.30.0`
  - Must NOT include any store, collector, or predictor logic
  - Must NOT cache or filter results — raw API responses only
  - Add unit tests using fake clientsets from k8s.io/client-go/testing
  
  Parallelization: Wave 2 | Blocked by: T1 | Blocks: T5, T6, T7
  References: DESIGN.md Tech Stack (client-go), pkg/k8s/client.go structure
  Acceptance criteria: `go test ./pkg/k8s/ -v` passes with fake clients
  QA scenarios:
    - Happy: fake client returns pod list, GetPods returns expected namespaced results
    - Failure: fake client returns error (API server down), methods propagate error correctly
  Commit: Y | feat: add Kubernetes client helpers with in-cluster and kubeconfig modes

### Wave 3 — Collector (parallel, depends on Wave 2)

- [ ] 5. Implement Prometheus metrics collector
  What to do / Must NOT do:
  - Create internal/agent/collector/prometheus.go: PrometheusCollector struct with Address, Client, and CollectMetrics(ctx) method
  - Implement 17 PromQL queries from DESIGN.md Agent → Collector section (pod_cpu, pod_memory, restart_rate, oomkilled, crashloop, imagepull, not_ready, node_memory_pressure, node_disk_pressure, node_load, deployment_replicas_unavailable, tcp_retransmit, dns_failure, node_disk_usage)
  - Each query returns a MetricResult{Name string, Values []MetricPoint{Timestamp time.Time, Value float64, Labels map[string]string}}
  - Parse Prometheus response format (range vector) — use prometheus/api/v1 client or manual HTTP
  - Import `github.com/prometheus/client_golang/api` and `api/v1` for proper PromQL client
  - Store results in Store: AppendMetric for each returned series
  - Add context timeout (default 15s for scrape)
  - Must NOT include any prediction or analysis logic
  - Must NOT hardcode Prometheus address — use config
  - Add unit tests with a fake HTTP server returning sample Prometheus responses
  
  Parallelization: Wave 3 | Blocked by: T3, T4 | Blocks: T8, T9
  References: DESIGN.md Agent → Collector section (PromQL queries table)
  Acceptance criteria: `go test ./internal/agent/collector/ -v` passes with fake Prometheus server
  QA scenarios:
    - Happy: fake server returns valid Prometheus response, ParseMetrics populates MetricResult correctly
    - Failure: fake server returns 500, timeout, malformed JSON — errors are handled gracefully
  Commit: Y | feat: implement Prometheus metrics collector with 17 PromQL queries

- [ ] 6. Implement K8s events delta watcher
  What to do / Must NOT do:
  - Create internal/agent/collector/events.go: EventsCollector struct with WatchEvents(ctx) method returning event channel
  - Use client-go's Watch with resourceVersion tracking for delta updates
  - Filter to Warning events with failure-related reasons (BackOff, OOMKilling, FailedMount, ImagePull, ProbeError, NodeCondition)
  - Parse each watch event into internal EventRecord{Type, Reason, Message, Count int32, InvolvedObject, Source, FirstTimestamp, LastTimestamp time.Time}
  - Store significant events in Store (as metric-like time series or anomaly records)
  - Handle watch timeout/reconnect with exponential backoff
  - Must NOT store every event — only Warning+ failure events
  - Must NOT block on event processing (use goroutine)
  - Add unit tests using fake event watcher
  
  Parallelization: Wave 3 | Blocked by: T3, T4 | Blocks: T9
  References: DESIGN.md Agent → Collector section (K8s Events)
  Acceptance criteria: `go test ./internal/agent/collector/ -run TestEvents -v` passes
  QA scenarios:
    - Happy: fake API returns events, collector emits them on the channel
    - Failure: API watch times out, reconnection works correctly
  Commit: Y | feat: add K8s event delta watcher for failure-related events

- [ ] 7. Implement K8s resource informers
  What to do / Must NOT do:
  - Create internal/agent/collector/resources.go: ResourcesCollector struct using informers from client-go
  - Watch Pods, Nodes, Deployments using factory.NewInformer or filtered shared informers
  - Track state snapshots: PodState{Name, Namespace, Phase, Conditions, RestartCount}, NodeState{Name, Conditions}, DeploymentState{Name, Namespace, Available, Replicas}
  - Store current state in-memory (map of resource type to state objects) — no bbolt needed for live state
  - Provide accessor methods: GetFailingPods() []PodState, GetUnhealthyNodes() []NodeState
  - Handle informer sync with HasSynced check before reporting state
  - Must NOT block startup waiting for full sync (timeout after 30s)
  - Must NOT store full state in bbolt (live state is ephemeral)
  - Add unit tests using fake informer factories
  
  Parallelization: Wave 3 | Blocked by: T4 | Blocks: T9
  References: DESIGN.md Agent → Collector section (K8s Resource State)
  Acceptance criteria: `go test ./internal/agent/collector/ -run TestResources -v` passes
  QA scenarios:
    - Happy: fake informer adds pod, GetFailingPods() returns it
    - Failure: informer never syncs, Collector times out gracefully
  Commit: Y | feat: add K8s resource informers for pod/node/deployment state tracking

### Wave 4 — Predictor (depends on Wave 3)

- [ ] 8. Implement statistical predictor (EWMA, Z-score, rate-of-change)
  What to do / Must NOT do:
  - Create internal/agent/predictor/predictor.go: Predictor struct with Run(metrics []MetricResult) ([]PredictionResult, error)
  - Create internal/agent/predictor/statistical.go:
    - EWMAModel: alpha factor, current value, Update(value float64) (trend float64), returns smoothed value
    - ZScoreModel: buffer of last N values, mean, stddev, Score(value float64) (zScore float64)
    - RateOfChange: delta over window, Velocity(values []MetricPoint) (slope float64, acceleration float64)
  - Create internal/agent/predictor/scorer.go: combineSignals(ewmaScore, zScore, rateOfChange) → combined anomaly score 0-1
  - Default weights: EWMA 0.25, Z-score 0.5, ROC 0.25 (configurable)
  - Models are in-memory, updated on each scrape cycle
  - Each model maintains its own state per metric name + labels
  - Metric must have >= 3 data points before producing a score (warmup)
  - Must NOT call any LLM API or external service
  - Must NOT include pattern matching logic (separate in T9)
  - Add extensive unit tests with known datasets (monotonic increase → high score, flat → low score)
  
  Parallelization: Wave 4 | Blocked by: T3, T5 | Blocks: T10, T11
  References: DESIGN.md Agent → Predictor → Tier 1 Statistical section
  Acceptance criteria: `go test ./internal/agent/predictor/ -run TestStatistical -v` passes, known trend produces expected score
  QA scenarios:
    - Happy: memory metric rising 10% per cycle → score > 0.8 after 5 cycles
    - Failure: flat metric → score < 0.3, metric with only 2 points → no score (warmup)
  Commit: Y | feat: implement statistical predictor with EWMA, Z-score, and rate-of-change

- [ ] 9. Implement static pattern matchers (OOMRisk, CrashLoopRisk)
  What to do / Must NOT do:
  - Create internal/agent/predictor/patterns.go: PatternMatcher interface{Name() string, Match(ctx, metrics, events, resources) []PatternMatch} where PatternMatch{Pattern string, Confidence float64, TimeToFailure time.Duration, SuggestedAction string, Entity string}
  - Create internal/agent/predictor/patterns_oom.go: OOMPattern — checks memory trend > threshold AND OOMKilled in recent events → OOMRisk prediction
  - Create internal/agent/predictor/patterns_crashloop.go: CrashLoopPattern — checks restart_rate trending up AND CrashLoopBackOff events → CrashLoopRisk prediction
  - Create internal/agent/predictor/patterns_degradation.go: DegradationPattern — checks combined error_rate + latency proxy signals
  - Pattern registry in predictor.go: registered patterns are iterated on each scrape cycle
  - Patterns read from Store (metrics + events) and ResourcesCollector (pod/node state)
  - Each pattern has a minimum confidence threshold (default 0.5) before reporting
  - Must NOT store results directly — return them, caller (Predictor.Run) handles storage
  - Must NOT call LLM or external APIs
  - Add unit tests with crafted metric/event datasets that trigger each pattern
  
  Parallelization: Wave 4 | Blocked by: T5, T6, T7 | Blocks: T10, T11
  References: DESIGN.md Agent → Predictor → Tier 2 Pattern Matching section
  Acceptance criteria: `go test ./internal/agent/predictor/ -run TestPatterns -v` passes, OOMRisk pattern fires on memory+OOMKilled input
  QA scenarios:
    - Happy: memory rising + OOMKilled event → OOMPattern returns Confidence > 0.8
    - Failure: memory flat + no OOM events → OOMPattern returns nil (no match)
  Commit: Y | feat: add static pattern matchers for OOMRisk and CrashLoopRisk

### Wave 5 — API + CLI (parallel, depends on Wave 4)

- [ ] 10. Implement agent HTTP API server
  What to do / Must NOT do:
  - Create internal/api/server.go: NewServer(store, predictor) and Serve(address string) error
  - Create internal/api/routes.go:
    - GET /health — returns {"status": "ok"}
    - GET /status — returns agent summary (scrape count, active predictions count, uptime, collector status)
    - GET /api/v1/predictions — returns active predictions from store
    - GET /api/v1/anomalies?limit=20 — returns recent anomalies from store
    - GET /api/v1/config — returns current agent configuration
  - Use standard net/http with Go 1.22+ routing (http.NewServeMux with method patterns)
  - All responses are JSON with Content-Type: application/json
  - Add request logging middleware (method, path, duration, status)
  - Add recovery middleware (catch panics, return 500)
  - CORS headers for local development
  - Must NOT use gin/echo/fiber — Go 1.22+ stdlib routing is sufficient
  - Must NOT include gRPC, WebSocket, or any streaming
  - Must NOT require authentication (in-cluster network is trusted, auth added later)
  - Add unit tests using httptest.Server
  
  Parallelization: Wave 5 | Blocked by: T8, T9, T3 | Blocks: T11, T12
  References: DESIGN.md Agent → Agent HTTP API section, internal/api/ structure
  Acceptance criteria: `go test ./internal/api/ -v` passes, curl /health returns 200
  QA scenarios:
    - Happy: GET /health returns 200 with {"status":"ok"}
    - Failure: GET /nonexistent returns 404 with JSON error body, not HTML
  Commit: Y | feat: add agent HTTP API with health, status, predictions endpoints

- [ ] 11. Implement CLI commands (status, config, predict)
  What to do / Must NOT do:
  - Create internal/cli/root.go: root cobra.Command with persistent flags --kubeconfig, --context, --agent-namespace (default: kubewise), --agent-service (default: kubewise-agent), -o/--output (table/json/yaml)
  - Create internal/cli/status.go: `kwctl status` — connects to agent via port-forward or direct service call, GET /status, renders table with component rows
  - Create internal/cli/config.go: `kwctl config` — GET /api/v1/config, renders YAML or table
  - Create internal/cli/predict.go: `kwctl predict` — GET /api/v1/predictions, renders table with columns: TYPE, ENTITY, CONFIDENCE, ETA, ACTION
  - Agent connection logic: first try in-cluster service DNS (agent service in kubewise namespace), fall back to port-forward
  - Table rendering using `github.com/olekukonez/tablewriter` or `text/tabwriter` (stdlib)
  - JSON/YAML output via `-o json` / `-o yaml` flag using encoding/json and gopkg.in/yaml.v3
  - Dependencies: `go get github.com/spf13/cobra github.com/spf13/viper gopkg.in/yaml.v3`
  - Must NOT include Bubbletea (Phase 2)
  - Must NOT include watch, anomalies describe, exec commands (Phase 2)
  - Must NOT call LLM or any AI API
  - Add unit tests using cobra's Execute test pattern with command output capture
  
  Parallelization: Wave 5 | Blocked by: T10 | Blocks: T14
  References: DESIGN.md CLI section (all commands), internal/cli/ structure
  Acceptance criteria: `go build ./cmd/kwctl/` exits 0, `./bin/kwctl --help` shows all commands
  QA scenarios:
    - Happy: `go run ./cmd/kwctl/ status` returns formatted status table
    - Failure: `go run ./cmd/kwctl/ nonexistent-subcommand` exits with error message
  Commit: Y | feat: add CLI commands for status, config, and predict

### Wave 6 — Deploy + Docs (parallel, depends on Wave 5)

- [ ] 12. Create Kubernetes deployment manifests
  What to do / Must NOT do:
  - Create manifests/00-namespace.yaml: kind: Namespace, name: kubewise
  - Create manifests/10-serviceaccount.yaml: ServiceAccount + namspace
  - Create manifests/20-clusterrole.yaml: ClusterRole + ClusterRoleBinding — rules for pods (get/list/watch), nodes (get/list/watch), deployments (get/list/watch), events (get/list/watch), leases (coordination)
  - Create manifests/30-configmap.yaml: ConfigMap with embedded config.yaml matching DESIGN.md ConfigMap section
  - Create manifests/40-deployment.yaml: Deployment — 1 replica, image: ghcr.io/lohitkolluri/kubewise/agent:latest, port 8080, volume mount for ConfigMap, env from Secret (OPENROUTER_API_KEY), resource requests 100m CPU/128Mi memory, limits 500m CPU/512Mi memory
  - Create manifests/50-service.yaml: ClusterIP service on port 8080
  - Create manifests/kustomization.yaml: namespace: kubewise, commonLabels: app.kubernetes.io/name: kubewise
  - All manifests use apiVersion consistent with K8s 1.24+
  - Must NOT include ingress, HPA, PDB, or monitoring (Phase 3)
  - Must NOT include Helm chart (Phase 3)
  - Validate with `kubectl kustomize manifests/` or dry-run
  
  Parallelization: Wave 6 | Blocked by: T10 | Blocks: T13
  References: DESIGN.md Installation section, manifests/ structure from Project Structure
  Acceptance criteria: `kubectl kustomize manifests/ --dry-run=client` succeeds with no errors
  QA scenarios:
    - Happy: `kubectl kustomize manifests/` outputs valid YAML with all resources
    - Failure: missing required field produces proper error (caught by validation)
  Commit: Y | feat: add Kubernetes deployment manifests for agent

- [ ] 13. Create kind development environment scripts
  What to do / Must NOT do:
  - Create hack/kind-cluster.sh: creates kind cluster, installs Prometheus via helm, deploys KubeWise agent, waits for readiness. Based on DESIGN.md kind section.
  - Create hack/deploy-dev.sh: builds agent binary with `go build -o bin/agent ./cmd/agent/`, builds CLI with `go build -o bin/kwctl ./cmd/kwctl/`, builds local Docker image, loads into kind, applies manifests
  - Create hack/install.sh: shell one-liner installer for kwctl CLI — detects OS/arch, downloads from GitHub releases, installs to /usr/local/bin
  - Must NOT destroy existing kind cluster (check first, prompt or use existing)
  - Must NOT require Docker Hub (use local registry or kind load)
  - Must include error handling (set -euo pipefail, error messages)
  - 
  Parallelization: Wave 6 | Blocked by: T12 | Blocks: none
  References: DESIGN.md Development → kind section, hack/ structure
  Acceptance criteria: `shellcheck hack/*.sh` passes (if available), scripts are executable
  QA scenarios:
    - Happy: `bash hack/kind-cluster.sh` creates kind cluster with Prometheus
    - Failure: kind not installed — script exits with clear error message
  Commit: Y | feat: add kind development environment and install script

- [ ] 14. Update README.md for v2
  What to do / Must NOT do:
  - Replace existing README.md with v2 content
  - Sections: Overview (1 paragraph), Quick Start (build + run on kind), CLI Commands (status, config, predict), Architecture (1-line + link to DESIGN.md), Contributing, License
  - Add badge section: Go version, build status (placeholder), license
  - Keep it concise — DESIGN.md has the full detail
  - Must NOT include installation instructions that reference URLs that don't exist yet (use "coming soon" or build-from-source)
  - Must NOT include screenshots or complex formatting
  - 
  Parallelization: Wave 6 | Blocked by: T11 | Blocks: none
  References: Existing README.md, DESIGN.md
  Acceptance criteria: README.md renders correctly on GitHub (no broken links, valid markdown)
  QA scenarios:
    - Review: check all links work, code blocks have language tags, no placeholder URLs
  Commit: Y | docs: update README.md for KubeWise v2

## Final verification wave
> Runs in parallel after ALL todos. All must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. Plan compliance audit — verify every todo was completed against acceptance criteria
- [ ] F2. Code quality review — `go vet ./...`, `golangci-lint run` (if available), no CGO in deps
- [ ] F3. Build integrity — `go build ./cmd/kwctl/ && go build ./cmd/agent/` exits 0, binaries run
- [ ] F4. Manifest validation — `kubectl kustomize manifests/ --dry-run=client` succeeds

## Commit strategy
- Each todo produces exactly one atomic commit
- Format: `<type>(<scope>): <summary>` with body ≤72 chars per line
- Types: `feat` for new features, `chore` for scaffolding, `docs` for documentation
- Commits are made on the `v2` branch

## Success criteria
1. `go build ./cmd/kwctl/` and `go build ./cmd/agent/` succeed
2. `go test ./...` passes all unit tests
3. `kubectl kustomize manifests/ --dry-run=client` validates manifests
4. Statistical predictor produces anomaly scores > 0.8 on known-rising metrics
5. Pattern matchers detect OOMRisk and CrashLoopRisk patterns from crafted inputs
6. CLI outputs formatted tables for status, config, and predict commands
7. Agent HTTP API responds on /health, /status, /api/v1/predictions, /api/v1/config
