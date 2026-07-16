# KubeWise Architecture

This document describes the KubeWise system architecture across four views:

1. **System Overview** — high-level component diagram and data flow
2. **Detection Pipeline** — how metrics become anomaly predictions
3. **Remediation Pipeline** — how anomalies become automated fixes
4. **Deployment Architecture** — how the system runs inside Kubernetes

---

## 1. System Overview

```mermaid
%%{init: {
  "theme": "base",
  "themeVariables": {
    "background": "#ffffff",
    "mainBkg": "#ffffff",
    "nodeBorder": "#e2e8f0",
    "clusterBkg": "#f8fafc",
    "clusterBorder": "#cbd5e1",
    "titleColor": "#1e293b",
    "edgeLabelBackground": "#ffffff",
    "nodeTextColor": "#334155"
  },
  "flowchart": {
    "curve": "stepBefore",
    "nodeSpacing": 100,
    "rankSpacing": 80,
    "htmlLabels": true,
    "padding": 20
  }
}}%%
flowchart TD
  subgraph k8s["☸️  Kubernetes Cluster"]
    subgraph sources["📡  Data Sources"]
      prom["Prometheus /<br/>VictoriaMetrics"]:::source
      k8sapi["K8s API Server"]:::source
    end

    subgraph agent["🤖  KubeWise Agent"]
      cfg["⚙️  Config / Secrets"]:::config
      collector["Collector"]:::core
      predictor["Predictor"]:::core
      gate["Anomaly Gate"]:::core
      store["💾  BoltDB"]:::storage
      ruleEngine["Rule Engine"]:::engine
      correlator["Correlator"]:::llm
      executor["K8s Executor"]:::core
      forecasterClient["Forecaster<br/>gRPC"]:::core
      api["HTTP API"]:::api
    end

    subgraph obs["🔭  Observability"]
      vm["VictoriaMetrics"]:::obs
      vl["VictoriaLogs<br/>/ Loki"]:::obs
      tempo["Grafana Tempo"]:::obs
      alloy["Grafana Alloy"]:::obs
      grafana["Grafana"]:::obs
    end
  end

  subgraph external["🌐  External Services"]
    llm["LLM Providers"]:::external
    notify["Notifiers"]:::external
    cli["kwctl CLI / TUI"]:::external
  end

  prom --> collector
  k8sapi --> collector
  cfg -.-> collector
  collector --> predictor
  forecasterClient -.-> predictor
  predictor --> gate
  gate --> store
  store --> ruleEngine
  store --> correlator
  ruleEngine --> executor
  correlator -.-> llm
  correlator --> executor
  store --> api
  api --> cli
  store -.-> notify

  alloy --> vl
  alloy --> tempo
  vm --> grafana
  vl --> grafana
  tempo --> grafana

  classDef core fill:#e0e7ff,stroke:#4f46e5,color:#1e1b4b,stroke-width:2px
  classDef storage fill:#d1fae5,stroke:#059669,color:#064e3b,stroke-width:2px
  classDef engine fill:#fef3c7,stroke:#d97706,color:#78350f,stroke-width:2px
  classDef llm fill:#ede9fe,stroke:#7c3aed,color:#3b0764,stroke-width:2px
  classDef api fill:#ccfbf1,stroke:#0d9488,color:#134e4a,stroke-width:2px
  classDef config fill:#ffedd5,stroke:#ea580c,color:#7c2d12,stroke-width:2px,stroke-dasharray: 5 3
  classDef source fill:#f1f5f9,stroke:#64748b,color:#0f172a,stroke-width:2px
  classDef external fill:#fee2e2,stroke:#dc2626,color:#7f1d1d,stroke-width:2px
  classDef obs fill:#f8fafc,stroke:#94a3b8,color:#334155,stroke-width:2px,stroke-dasharray: 4 4
```

**Data flow (30-second cycle):**
1. Collector scrapes PromQL metrics + watches K8s resources
2. Predictor runs 3 statistical strategies (Z-score, Changepoint, CombinedOR) + 3 pattern matchers
3. Anomaly Gate filters noise using score-cooldown-per-entity dedup
4. Valid anomalies persist to BoltDB
5. Correlator loads open anomalies → rule engine fast-path (8 rules) → LLM investigation → plan generation → tier assignment
6. Executor applies approved remediation (restart/scale/patch/exec)
7. HTTP API exposes all data to the CLI and external consumers

---

## 2. Detection Pipeline

