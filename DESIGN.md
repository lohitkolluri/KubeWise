# KubeWise v2 — AI-Powered Predictive SRE

> Predict failures before they happen. Resolve them autonomously.

## Overview

KubeWise v2 is a complete rewrite of KubeWise in **Go**. It is an AI-driven SRE tool that deploys into Kubernetes clusters as a lightweight agent, continuously monitors cluster health, predicts failures before they occur, and autonomously remediates them — all controlled through a fast CLI.

### Design Principles

| Principle | How |
|-----------|-----|
| **Fast** | Sub-millisecond statistical detection, LLM only for context. Metrics scraped from existing Prometheus — no new scrape targets. |
| **Lightweight** | Single Go binary for the agent (~15MB). Zero external runtime deps (no CGO, no Python, no Node). ~50MB RAM idle. |
| **Simple to install** | `kubectl apply -f manifests/` for the agent, `brew install kwctl` for the CLI. |
| **Production-capable** | Handles large clusters via prometheus namespace scoping, K8s field selectors, and rate-limited remediation. |
| **Safe by default** | Guardrails engine prevents dangerous actions. Opt-in autonomy. Users control the blast radius. |
| **Accurate** | 3-tier prediction pipeline: statistical detection catches fast, pattern matching catches known signatures, LLM provides context-aware confidence. |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Kubernetes Cluster                                               │
│                                                                    │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │  KubeWise Agent (Deployment — 1 replica)                   │   │
│  │                                                              │   │
│  │  ┌──────────────────┐  ┌────────────────────────────────┐   │   │
│  │  │   Collector       │  │   Predictor                    │   │   │
│  │  │  ┌──────────────┐ │  │  ┌──────────┐ ┌─────────────┐ │   │   │
│  │  │  │ PromQL       │ │  │  │Tier 1:   │ │Tier 3:      │ │   │   │
│  │  │  │ (15-20 metr.)│ │  │  │Statistic.│ │LLM context  │ │   │   │
│  │  │  ├──────────────┤ │  │  │(EWMA,    │ │(OpenRouter) │ │   │   │
│  │  │  │ K8s Events   │ │  │  │ Z-score, │ └─────────────┘ │   │   │
│  │  │  │ (Delta watch)│ │  │  │ rate-of- │                 │   │   │
│  │  │  ├──────────────┤ │  │  │ change)  │                 │   │   │
│  │  │  │ K8s Resources│ │  │  ├──────────┤                 │   │   │
│  │  │  │ (State inf.) │ │  │  │Tier 2:   │                 │   │   │
│  │  │  └──────────────┘ │  │  │Patterns  │                 │   │   │
│  │  └──────────────────┘  │  └────────────────────────────┘   │   │
│  │                          │                                   │   │
│  │                          ▼                                   │   │
│  │  ┌────────────────────────────────────────────────────┐     │   │
│  │  │  Guardrails Engine                                  │     │   │
│  │  │  ┌───────────┐ ┌───────────┐ ┌───────────┐         │     │   │
│  │  │  │ Action    │ │ Namespace │ │ Rate      │ ┌─────┐ │     │   │
│  │  │  │ Allowlist │ │ Denylist  │ │ Limiter   │ │Dry  │ │     │   │
│  │  │  └───────────┘ └───────────┘ └───────────┘ │Run  │ │     │   │
│  │  │                                             └─────┘ │     │   │
│  │  └────────────────────────────────────────────────────┘     │   │
│  │                          │                                   │   │
│  │                          ▼                                   │   │
│  │  ┌────────────────────────────────────────────────────┐     │   │
│  │  │  Remediator                                        │     │   │
│  │  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌─────────────┐ │     │   │
│  │  │  │Restart │ │ Scale  │ │Rollback│ │ Annotate/   │ │     │   │
│  │  │  │Pod     │ │Deploy  │ │        │ │ Taint       │ │     │   │
│  │  │  └────────┘ └────────┘ └────────┘ └─────────────┘ │     │   │
│  │  └────────────────────────────────────────────────────┘     │   │
│  │                                                              │   │
│  │  ┌────────────────────────────────────────────────────┐     │   │
│  │  │  Store (bbolt + optional PVC)                       │     │   │
│  │  │  — Metrics ring buffer (15m window)                 │     │   │
│  │  │  — Anomaly records + predictions                    │     │   │
│  │  │  — Remediation history                              │     │   │
│  │  │  — Agent configuration cache                        │     │   │
│  │  └────────────────────────────────────────────────────┘     │   │
│  └────────────────────────────────────────────────────────────┘   │
│                                                                    │
│  ┌─────────────┐  ┌──────────────────────┐  ┌────────────────┐   │
│  │ Prometheus  │  │  K8s API Server      │  │  K8s Events    │   │
│  │ (existing)  │  │  (existing)           │  │  (existing)     │   │
│  └─────────────┘  └──────────────────────┘  └────────────────┘   │
│                                                                    │
└──────────────────────────────────────────────────────────────────┘
         ▲                            ▲
         │ port-forward / K8s API     │ kubectl apply
         │                            │
┌────────┴────────┐     ┌─────────────┴──────────────┐
│  kwctl CLI      │     │  Manifests / Helm Chart     │
│  Single binary   │     │  (for initial deploy)       │
└─────────────────┘     └────────────────────────────┘
```

### Data Flow

```
Prometheus ──▶ Collector ──▶ Predictor ──▶ Guardrails ──▶ Remediator ──▶ K8s API
    │              │             │              │              │
    │              │             │              │              └──▶ Anomaly Record
    │              │             │              │                   (bbolt)
    │              │             │              │
    │              │             │       ┌──────┴──────┐
    │              │             │       │  LLM (OpenRouter)
    │              │             │       │  - context analysis
    │              │             │       │  - confidence
    │              │             │       │  - recommendation
    │              │             │       └─────────────┘
    ▼              ▼             ▼
  (bbolt)       (bbolt)      (bbolt)
```

---

## Tech Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| **Language** | Go 1.22+ | Single binary, client-go, fast compile, easy cross-compile |
| **CLI framework** | [Cobra](https://github.com/spf13/cobra) + [pflag](https://github.com/spf13/pflag) | De facto standard Go CLI. Sub-commands, help, auto-completion |
| **Terminal UI** | [Bubbletea](https://github.com/charmbracelet/bubbletea) + [Lipgloss](https://github.com/charmbracelet/lipgloss) | Real-time `kwctl watch` dashboard. Beautiful, lightweight |
| **K8s client** | [client-go](https://github.com/kubernetes/client-go) | Official. Informers, leader election, RBAC |
| **Embedded DB** | [bbolt](https://github.com/etcd-io/bbolt) | Pure Go, embedded, zero deps, battle-tested in etcd |
| **AI/LLM** | OpenRouter HTTP API | One API key, 200+ models. Fast+cheap (Haiku, Flash) for real-time, capable (Sonnet, GPT-4o) for deep analysis |
| **Metrics** | Prometheus HTTP API over in-cluster DNS | Reads from existing Prometheus, no new scrape targets |
| **API** | HTTP (net/http) + optional gRPC | Simple REST for CLI ↔ agent communication |
| **Releases** | [GoReleaser](https://goreleaser.com) | Multi-platform (linux/darwin amd64/arm64), Homebrew tap, SBOM |
| **Testing** | kind + envtest | Local K8s clusters for integration tests |

### Constraint: Zero CGO

All dependencies must work without CGO. This means pure Go SQLite alternatives (bbolt, not mattn/go-sqlite3), pure Go DNS, etc.

---

## Project Structure

```
KubeWise/
├── DESIGN.md                  # This document
├── README.md                  # Project overview + quickstart
├── LICENSE
├── go.mod / go.sum
├── .goreleaser.yaml           # Multi-platform release config
│
├── cmd/
│   ├── kwctl/                 # CLI client entrypoint
│   │   └── main.go
│   └── agent/                 # Agent entrypoint (runs in cluster)
│       └── main.go
│
├── internal/
│   ├── agent/                 # Agent core
│   │   ├── agent.go           # Main agent loop
│   │   ├── collector/         # Data collection
│   │   │   ├── collector.go
│   │   │   ├── prometheus.go  # PromQL queries
│   │   │   ├── events.go      # K8s event watcher
│   │   │   └── resources.go   # K8s resource informers
│   │   ├── predictor/         # Prediction engine
│   │   │   ├── predictor.go   # Pipeline orchestrator
│   │   │   ├── statistical.go # EWMA, Z-score, rate-of-change
│   │   │   ├── patterns.go    # Pre-failure pattern matching
│   │   │   ├── llm.go         # OpenRouter client
│   │   │   └── types.go       # Prediction types
│   │   ├── guardrails/        # Safety engine
│   │   │   ├── guardrails.go
│   │   │   ├── allowlist.go
│   │   │   ├── ratelimit.go
│   │   │   └── policy.go
│   │   ├── remediator/        # Remediation executor
│   │   │   ├── remediator.go
│   │   │   ├── actions.go     # Scale, restart, rollback, etc.
│   │   │   └── verify.go      # Post-action verification
│   │   └── store/             # bbolt storage
│   │       ├── store.go
│   │       ├── migrations.go
│   │       ├── anomalies.go
│   │       └── config.go
│   │
│   ├── cli/                   # CLI implementation
│   │   ├── root.go            # Root command + shared flags
│   │   ├── status.go
│   │   ├── watch.go           # Bubbletea TUI
│   │   ├── anomalies.go
│   │   ├── predict.go
│   │   ├── exec.go
│   │   ├── config.go
│   │   └── logs.go
│   │
│   ├── api/                   # Agent HTTP API (for CLI)
│   │   ├── server.go
│   │   ├── routes.go
│   │   └── types.go
│   │
│   └── pkg/                   # Shared packages
│       ├── models/            # Domain types
│       │   ├── anomaly.go
│       │   ├── prediction.go
│       │   ├── remediation.go
│       │   └── config.go
│       ├── k8s/               # K8s helpers
│       │   └── client.go
│       └── llm/               # LLM / OpenRouter client
│           ├── client.go
│           ├── prompts.go
│           └── types.go
│
├── manifests/                 # K8s deployment manifests
│   ├── 00-namespace.yaml
│   ├── 10-serviceaccount.yaml
│   ├── 20-clusterrole.yaml
│   ├── 30-configmap.yaml
│   ├── 40-deployment.yaml
│   ├── 50-service.yaml
│   └── kustomization.yaml
│
├── hack/                      # Development scripts
│   ├── kind-cluster.sh        # Create kind cluster
│   ├── deploy-dev.sh          # Build + deploy to kind
│   └── test-e2e.sh            # End-to-end tests
│
└── test/
    ├── integration/           # Integration tests (kind)
    └── unit/                  # Unit tests