```mermaid
%%{init: {"flowchart": {"curve": "stepAfter", "nodeSpacing": 70, "rankSpacing": 60}}}%%
flowchart LR
  subgraph collection["📡 Collection (30s Interval)"]
    direction TB
    promQL["17 PromQL Queries<br/>pod_cpu · pod_memory · restart ·<br/>crash · oom · node · tcp · dns<br/>network · cpu_throttle · ready ·<br/>not_ready · imagepull · deployment"]:::collect
    resources["K8s Resource Informers<br/>Pods · Nodes · Deployments"]:::collect
    events["K8s Event Watchers<br/>Failure-related Events"]:::collect
  end

  subgraph detection["🔬 Statistical Detection"]
    direction TB
    metricProfile["Metric->Strategy Router<br/>15 Prefix-based Profiles"]:::detect
    subgraph strategies["Detection Strategies"]
      rz["Robust Z-Score<br/>Point anomalies (CPU spikes)"]:::detect
      cp["Changepoint (BOCPD)<br/>Regime shifts (memory leaks)"]:::detect
      or["Combined OR<br/>Mixed patterns (network)"]:::detect
    end
    scoring["Scorer<br/>Hoeffding Bounds · ROC Boost<br/>Adaptive Median"]:::detect
    patterns["Pattern Matchers<br/>OOM · CrashLoop · Degradation"]:::detect
    forecaster["Forecaster Sidecar<br/>12-step ETS (gRPC)"]:::detect
  end

  subgraph gating["🚦 Anomaly Gating"]
    direction TB
    gate["AnomalyGate<br/>Per-entity-per-metric score-cooldown<br/>24h expiry · Observe-only mode"]:::gate
    gateStats["Stats<br/>Passed · Dropped · Observed"]:::gate
  end

  subgraph persistence["💾 Persistence"]
    direction TB
    store[("BoltDB<br/>18 Buckets")]:::storage
    metricsStore["Metrics Ring Buffer<br/>10,080 samples/metric<br/>Time+Status Indexes"]:::storage
    anomalyStore["Anomaly Records<br/>CRUD + Status Lifecycle<br/>detected→active→correlated→...→resolved"]:::storage
  end

  promQL --> metricProfile
  metricProfile --> rz & cp & or
  resources -.-> patterns
  events -.-> patterns
  rz & cp & or --> scoring
  scoring --> gate
  patterns --> gate
  forecaster -. "12-step forecast<br/>comparison" .-> scoring
  gate --> gateStats
  gate ==> anomalyStore
  scoring -. "Time-series data" .-> metricsStore

  classDef collect fill:#eef2ff,stroke:#4f46e5,color:#1e3a8a,stroke-width:2px
  classDef detect fill:#f3e8ff,stroke:#9333ea,color:#581c87,stroke-width:2px
  classDef gate fill:#fef3c7,stroke:#f59e0b,color:#92400e,stroke-width:2px
  classDef storage fill:#f0fdf4,stroke:#16a34a,color:#14532d,stroke-width:2px
```

**Metric routing strategy** (15 prefix entries):

| Prefix | Strategy | Min Score | Persistence |
|--------|----------|-----------|-------------|
| `pod_cpu_` | Robust Z-Score | 0.65 | 2 scrapes |
| `pod_memory_` | Changepoint | 0.50 | 2 |
| `restart_`, `crash`, `oom` | Changepoint (low threshold) | 0.30-0.40 | 1 |
| `node_` | Changepoint | 0.50 | 2 |
| `tcp_`, `dns_`, `network_` | Combined OR | 0.60 | 2 |
| `cpu_throttle` | Robust Z-Score | 0.50 | 2 |
| `pod_ready_`, `pod_not_ready` | Changepoint | 0.50 | 2 |
| `imagepull`, `deployment_` | Changepoint | 0.50 | 1-2 |
| *(default)* | Robust Z-Score | *(config default)* | *(default)* |

---

## 3. Remediation Pipeline