```

---

## Agent Components

### Collector

The collector gathers data from three sources in every scrape cycle (default: 30s):

**1. Prometheus metrics (15-20 queries)**
```
# Resource utilization
- pod_cpu_utilization_pct      # rate of CPU usage
- pod_memory_usage_bytes        # absolute memory
- pod_memory_working_set_bytes  # actual memory pressure

# Failure indicators
- pod_restart_rate              # restarts per minute
- pod_oomkilled                 # OOMKilled count
- pod_crashloopbackoff          # CrashLoopBackOff count
- pod_imagepullbackoff          # ImagePullBackOff count
- pod_not_ready                 # Not ready pods

# Node health
- node_memory_pressure          # MemoryPressure flag
- node_disk_pressure            # DiskPressure flag
- node_load                     # Load averages
- node_disk_usage_pct           # Disk usage %

# Deployment health
- deployment_replicas_unavailable
- deployment_rollout_status     # Progressing, degraded, etc.

# Network
- tcp_retransmit_rate
- dns_failure_rate              # CoreDNS error rate

# Custom metrics (user-configurable)
- <user_defined_promql_queries>
```

**2. K8s Events** (delta watch since last cycle):
- Warning events
- Failed mount / image pull events
- Probe failure events
- Node condition change events

**3. K8s Resource State** (informers):
- Pod phases (Pending, Running, Failed)
- Node conditions (Ready, MemoryPressure, DiskPressure)
- Deployment status (replicas, conditions)
- StatefulSet / DaemonSet status

### Predictor

Three-tier prediction pipeline executed every cycle:

#### Tier 1: Statistical (~1ms per metric)

| Algorithm | What it detects | Output |
|-----------|----------------|--------|
| **EWMA** | Gradual metric drift | Trend direction + slope |
| **Z-score** | Sudden deviation from baseline | Deviation score (0-1) |
| **Rate-of-change** | Acceleration toward threshold | Time-to-threshold estimate |
| **Combined** | Weighted fusion of above | Pre-failure score (0-1) |

The statistical models are maintained in-memory and updated with each scrape. They require no training data — they learn the baseline adaptively.

#### Tier 2: Pattern Matching (~3ms)

Known pre-failure signatures encoded as Go pattern matchers:

| Pattern | Input Signals | Prediction |
|---------|--------------|------------|
| `OOMRisk` | Memory ↑ + OOMKilled history | "Pod likely OOM in N min" |
| `CrashLoopRisk` | Restarts ↑ + probe failures | "CrashLoopBackOff imminent" |
| `Degradation` | Error rate ↑ + latency ↑ | "Service degradation" |
| `DiskFull` | Disk % ↑ + large pod | "PVC full in N hours" |
| `NodePressure` | MemPressure + critical pods | "Evictions likely" |
| `ImagePullRisk` | ImagePullBackOff + registry latency | "Image pull failures" |
| `RolloutFail` | Unavailable replicas + bad rollout | "Deployment rollout failed" |

Each pattern returns:
- Pattern name
- Confidence (0-1)
- Estimated time-to-failure
- Suggested remediation action

#### Tier 3: LLM Context Analysis (when score > threshold, ~500ms-2s)

When the combined anomaly score exceeds `prediction_threshold` (default: 0.7), the predictor packages recent context and sends it to OpenRouter.

**Context package sent to LLM:**
```json
{
  "cluster": "production-us-east",
  "namespace": "default",
  "entity": "pod/nginx-7d8f9c-5x2k1",
  "timeframe": "last 60 seconds",
  "metrics": {
    "cpu_util_pct": {"current": 85, "trend": "rising", "baseline": 45},
    "memory_mib": {"current": 420, "trend": "rising", "baseline": 256},
    "restart_rate": {"current": 0.5, "trend": "accelerating"},
    "error_rate": {"current": 0.12, "trend": "rising"}
  },
  "events": [
    {"type": "Warning", "reason": "BackOff", "count": 3, "time": "15s ago"},
    {"type": "Warning", "reason": "OOMKilling", "count": 1, "time": "5m ago"}
  ],
  "resource_state": {
    "phase": "Running",
    "restart_count": 5,
    "conditions": [{"type": "Ready", "status": "True"}]
  },
  "anomaly_score": 0.87,
  "patterns_matched": ["OOMRisk"]
}
```

**LLM response (structured):**
```json
{
  "prediction": {
    "failure_type": "OOMKill",
    "confidence": 0.85,
    "timeframe": "2-5 minutes",
    "reasoning": "Memory is rising at 50MiB/min with no limit configured. Previous OOMKill in history."
  },
  "remediation": {
    "suggested_action": "scale_deployment",
    "target": "deployment/nginx",
    "params": {"replicas": 3},
    "alternatives": [
      {"action": "annotate_pod", "params": {"memory_limit": "512Mi"}}
    ]
  }
}
```

**Default: fast/cheap model** (Claude 3 Haiku or Gemini Flash 2.0) for real-time predictions.
**Configurable**: user can set a different model for diagnosis vs prediction.

### Remediator

Executes remediation actions against the K8s API after guardrail approval:

| Action | What it does | Destructiveness | Guardrail |
|--------|-------------|----------------|-----------|
| `restart_pod` | Delete pod → recreate | Low | Must not be a standalone pod |
| `scale_deployment` | Increase replicas | Low | Max replica cap |
| `annotate_pod` | Add/update annotation | None | — |
| `set_resource_limits` | Patch pod resource limits | Low | Must not exceed node capacity |
| `rollback_deployment` | Rollback to previous revision | Medium | Requires rollout history |
| `drain_node` | Cordon + drain node | High | Requires confirmation flag |
| `delete_pvc` | Delete PVC to free space | High | Require confirm + namespace allow |

Each action:
1. Is parameterized (target, params, dry-run flag)
2. Has an idempotency check (don't restart the same pod twice in cooldown)
3. Has a verification step (confirm the action had the intended effect)
4. Is recorded in bbolt with timestamp, result, and human-readable log

---

## Guardrails System

Remediation is fully autonomous by default but constrained by a layered guardrail system:

### Layer 1: Action Allowlist
```
remediation:
  allowlist:
    - restart_pod
    - scale_deployment
    - set_resource_limits
    - annotate_pod
  # rollback_deployment and drain_node require explicit enable
```

### Layer 2: Namespace Denylist
```
remediation:
  namespace_denylist:
    - kube-system
    - kube-public
    - monitoring
    - kubewise
```

### Layer 3: Rate Limiter
```
remediation:
  rate_limit: 5                # max actions per minute
  per_namespace_rate_limit: 2  # max per namespace per minute
  cooldown_seconds: 120        # same entity cooldown
  max_parallel: 3
```

### Layer 4: Severity Gate
```
remediation:
  min_confidence: 0.8          # only remediate if LLM confidence >= 0.8
  pattern_min_score: 0.85      # only remediate on pattern match if score >= 0.85
  require_pattern_match: true  # LLM suggestion alone is not enough
```

### Layer 5: Safety Mode
```
remediation:
  mode: "autonomous"           # or "suggest" (log only, no execution)
  dry_run: false               # true = log what would happen, don't do it
```

### Layer 6: Pre-action Validation
Each remediation action is validated before execution:
- `restart_pod`: is pod managed by a ReplicaSet/StatefulSet? (otherwise restart is meaningless)
- `scale_deployment`: would new replicas exceed max? is HPA managing this?
- `drain_node`: are there DaemonSet-managed pods? PDBs respected?
- `delete_pvc`: is PVC bound? is pod using it?

---

## CLI (`kwctl`)

### Commands

```
Usage:
  kwctl [command]