```mermaid
%%{init: {"flowchart": {"curve": "stepAfter", "nodeSpacing": 70, "rankSpacing": 60}}}%%
flowchart LR
  subgraph input["📥 Input"]
    anomalies("Open Anomalies<br/>detected · active · correlated"):::input
    config("RemediationConfig<br/>mode · dryRun · allowlist<br/>denylist · minConfidence"):::input
  end

  subgraph evaluate["⚡ Rule Engine (Fast Path)"]
    direction TB
    rules["8 Deterministic Rules"]:::engine
    ruleOOM["OOM → restart_pod · T1 · 0.98"]:::engine
    ruleCL["CrashLoop → restart_pod · T1 · 0.95"]:::engine
    ruleIPB["ImagePullBackOff → escalate · T3 · 0.97"]:::engine
    ruleNN["NodeNotReady → escalate · T3 · 0.90"]:::engine
    rulePend["Pending (≥5m) → escalate · T3 · 0.85"]:::engine
    ruleRR["ReadyRatio (<0.5) → scale · T2 · 0.80"]:::engine
    ruleCT["CPUThrottle (>50%) → patch · T2 · 0.75"]:::engine
    ruleMP["MemoryPressure → escalate · T3 · 0.85"]:::engine
  end

  subgraph llm_path["🧠 LLM Path"]
    direction TB
    dedup["Semantic Cache<br/>Hash-based dedup"]:::llm
    router["LLM Router<br/>T1: mistral-nemo<br/>T2: deepseek-v4<br/>T3: qwen3-coder<br/>T4: gpt-oss-120b"]:::llm
    investigation["Investigation<br/>kubectl describe · logs · events<br/>Loki query · Tempo trace"]:::llm
    diagnosis["LLM Diagnosis<br/>Root cause · Severity · Evidence"]:::llm
    plan["Plan Generation<br/>RemediationPlan struct<br/>Steps · Verification"]:::llm
    normalize["Plan Normalization<br/>Runbook validation · Namespace check<br/>Protected namespace enforcement"]:::llm
  end

  subgraph tiers["🎯 Risk Tiers"]
    direction TB
    t1["T1 · Auto-execute<br/>restart_pod · delete_pod"]:::tier
    t2["T2 · Cooldown-gated<br/>scale · rollback · patch"]:::tier
    t3["T3 · Human Approval<br/>escalate · cluster-wide"]:::tier
    t4["T4 · Always Rejected<br/>unknown · destructive"]:::tier
  end

  subgraph execution["⚙️ Execution"]
    direction TB
    dryRun["Dry-Run Check<br/>Log only · No K8s changes"]:::exec
    approval["Approval Gate<br/>ListPending · Approve · Reject"]:::exec
    k8sExec["K8s Executor<br/>restart · scale · patch · exec"]:::exec
    verify["Verification<br/>Post-remediation health poll"]:::exec
    audit[("Audit Record<br/>Prompt · Response · Result · Verdict")]:::storage
  end

  anomalies ==> evaluate
  anomalies -. "LLM-only path<br/>(feature gate off)" .-> dedup
  config --- evaluate
  config --- llm_path

  evaluate --> dedup
  dedup --> router
  router --> investigation
  investigation --> diagnosis
  diagnosis --> plan
  plan --> normalize

  normalize --> t1 & t2 & t3 & t4
  t1 --> dryRun
  t2 --> dryRun
  t3 --> approval
  t4 --> stop((Rejected))

  dryRun --> approval
  approval --> k8sExec
  k8sExec --> verify
  verify ==> audit

  classDef input fill:#eef2ff,stroke:#4f46e5,color:#1e3a8a,stroke-width:2px
  classDef engine fill:#fef3c7,stroke:#f59e0b,color:#92400e,stroke-width:2px
  classDef llm fill:#f3e8ff,stroke:#9333ea,color:#581c87,stroke-width:2px
  classDef tier fill:#fce7f3,stroke:#ec4899,color:#831843,stroke-width:2px
  classDef exec fill:#ecfeff,stroke:#0891b2,color:#164e63,stroke-width:2px
  classDef storage fill:#f0fdf4,stroke:#16a34a,color:#14532d,stroke-width:2px
```

**Rule engine — 8 built-in rules:**

| Rule | Action | Tier | Confidence | Needs LLM |
|------|--------|------|-----------|-----------|
| OOM | `restart_pod` | T1 | 0.98 | No |
| CrashLoopBackOff | `restart_pod` | T1 | 0.95 | No |
| ImagePullBackOff | `escalate` | T3 | 0.97 | No |
| NodeNotReady | `escalate` | T3 | 0.90 | No |
| Pending (≥5 min) | `escalate` | T3 | 0.85 | No |
| ReadyRatio (<50%) | `scale_replicas` | T2 | 0.80 | No |
| CPUThrottle (>50%) | `patch_resources` | T2 | 0.75 | Yes |
| MemoryPressure | `escalate` | T3 | 0.85 | No |

---

## 4. Deployment Architecture