Available Commands:
  status          Show agent health and active predictions
  watch           Real-time dashboard (Bubbletea TUI)
  anomalies       List and describe anomalies
  predict         Show current predictions
  exec            Manually trigger remediation
  config          View/update agent configuration
  logs            Stream agent logs
  completion      Generate shell completion

Flags:
  --kubeconfig    Path to kubeconfig (default: ~/.kube/config)
  --context       K8s context to use
  --agent-namespace  Namespace where agent is deployed (default: kubewise)
  --agent-service    Agent service name (default: kubewise-agent)
  -o, --output    Output format: table, json, yaml (default: table)
```

### Key Commands in Detail

**`kwctl watch`** — Real-time Bubbletea TUI:
```
┌─────────────────────────────────────────────────────────────┐
│  KubeWise v2 — Live Dashboard               [Ctrl+C to exit]│
├─────────────────────────────────────────────────────────────┤
│  Clusters: 1 │  Pods: 247 │  Nodes: 12 │  Uptime: 3h 12m   │
├─────────────────────────────────────────────────────────────┤
│  ACTIVE PREDICTIONS (2)                                      │
│  ├─ OOMRisk    pod/nginx-7d8f9c-5x2k1   92%  ~3min  ⚡     │
│  └─ Degradation svc/api-gateway-0       74%  ~8min  ◐     │
│                                                             │
│  RECENT REMEDIATIONS (3)                                    │
│  ├─ ✅ restarted pod/metrics-5f9c2   2m ago                  │
│  ├─ ✅ scaled deployment/nginx       5m ago                  │
│  └─ ⏭️ drain node/ip-10-0-1-42      12m ago (skipped-grdl) │
│                                                             │
│  TIMELINE                                                    │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ ████║██████████║████████████████║███                 │   │
│  │ 14:00        14:30        15:00        15:30         │   │
│  └──────────────────────────────────────────────────────┘   │
│  █ Active predictions   █ Remediations    █ LLM calls       │
└─────────────────────────────────────────────────────────────┘
```

**`kwctl predict`** — Current predictions:
```
$ kwctl predict

PREDICTIONS
┌──────────┬────────────────────────────────┬────────┬────────┬──────────┐
│ TYPE     │ ENTITY                         │ CONFID │  ETA   │ ACTION   │
├──────────┼────────────────────────────────┼────────┼────────┼──────────┤
│ OOMRisk  │ pod/nginx-7d8f9c-5x2k1         │   92%  │  3 min │ scale    │
│ Degrad.  │ svc/api-gateway-0              │   74%  │  8 min │ verify   │
│ DiskFull │ pvc/data-pod-3                 │   65%  │  2 hrs │ monitor  │
└──────────┴────────────────────────────────┴────────┴────────┴──────────┘

$ kwctl predict --watch    # Tail new predictions in real-time
```

**`kwctl anomalies describe <id>`** — Deep dive:
```
$ kwctl anomalies describe a7f3d2

ANOMALY a7f3d2
──────────────────────────────────────────────────────────
  Entity:      pod/nginx-7d8f9c-5x2k1
  Detected:    2026-07-06 14:32:18 UTC (3m ago)
  Score:       0.92 (statistical: 0.87, pattern: 0.95)
  Status:      remediated ✓

PREDICTION
  Failure:     OOMKill
  Confidence:  85%
  Timeframe:   2-5 minutes (from detection)
  Reasoning:   Memory rising 50MiB/min, no limit set,
               previous OOMKill in history

REMEDIATION
  Action:      scale deployment/nginx → 3 replicas
  Executed:    14:32:22 UTC
  Result:      ✓ verification passed
  Outcome:     Memory pressure reduced, prediction averted

METRICS (last 5 min)
  ┌─────────────────────────────────────────────────────┐
  │  Mem   ▄▂▃▅▇███▇▇█▇                               │
  │  CPU   ▃▃▄▄▅▅▆██████▇▆                             │
  │  Rst   ▁▁▁▁▁▁▁▁▁▁▁    (post-remediation)            │
  └─────────────────────────────────────────────────────┘
```

---

## Configuration

The agent is configured via a Kubernetes ConfigMap:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: kubewise-config
  namespace: kubewise
data:
  config.yaml: |
    # --- Collection ---
    scrape_interval: 30s            # How often to collect metrics
    prometheus_address: "http://prometheus-server.monitoring:9090"
    custom_promql_queries:
      my_custom_metric: 'rate(my_metric_total[5m])'

    # --- Prediction ---
    prediction_threshold: 0.7       # Score threshold for LLM analysis
    anomaly_baseline_window: 15m    # Window for statistical baseline
    
    # --- LLM / OpenRouter ---
    llm:
      provider: openrouter
      model: "anthropic/claude-3-haiku"  # Fast model for real-time
      diagnosis_model: "anthropic/claude-3.5-sonnet"  # Capable model for deep analysis
      temperature: 0.1
      max_tokens: 1024

    # --- Remediation ---
    remediation:
      mode: "autonomous"           # autonomous | suggest | off
      dry_run: false
      allowlist:
        - restart_pod
        - scale_deployment
        - set_resource_limits
      namespace_denylist:
        - kube-system
        - kube-public
      rate_limit: 5
      per_namespace_rate_limit: 2
      cooldown_seconds: 120
      max_parallel: 3
      min_confidence: 0.8
      require_pattern_match: true
      destructive_actions_require:
        - drain_node
        - delete_pvc
        - scale_to_zero

    # --- Notification ---
    notifications:
      enabled: true
      webhook_url: ""              # Slack/Teams/Discord webhook
```