```mermaid
%%{init: {"flowchart": {"curve": "stepBefore", "nodeSpacing": 80, "rankSpacing": 70}}}%%
flowchart TD
  subgraph cluster["☸️ Kubernetes Cluster"]
    subgraph ns["📦 Namespace: kubewise"]
      subgraph agentPod["Agent Pod (Recreate Strategy)"]
        direction TB
        agentContainer["agent (port 8080)<br/>Distroless Static<br/>readOnlyRootFS · non-root<br/>env: OPENROUTER_API_KEY<br/>env: KUBEWISE_API_TOKEN<br/>env: 7x KUBEWISE_FEATURE_*"]:::container
        forecasterContainer["forecaster (port 50051)<br/>Python 3.12-slim<br/>gRPC Server<br/>statsmodels ETS"]:::container
        configVol["ConfigMap Volume<br/>mount: /etc/kubewise/config.yaml<br/>readonly"]:::volume
        dataVol[("PVC Volume<br/>mount: /var/lib/kubewise<br/>BoltDB · 10Gi")]:::volume
        tmpVol["emptyDir Volume<br/>mount: /tmp<br/>forecaster temp"]:::volume
      end

      subgraph infra["Supporting Resources"]
        sa["ServiceAccount<br/>kubewise-agent"]:::infra
        clusterRole["ClusterRole / Role<br/>pods · deployments · nodes<br/>events · logs · exec"]:::infra
        svc["Service (ClusterIP:8080)<br/>kwctl port-forward target"]:::infra
        cm["ConfigMap<br/>config.yaml"]:::infra
        secret["Secret<br/>openrouter_api_key<br/>api_token · client_password_hash"]:::infra
        pvcClaim["PersistentVolumeClaim<br/>10Gi · RWO · BoltDB"]:::infra
      end
    end

    subgraph observability["🔭 Observability (Optional)"]
      vmSvc["VictoriaMetrics<br/>metrics endpoint"]:::obs
      vlSvc["VictoriaLogs<br/>logs endpoint"]:::obs
      tempoSvc["Grafana Tempo<br/>traces endpoint"]:::obs
      alloyDS["Grafana Alloy<br/>DaemonSet · log collection"]:::obs
    end

    promSvc("Prometheus / VM<br/>Service"):::external
  end

  subgraph outside["🌐 Outside Cluster"]
    kwctl["kwctl CLI<br/>kwctl up (port-forward)<br/>kwctl ui (TUI)<br/>kwctl install"]:::external
    llmProviders["LLM Providers<br/>OpenRouter · Ollama · OpenAI"]:::external
  end

  agentContainer --> configVol
  agentContainer ==> dataVol
  agentContainer --> tmpVo
  forecasterContainer --> tmpVol
  agentContainer -. "gRPC localhost:50051" .-> forecasterContainer

  sa --- clusterRole
  agentContainer --- sa
  svc -. "port 8080" .-> agentContainer
  agentContainer -. "reads" .-> cm
  agentContainer -. "reads" .-> secret
  dataVol --> pvcClaim

  promSvc -. "PromQL queries" .-> agentContainer
  agentContainer -. "LLM API calls" .-> llmProviders
  kwctl -. "HTTP API" .-> svc

  vmSvc -. "if VM subchart enabled" .-> promSvc
  vlSvc -. "log queries" .-> agentContainer
  tempoSvc -. "trace queries" .-> agentContainer
  alloyDS -. "collects pod logs" .-> vlSvc
  alloyDS -. "collects traces" .-> tempoSvc

  classDef container fill:#eef2ff,stroke:#4f46e5,color:#1e3a8a,stroke-width:2px
  classDef volume fill:#f0fdf4,stroke:#16a34a,color:#14532d,stroke-width:2px
  classDef infra fill:#f8fafc,stroke:#94a3b8,color:#334155,stroke-width:1px
  classDef obs fill:#f1f5f9,stroke:#94a3b8,color:#475569,stroke-width:1px,stroke-dasharray: 3 3
  classDef external fill:#fef2f2,stroke:#dc2626,color:#991b1b,stroke-width:1px
```

**Key deployment notes:**
- **Recreate strategy** — required because BoltDB holds an exclusive file lock on the RWO PVC. Rolling update would deadlock.
- **Non-root containers** — both agent and forecaster run without privilege escalation.
- **Feature flags** — all 7 flags are injected as environment variables from Helm values.
- **Observability subcharts** — VictoriaMetrics, VictoriaLogs, Tempo, and Alloy deploy automatically via Helm dependencies when `agent.observability.*.enabled=true`.
- **Port-forward** — `kwctl up` creates a local port-forward to the agent service for CLI/TUI access.