Environment variables override ConfigMap values:
| Variable | Overrides |
|----------|-----------|
| `KUBEWISE_LLM_API_KEY` | OpenRouter API key (from Secret, not ConfigMap) |
| `KUBEWISE_LOG_LEVEL` | Log verbosity |
| `KUBEWISE_DRY_RUN` | Force dry-run mode |
| `KUBEWISE_REMEDIATION_MODE` | Force mode override |

---

## Installation

### Prerequisites
- Kubernetes cluster 1.24+
- Prometheus installed (for metrics)
- `kubectl` configured

### Install the CLI

```bash
# macOS / Linux (Homebrew)
brew install lohitkolluri/tap/kwctl

# Linux / macOS (curl one-liner)
curl -sfL https://raw.githubusercontent.com/lohitkolluri/KubeWise/v2/hack/install.sh | sh

# Go users
go install github.com/lohitkolluri/KubeWise/v2/cmd/kwctl@latest

# Docker
docker pull ghcr.io/lohitkolluri/kubewise/kwctl:latest
```

> **Note:** The `kubewise.dev` domain is aspirational. Early releases use GitHub raw content URLs until the project site is set up.

### Deploy the Agent

```bash
# Quick start
kubectl create namespace kubewise
kubectl create secret generic kubewise-secret \
  --namespace kubewise \
  --from-literal=openrouter-api-key="sk-or-v1-..."

kubectl apply -f https://raw.githubusercontent.com/lohitkolluri/KubeWise/v2/manifests/install.yaml

# Verify
kwctl status
```

Or with Helm (coming soon):

```bash
helm repo add kubewise https://raw.githubusercontent.com/lohitkolluri/KubeWise/v2/charts
helm install kubewise kubewise/kubewise \
  --set llm.apiKey="sk-or-v1-..."
```

---

## Performance Targets

| Metric | Target | Notes |
|--------|--------|-------|
| **Scrape cycle** | 30s | Configurable 10-120s |
| **Statistical prediction latency** | <10ms per cycle | In-memory models, no I/O |
| **Pattern matching latency** | <5ms per cycle | Deterministic pattern checks |
| **LLM analysis latency** | ~500ms-2s | Depends on OpenRouter model |
| **End-to-end predict→remediate** | <5s typical | Including guardrail checks |
| **Agent memory** | ~50MB idle, ~150MB peak | bbolt + in-memory metrics window |
| **Agent CPU** | ~0.1 core average | PromQL queries are heavier than local computation |
| **CLI binary size** | ~15MB compressed | Single Go binary |
| **Agent binary size** | ~20MB compressed | Includes collector + predictor + remediator |

### Accuracy Targets

| Metric | Target | Measurement |
|--------|--------|-------------|
| **Prediction precision** | >85% | (true positives) / (true + false positives) |
| **Prediction recall** | >75% | (true positives) / (actual failures) |
| **Mean time to prediction** | <5 min before failure | Average lead time |
| **False positive rate** | <15% | False alarms per total predictions |
| **Remediation success** | >90% | Remediation that averted the predicted failure |

---

## Development

### Prerequisites
- Go 1.22+
- Docker + kind (for integration tests)
- OpenRouter API key (for LLM features)

### Quick Start

```bash
# Clone
git clone https://github.com/lohitkolluri/KubeWise.git
cd KubeWise
git checkout v2

# Build agent + CLI
go build -o bin/kwctl ./cmd/kwctl/
go build -o bin/agent ./cmd/agent/

# Create kind cluster with Prometheus
./hack/kind-cluster.sh

# Build + deploy to kind
./hack/deploy-dev.sh

# Run CLI against it
./bin/kwctl status

# Test
go test ./...
```

### Testing Strategy

| Test Type | Tool | What it covers |
|-----------|------|----------------|
| **Unit tests** | `go test` | Predictor algorithms, guardrails logic, store operations |
| **Integration tests** | kind + envtest | Full cycle: collect → predict → guardrails → remediate |
| **E2E tests** | kind + Prometheus | Deploy agent, simulate failures, verify predictions + remediation |
| **Chaos tests** | kind + chaos-mesh | Verify prediction accuracy under real failure conditions |

### kind Cluster Setup

```bash
# hack/kind-cluster.sh
#!/bin/bash
set -euo pipefail

CLUSTER_NAME="kubewise-dev"

# Create cluster
kind create cluster --name $CLUSTER_NAME --config - <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
  - role: worker
EOF

# Install Prometheus
kubectl create namespace monitoring
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring

# Deploy KubeWise agent
kubectl create namespace kubewise
kubectl create secret generic kubewise-secret \
  --namespace kubewise \
  --from-literal=openrouter-api-key="${OPENROUTER_API_KEY:-}"

kubectl apply -k manifests/
```

---

## Roadmap

### Phase 1 (v2.0.0-alpha) — Foundation
- [ ] Go project scaffolding: cmd, internal, manifests
- [ ] bbolt store with schema
- [ ] Collector: PromQL, K8s events, K8s resource informers
- [ ] Statistical predictor (EWMA, Z-score, rate-of-change)
- [ ] Static pattern matcher (OOMRisk, CrashLoopRisk)
- [ ] CLI: `status`, `config`, `predict`
- [ ] Agent HTTP API
- [ ] Deployment manifests
- [ ] kind dev environment + e2e test

### Phase 2 (v2.0.0-beta) — AI + Remediation
- [ ] OpenRouter LLM integration
- [ ] LLM context analysis (Tier 3 prediction)
- [ ] Guardrails engine with all 6 layers
- [ ] Remediator: restart, scale, annotate actions
- [ ] CLI: `watch` (Bubbletea TUI), `anomalies`, `exec`
- [ ] Homebrew tap + GoReleaser
- [ ] Documentation

### Phase 3 (v2.0.0) — Production
- [ ] Helm chart
- [ ] Prometheus rules for prediction metrics (export agent metrics)
- [ ] Notification webhooks (Slack, Discord, Teams)
- [ ] Benchmarking and performance tuning
- [ ] Chaos testing against kind
- [ ] GitHub Actions CI/CD

### Phase 4 (v2.1+) — Advanced
- [ ] Custom user-defined prediction patterns
- [ ] Multi-cluster support (one CLI, many clusters)
- [ ] Historical model training on past incidents
- [ ] Plugin system for custom collectors/remediators
- [ ] Prometheus-compatible metrics export from agent itself
- [ ] Grafana dashboard
- [ ] Incident report generation (LLM-summarized)

---

## Design Decisions

### Why not an operator with CRDs?
Operators solve "reconcile desired state" problems. KubeWise's core is monitoring + prediction + remediation — a fundamentally different pattern. Using a standard Deployment with ConfigMap config is simpler, faster to iterate, and avoids CRD registration complexity. If a K8s-native controller pattern is needed later, leader election via leases still works without CRDs.

### Why bbolt instead of SQLite?
bbolt is pure Go, embedded, and battle-tested in etcd. SQLite requires CGO (mattn/go-sqlite3) or a pure Go port (modernc.org/sqlite) that has trade-offs. For KubeWise's needs — key-value storage with bucket-based organization — bbolt maps directly to the problem: metrics ring buffer, anomaly records, remediation history. No complex queries needed.

### Why OpenRouter instead of a single provider?
One API key grants access to 200+ models. This lets KubeWise use cheap fast models (Haiku at ~$0.25/M tokens) for real-time prediction and capable models (Sonnet at ~$3/M tokens) for deep diagnosis — all through the same client. Users can switch models without changing code. If they prefer OpenAI or Gemini directly, the OpenRouter client can be swapped for a direct provider client.

### Why not run an LLM locally?
Local models (via Ollama/llama.cpp) require GPU or significant CPU resources — antithetical to "lightweight." OpenRouter gives instant access to models that outperform local models at a fraction of the operational cost. For users who need air-gapped deployments, a local model integration can be added as an optional provider later.

### Why Bubbletea for the TUI?
Bubbletea is an Elm-architecture TUI framework that's lightweight, testable, and produces beautiful terminal interfaces. For `kwctl watch`, it enables real-time updates without the bloat of a web dashboard. The binary stays a single file with no runtime dependencies.
