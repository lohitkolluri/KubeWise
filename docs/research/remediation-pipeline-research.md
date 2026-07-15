# KubeWise Remediation Pipeline — Research & Recommendations

> **Date:** 2026-07-16  
> **Scope:** 6 dimensions of the auto-remediation pipeline  
> **Methodology:** Codebase analysis + parallel web research across OSS projects

---

## Overview

KubeWise has a working remediation pipeline with LLM-driven correlation, risk tiering, sequential runbook execution, tool plugins, and post-remediation verification. This document evaluates alternatives and improvements across 6 dimensions, rating each as:

- **Must Have** — significant gap, high ROI, aligns with roadmap
- **Nice to Have** — meaningful improvement, lower urgency
- **Skip** — over-engineering for current needs

---

## Q1: Tool Plugin Architecture

### Current Approach

**File:** `internal/tools/plugin.go`, `internal/tools/registry.go`

Custom `ToolPlugin` interface with 4 methods (`Name`, `Capabilities`, `Validate`, `Execute`) and a global `Registry` that maps tool names to instances. Each plugin makes raw `client-go` calls.

```go
type ToolPlugin interface {
    Name() string
    Capabilities() []string
    Validate(action models.ToolAction) error
    Execute(ctx context.Context, action models.ToolAction) (*models.ToolResult, error)
}
```

### Alternatives Researched

#### A. StackStorm Packs & Actions

StackStorm's unit of automation is a **Pack** — a versioned, shareable bundle of actions, workflows, rules, and sensors. Actions are defined via YAML metadata + executable scripts (Python, PowerShell, remote SSH via `mistral`).

**Key patterns:**
- **Action metadata** (`actions/my_action.yaml`) defines parameters, runner type, timeout, output schema
- **Action runners** abstract execution model (local, remote, HTTP, cloud)
- **Rule engine** (trigger + criteria + action) wires events to actions declaratively
- **Datastore** for shared state between actions/sensors
- **Webhooks** for external triggers

```yaml
# StackStorm action metadata example
name: "scale_deployment"
runner_type: "python-script"
parameters:
  deployment:
    type: "string"
    required: true
  replicas:
    type: "integer"
    required: true
  namespace:
    type: "string"
    default: "default"
```

**Comparison to KubeWise:**

| Aspect | KubeWise | StackStorm |
|--------|----------|------------|
| Plugin def | Go interface + code | YAML metadata + scripts |
| Execution | Direct Go function call | Runner abstraction (many types) |
| Parameters | ToolAction struct | Declared in YAML schema |
| State | None | Datastore (KV + TTL) |
| Distribution | None | Pack registry + versioning |
| Trigger binding | LLM decides | Rule engine + sensors |

#### B. Crossplane Composition Functions

Crossplane uses **Composition Functions** — gRPC servers that any language can implement — to template infrastructure. Functions receive a `FunctionIO` request and return desired state.

**Key pattern:** Functions are standalone gRPC servers (not linked code). The composition engine calls them in a pipeline. Functions can read cross-resource references, make external API calls, and conditionally generate resources.

```go
// Crossplane Function interface (simplified)
func (f *Function) RunFunction(ctx context.Context, req *fnv1beta1.RunFunctionRequest) (*fnv1beta1.RunFunctionResponse, error) {
    // Read desired state from request
    // Add/transform resources in response
    // Return errors for rejection
}
```

**Comparison:** KubeWise plugins are in-process, Crossplane functions are out-of-process gRPC servers. Crossplane's model is superior for polyglot teams but adds gRPC overhead. Not a good fit for KubeWise's single-binary model.

#### C. Robusta Playbooks

Robusta uses **Playbooks** — Python code decorated with triggers — for Kubernetes auto-remediation. Playbooks are discovered at startup, bound to Prometheus alerts or Kubernetes events via triggers, and can emit findings (changes, diffs, enrichment).

```python
# Robusta playbook example
@playbook
def scale_up_on_high_cpu(alert: PrometheusAlert):
    if alert.severity == "critical":
        deployment = alert.get_deployment()
        deployment.scale(replicas=deployment.replicas + 1)
        alert.add_finding("Scaled deployment to mitigate high CPU")
```

**Key patterns:**
- **Trigger binding** (event → playbook, not LLM → action)
- **Built-in diff generation** for side effects
- **Sinks** (Slack, PagerDuty, S3) for output
- **Shared actions** (scale, restart, describe, exec)

**Comparison:** Robusta's trigger-based model is fast (no LLM latency) but rigid. KubeWise's LLM-driven approach handles novel situations better. The two are complementary — KubeWise could adopt Robusta's triggers for common cases (fast path) while keeping LLM for novel.

### Recommendation: Nice to Have

**Action items (effort: ~2-3 weeks):**

1. **Add YAML-based tool definitions** — Keep the Go `ToolPlugin` interface but allow defining `Validate` schemas in YAML (parameters, constraints, output schema). This makes tool metadata self-documenting and enables future tool marketplace/marketplaces.

2. **Adopt Robusta-style trigger binding** — Add a `FastPathRegistry` that maps known Prometheus alert patterns to pre-cached tool sequences. This bypasses the LLM for common remediations (e.g., high CPU → scale up) and falls through to LLM for novel cases.

3. **Add structured output schemas** — Each tool should declare expected output fields + types so the LLM receives structured results, not raw JSON. This improves LLM decision quality.

**Skip from this round:**
- Crossplane gRPC function model (too much overhead)
- StackStorm pack distribution (premature until multi-cluster)
- StackStorm runner abstraction (Go's interface is sufficient)

---

## Q2: Remediation Patterns & Runbook Orchestration

### Current Approach

**File:** `internal/agent/remediator/runbook.go`, `internal/agent/remediator/execute_plan.go`

`Runbook` is a sequential list of steps. Each step is either: `tool_action`, `wait`, `message`, or `branch`. Steps execute one-by-one with optional `WaitSeconds` between them (polling sleep, not watch-driven). LLM selects the runbook based on correlation.

```go
type RunbookStep struct {
    Type        StepType
    Action      *ToolAction
    WaitSeconds int
    Message     string
}

type Runbook struct {
    Name        string
    Description string
    RiskTier    RiskTier
    Steps       []RunbookStep
    Outcome     RunbookOutcome
}
```

### Alternatives Researched

#### A. Temporal.io for Durable Orchestration

[Temporal Go SDK](https://docs.temporal.io/dev-guide/go/) provides durable execution with automatic retries, timeouts, saga rollback, and human-in-the-loop signals.

**Key patterns for remediation:**
- **Workflow as code** — Full Go control flow (loops, conditionals, branches) without breaking into steps
- **Deterministic replay** — Survives process restarts; mid-remediation state is never lost
- **Saga rollback** — Compensation logic if a step fails mid-runbook
- **Signal/Query** — Human approval channel: workflow pauses, sends notification, waits for signal
- **Child workflows** — Composite runbooks with isolation

```go
func RemediateWorkflow(ctx workflow.Context, params RemediationParams) error {
    // Step 1: Verify issue still exists
    ctx = workflow.WithActivityOptions(ctx, opts)
    var stillFailing bool
    workflow.ExecuteActivity(ctx, VerifyIssueActivity, params).Get(ctx, &stillFailing)
    if !stillFailing { return nil }

    // Step 2: Quarantine with compensation
    var qResult QuarantineResult
    workflow.ExecuteActivity(ctx, QuarantineActivity, params).Get(ctx, &qResult)
    defer workflow.ExecuteActivity(ctx, UnquarantineCompensation, qResult)

    // Step 3: Human approval gate (T3+ risk)
    var approved bool
    workflow.ExecuteActivity(ctx, NotifyHumanApproval, params).Get(ctx, &approved)
    if !approved { return ErrRejected }

    // Step 4: Remediate
    workflow.ExecuteActivity(ctx, RemediateActivity, params).Get(ctx, nil)
    return nil
}
```

**Comparison:**

| Aspect | KubeWise | Temporal |
|--------|----------|----------|
| Execution model | Sequential struct | Full Go code + replay |
| State persistence | None (in-memory only) | Event-sourced, survives crash/migrate |
| Retry | Manual polling | Built-in (interval, backoff, timeout) |
| Rollback | None | Saga pattern (compensation) |
| Human approval | T4 = manual only | Signal/Query mid-workflow |
| Parallelism | None | Promise/channel, child workflows |

#### B. Pulumi Drift Remediation

[Pulumi Automation API](https://www.pulumi.com/docs/guides/automation-api/) enables programmatic IaC. For drift remediation: detect drift via `pulumi preview`, auto-remediate via `pulumi up`, scheduled refresh to detect external changes.

**Key pattern:** KubeWise could model desired cluster state as Pulumi programs, then periodically diff vs real state. Remediation = running `pulumi up` to converge.

**Fit:** Useful for K8s resource drift (configmaps, deployments, RBAC) but overlaps with controller-runtime's reconciliation loop. Best reserved for infrastructure-level drift (node groups, VPCs, IAM) outside K8s.

#### C. LitmusChaos Workflow Templates

LitmusChaos uses **ChaosWorkflow** CRDs with a step-based engine. Steps can be parallel (fan-out), sequential, or conditional. Each step has a `Run` with probes before/after to measure impact.

**Key pattern for KubeWise:** The **probe** model (HTTP, promql, cmd, k8s) before and after each step is directly applicable to remedation — verify preconditions, execute action, verify outcome.

### Recommendation: Must Have

**Action items (effort: ~4-6 weeks):**

1. **Replace linear runbook struct with a DAG model** — Support parallel steps (`fanOut`), conditional branches (`when`), and error handlers (`onFailure`). Keep it YAML/JSON serializable (not full Temporal replay) to remain simple and debuggable.

    ```yaml
    # Future runbook format
    steps:
      - id: verify_issue
        type: tool_action
        tool: pod.check_ready
        params: { pod: "${.target_pod}" }
        on_failure: abort

      - id: scale_up  # parallel with scale_out
        type: tool_action
        run_after: verify_issue
        tool: deployment.scale
        params: { deployment: "${.target_deploy}", replicas: 3 }

      - id: scale_out
        type: tool_action
        run_after: verify_issue
        tool: hpa.update_min
        params: { hpa: "${.target_deploy}", min_replicas: 3 }

      - id: wait_propagate
        type: wait
        wait_until:
          promql: "kube_deployment_status_replicas_available{deployment='${.target_deploy}'} >= 3"
          timeout: 120s

      - id: verify_health
        type: tool_action
        run_after: [scale_up, scale_out]
        tool: service.check_healthy
    ```

2. **Add compensation (rollback) support** — Each runbook step can declare a compensation action that fires if the runbook fails after this step. This handles partial-failure scenarios.

3. **Implement promql-based step gates** — Replace `WaitSeconds` with conditionals that wait on real cluster state (Prometheus query returns expected value). This is more reliable than fixed sleep intervals.

**Skip from this round:**
- Full Temporal integration (event-sourced workflow engine is over-engineering for current scale; the DAG model + in-memory state covers the 90% case)
- Pulumi integration (useful scope is outside K8s; revisit for infra-level remediation)

---

## Q3: Post-Remediation Verification

### Current Approach

**File:** `internal/agent/remediator/verifier.go`

`Verifier` with `VerifyRemediation()` method. Check types: `pod_ready`, `deployment_ready`, `no_crashloop`, `service_healthy`. Implements polling with timeout via `watch.Until` or direct object get.

```go
func (v *Verifier) VerifyRemediation(ctx context.Context, plan *models.RemediationPlan) (*models.VerificationResult, error) {
    for _, check := range plan.VerificationChecks {
        switch check.Type {
        case models.CheckPodReady:
            // watch.Until(pod.ready)
        case models.CheckDeploymentReady:
            // deployment.status.readyReplicas == deployment.status.replicas
        case models.CheckNoCrashLoop:
            // container state waiting with CrashLoopBackOff
        }
    }
}
```

### Alternatives Researched

#### A. Prometheus Alert-as-Verification

Instead of checking raw K8s resources, check whether the **original Prometheus alert** is still firing (the alert that triggered remediation). This validates the root symptom, not an intermediate metric.

**Pattern:**
```
1. Remediation action executed
2. Query Prometheus: Is alert XYZ still firing?
   → Not firing → remediation successful
   → Still firing → remediation failed (try next strategy or escalate)
```

**PromQL queries for verification:**
- `ALERTS{alertname="HighCPUUsage", alertstate="firing"}` — exact alert still active?
- `ALERTS{alertname="~PodCrashLooping.*"}` — pattern match on alert family
- `max(kube_deployment_status_replicas_unavailable{deployment="X"}) > 0` — derived metric

**Advantages:**
- Measures what matters (symptom resolution, not intermediate state)
- Works across resource types (alerts abstract the underlying metric)
- Naturally integrates with existing monitoring stack
- Enables "convergence" detection (alert resolved and stays resolved for N minutes)

#### B. LitmusChaos Steady-State Probes

LitmusChaos defines probes that run before (baseline) and after (comparison) each workflow step. Four probe types:

| Probe Type | What It Checks | KubeWise Applicability |
|-----------|---------------|----------------------|
| **httpProbe** | HTTP status code, response body match | Service health endpoints |
| **promqlProbe** | PromQL query threshold | Alert resolution, metrics |
| **cmdProbe** | Command exit code/output | Custom scripts |
| **k8sProbe** | K8s resource state (field match) | Current pod_ready/deployment_ready |

**Key innovation:** Probes produce **baseline vs. post-action** comparison, not just post-action pass/fail. This detects side effects (e.g., scaled up 5 pods but only 2 remain → something else is interfering).

#### C. StackStorm Integration Tests

StackStorm has a `self-check` command that runs integration tests against installed packs. For remediation, this maps to running a subset of the application's own health tests post-remediation.

**Pattern:** After a pod restart, run the app's `/health` endpoint AND its integration test suite (lightweight smoke test). This catches application-level issues K8s resource checks miss.

### Recommendation: Must Have

**Action items (effort: ~2-3 weeks):**

1. **Add `promqlProbe` to verifier** — New check type that runs a PromQL query and waits for the result to match an expected value (e.g., `value == 0`, `value >= N`). This verifies the **symptom** is resolved, not just the resource state.

    ```go
    type PromqlCheck struct {
        Query    string
        Expected string  // "lt:1", "gt:0", "eq:5"
        Timeout  time.Duration
    }
    ```

2. **Implement baseline recording** — Before remediation, capture the state of verification targets (deployment generation, crash loop count, alert state). After remediation, diff vs baseline to confirm change. This catches "remediated but no actual change" scenarios.

3. **Add `CheckServiceHealthyHTTP`** — Verify that a service's HTTP health endpoint returns 200. This is more reliable than checking pods are running (a pod can be Ready but serving errors).

**Stretch (Nice to Have):**

4. **Add convergence window** — Verification should require the resolved state to persist for N seconds (configurable). This catches flapping resources that briefly appear healthy.

**Skip:**
- Full LitmusChaos probe framework (the promql, HTTP, and K8s probe types cover 95% of verification needs)
- Application integration test runner (too heavy for the remediation loop; app tests belong in CI)

---

## Q4: Risk Tiering & Execution Gating

### Current Approach

**File:** `internal/agent/remediator/tiers.go`

T1–T4 tier system with cooldowns:

| Tier | Risk | Cooldown | Execution |
|------|------|----------|-----------|
| T1 | Low (pod restart) | None | Immediate auto-remediate |
| T2 | Medium (scale down) | 30s | Auto with minimal check |
| T3 | High (scale up, delete) | 120s | Auto with expanded check |
| T4 | Critical (node drain, cluster) | — | Blocked — requires human |

Cooldowns tracked via `remediator.lastActionTimes` map. No approval workflow — T4 just blocks with "manual intervention required."

### Alternatives Researched

#### A. StackStorm Inquiries (Human-in-the-Loop)

StackStorm's `core.ask` action pauses a workflow, sends a notification (Slack, email), and waits for a human response. Responses are time-limited with optional escalation.

**Key patterns:**
- **Inquiry with timeout:** If no response in N minutes, auto-approve or auto-escalate
- **Role-based gating:** Only users with specific RBAC can approve T4 actions
- **Notification channels:** Slack, PagerDuty, email, webhook
- **Audit trail:** Every response logged with user identity

```yaml
# StackStorm inquiry pattern
- name: "approve_node_drain"
  type: "core.ask"
  properties:
    timeout: 300  # 5 min
    role: "cluster-admin"
    message: "Drain node {{node}} for maintenance?"
    schema:
      type: object
      properties:
        confirmed:
          type: boolean
      required: ["confirmed"]
  on-success: "drain_node"
  on-failure: "escalate_to_oncall"
```

#### B. Argo Workflows CVE Remediation Pipeline

Argo Workflows DAG can model conditional gating: scan → human gate (via `suspend` node) → patch → verify.

**Key pattern:** `suspend` template pauses execution until resumed via API or `argo resume`. Combined with Slack notifications, this creates an async approval loop.

```yaml
# Argo suspend pattern for human gate
- name: human-approval
  suspend:
    duration: 300  # auto-resume after 5 min
  onSuccess: run-patch
  onFailure: notify-rejected
```

#### C. OpenPolicyAgent Dry-Run Integration

OPA/Gatekeeper can evaluate the **impact** of a remediation action before execution policies. For example, scaling down a deployment — OPA checks: does this violate any constraint? If warning, escalate tier. If pass, allow auto-execute.

**Advantage:** Policy evaluation happens before execution, not after. Combined with dry-run mode (`kubectl --dry-run=server --server-side`), KubeWise could preview the effect and only proceed if the dry run matches expectations.

### Recommendation: Must Have

**Action items (effort: ~3-4 weeks):**

1. **Add approval workflow for T4** — Implement a `core.ask`-style pattern:
   - KubeWise sends notification (Slack webhook, ops channel)
   - Waits for approval signal (HTTP callback or polling a configmap/annotation)
   - If approved: execute T4 action
   - If rejected or timeout: log, escalate, abort
   - Store approval audit trail in the remediation record

    ```go
    // Tier config with approval
    type TierConfig struct {
        RiskTier           RiskTier
        Cooldown           time.Duration
        RequiresApproval   bool
        ApprovalTimeout    time.Duration
        ApprovalChannel    string // "slack:#ops" or "webhook:url"
        AutoRejectOnTimeout bool
    }
    ```

2. **Add automatic tier escalation** — If a T1/T2 remediation fails repeatedly (configurable threshold within cooldown period), escalate to the next tier. This prevents infinite retry loops.

3. **Add dry-run support** — Before executing mutable actions (delete, scale, drain), run `kubectl --dry-run=server` and validate the result matches expectations. This is low-cost (API is already there) and catches invalid states before they become outages.

**Nice to Have:**

4. **OPA integration** — Evaluate remediation actions against OPA policies before execution. This enables teams to bring their existing policy framework into the gating layer.

**Skip:**
- Full Argo Workflows suspend template integration (KubeWise needs a lightweight approval callback, not a DAG runner)

---

## Q5: Kubernetes Client Executor

### Current Approach

**File:** `internal/agent/remediator/executor.go`

Raw `client-go` (`k8s.Clientset`) for all K8s API operations. Patch/Get/Create calls directly on the REST client:

```go
func (e *Executor) executeScale(ctx context.Context, action models.ToolAction) error {
    patch := []byte(fmt.Sprintf(`{"spec":{"replicas":%d}}`, action.Params.Replicas))
    _, err := e.k8s.Appsv1().Deployments(action.Params.Namespace).
        Patch(ctx, action.Params.Name, types.StrategicMergePatchType, patch, metav1.PatchOptions{})
    return err
}
```

### Alternatives Researched

#### A. controller-runtime Client

[controller-runtime](https://github.com/kubernetes-sigs/controller-runtime) provides a higher-level client over `client-go` with:
- **Cached reads** — Read operations go through an informer-backed cache (reduces API server load, faster reads)
- **Typed reads/writes** — `Get`, `List`, `Create`, `Update`, `Patch`, `Delete` on Go structs, not raw REST
- **Dry-run support** — `DryRunAll` option on all mutations
- **Field selectors, label selectors** — Built-in filtering
- **Multi-cluster** — Manager can watch multiple clusters
- **Status subresource** — Explicit `Status().Update()` for status fields

```go
// controller-runtime vs. client-go — same operation
// client-go
k8s.Appsv1().Deployments(ns).Patch(ctx, name, types.StrategicMergePatchType, patch, metav1.PatchOptions{})

// controller-runtime
deploy := &appsv1.Deployment{}
r.Client.Get(ctx, types.NamespacedName{Name: name, Namespace: ns}, deploy)
deploy.Spec.Replicas = &newReplicas
r.Client.Update(ctx, deploy)
```

**Key differences for KubeWise:**

| Aspect | client-go (current) | controller-runtime |
|--------|---------------------|-------------------|
| Read caching | Direct API call (slow, high load) | Informer cache (fast, low load) |
| Typed CRUD | Raw bytes (json, patch bytes) | Go structs (type-safe) |
| Multi-cluster | Manual config | Built-in Manager |
| Dry-run | Manual header | `client.DryRunAll` |
| Watch | Manual watch.Until | Informer-based event-driven |

#### B. kubebuilder + Controller Pattern

[kubebuilder](https://github.com/kubernetes-sigs/kubebuilder) scaffolds full operators with controller-runtime. The **reconciler pattern** (Reconcile loop triggered by watched events) is relevant to KubeWise because remediation could be modeled as a controller:

```go
func (r *RemediationReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    // 1. Get the Remediation CR
    // 2. Run remediation logic (scale, restart, etc.)
    // 3. Update status
    return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
}
```

#### C. Fake client for testing

controller-runtime provides `fake.NewClientBuilder()` for unit testing without a real API server. KubeWise currently relies on environment-based mocking; a fake client would enable deterministic unit tests for executor.go.

### Recommendation: Nice to Have

**Action items (effort: ~2 weeks):**

1. **Migrate executor.go from raw client-go to controller-runtime client** — This gives cached reads, typed operations, and built-in dry-run. The migration is mechanical (client-go calls → `r.Get`/`r.Update`/`r.Patch`). Benefits compound as more tools are added.

2. **Use fake client in executor tests** — controller-runtime's `fake.NewClientBuilder()` enables writing unit tests for executor.go without a real cluster. Current tests likely require a real cluster or complex mocking.

**Skip:**
- Full kubebuilder operator migration (KubeWise is an agent, not an operator. The reconciler pattern doesn't fit command-based remediation well)
- Multi-cluster Manager (adds complexity; single-cluster covers the use case; multi-cluster can be added later)

---

## Q6: Specialized Remediation Patterns

### Current Approach

KubeWise has a monolithic executor and runbook model. There are no specialized handlers for common operational patterns (node drains, workload evictions, reboot coordination).

### Alternatives Researched

#### A. kured (Kubernetes Reboot Daemon)

[kured](https://github.com/kubereboot/kured) handles the specific pattern of safe node reboots. Key patterns applicable to KubeWise:

- **Drain with safety checks:** Drains a node only when pods can be rescheduled (respects PDBs, daemonset tolerations)
- **Reboot sentinel:** Uses a file (`/var/run/reboot-required`) or Prometheus alert to trigger reboot
- **Lock management:** Distributed lock via configmap to prevent multi-node concurrent drain
- **Time window:** Only reboots within configured maintenance windows
- **Prometheus alert integration:** Can be triggered by `PrometheusAlert` instead of sentinel file
- **Uncordon after reboot:** Automatically uncordons nodes after successful reboot

```go
// kured drain logic (simplified)
func drainNode(node string, opts DrainOptions) error {
    // 1. Check lock (configmap)
    // 2. Check maintenance window
    // 3. Check PDB compliance
    // 4. Cordon & drain with timeout
    // 5. Wait for reboot signal (pod restart)
    // 6. Uncordon
}
```

#### B. descheduler (Eviction Strategies)

[descheduler](https://github.com/kubernetes-sigs/descheduler) provides **eviction strategies** that could model remediation patterns:

| Strategy | What It Does | Remediation Equivalent |
|----------|-------------|----------------------|
| `lownodeutilization` | Evicts pods from underutilized nodes | Scale down / consolidate |
| `highnodeutilization` | Evicts pods to binpack tightly | Resource rebalancing |
| `podlifetime` | Evicts pods older than threshold | Pod rotation / restart |
| `duplicatetos` | Removes duplicate pod topology | HA enforcement |
| `nodeaffinity` | Evicts pods violating node affinity | Pod placement fix |
| `topologyspreadconstraint` | Evicts pods violating spread constraints | AZ rebalancing |

**Key pattern for KubeWise:** Each strategy is parameterized with thresholds and has a `dry-run` mode. Strategies are composable. KubeWise could expose descheduler profiles as remediation tools (e.g., `tool: descheduler.evict_old_pods`).

#### C. Argo Workflows DAG Engine

[Argo Workflows](https://argoproj.github.io/argo-workflows/) provides a full DAG execution engine on Kubernetes. For KubeWise, Argo could replace the custom runbook engine with a battle-tested one.

```yaml
# Argo workflow for multi-step remediation
apiVersion: argoproj.io/v1alpha1
kind: Workflow
spec:
  entrypoint: remediate
  templates:
  - name: remediate
    dag:
      tasks:
      - name: verify
        template: check-health
      - name: scale-up
        template: scale-deployment
        dependencies: [verify]
      - name: notify
        template: send-slack
        dependencies: [scale-up]

  - name: check-health
    container:
      image: curlimages/curl
      command: [sh, -c, "curl -f http://service/health"]
```

**Advantage:** Argo handles retries, backoff, parallelism, artifact passing, GC of completed runs, and has a UI.
**Disadvantage:** KubeWise becomes an Argo workflow generator, adding a dependency. For multi-step runbooks, the custom DAG model (Q2 recommendation) is lighter and doesn't require Argo installation.

### Recommendation: Mixed

**Must Have — Integrate descheduler strategies (~1-2 weeks):**

Expose common descheduler strategies as KubeWise tools. The descheduler is already in-cluster or available as a library. KubeWise benefits from battle-tested eviction logic without reimplementing it.

```go
// Example: descheduler as a KubeWise tool
tool: descheduler.run_strategy
params:
  strategy: "lownodeutilization"
  thresholds:
    cpu_threshold: 20  # evict from nodes <20% CPU utilized
    memory_threshold: 20
  dry_run: false
```

**Nice to Have — Reboot coordination from kured (~1 week):**

Extract kured's drain-with-safety-checks pattern into a shared tool utility. Not importing kured as a library (it's not designed as one), but implementing the same pattern:

1. Check PDB compliance before drain
2. Acquire distributed lock (configmap)
3. Cordon, drain, wait for completion
4. Uncordon on success or timeout
5. Release lock

```go
// SafeDrain tool — modeled after kured's pattern
tool: node.safe_drain
params:
  node: "ip-10-0-1-42"
  ignore_daemonsets: true
  delete_emptydir_data: false
  timeout_seconds: 300
  post_drain_wait: 60
  auto_uncordon: true
```

**Skip:**
- Argo Workflows as runbook engine (too much dependency; lightweight DAG model in Q2 covers the need)
- Full kured integration (too opinionated — sentinel file, maintenance window, lock semantics are reboot-specific)

---

## Summary: Priority Matrix

| Dimension | Score | Effort | Key Outcome |
|-----------|-------|--------|-------------|
| **Q1: Tool Plugins** | Nice to Have | 2–3 wks | YAML tool defs, fast-path triggers, structured outputs |
| **Q2: Runbook Orchestration** | **Must Have** | 4–6 wks | DAG model, compensation rollback, promql step gates |
| **Q3: Verification** | **Must Have** | 2–3 wks | Prometheus alert probe, baseline recording, HTTP health check |
| **Q4: Risk Tiering** | **Must Have** | 3–4 wks | T4 approval workflow, auto-escalation, dry-run validation |
| **Q5: K8s Executor** | Nice to Have | 2 wks | controller-runtime migration, fake client tests |
| **Q6: Specialized Patterns** | Mixed | 1–3 wks | descheduler integration (Must), reboot coordination (Nice) |

### Recommended Ordering (3 phases):

**Phase 1 (Core safety — ~6–8 weeks):**
1. Q4: T4 approval + auto-escalation + dry-run (makes the system safe to trust)
2. Q3: Prometheus probe + baseline recording (verification that actually works)
3. Q2: DAG model + compensation rollback (resilient runbooks)

**Phase 2 (Ergonomics — ~3–4 weeks):**
4. Q1: YAML tool definitions + fast-path triggers (reduce LLM calls, faster remediation)
5. Q5: controller-runtime migration (cached reads, typed operations)

**Phase 3 (Specialization — ~2–3 weeks):**
6. Q6: descheduler strategy tools + reboot coordination utility

---

## References

- [StackStorm Documentation](https://docs.stackstorm.com/latest/)
- [Temporal Go SDK](https://docs.temporal.io/dev-guide/go/)
- [Crossplane Composition Functions](https://docs.crossplane.io/latest/concepts/composition-functions/)
- [Robusta Playbooks](https://docs.robusta.dev/)
- [LitmusChaos Probes](https://litmuschaos.io/docs/3.0/concepts/probes/)
- [Pulumi Automation API](https://www.pulumi.com/docs/guides/automation-api/)
- [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime)
- [kured](https://github.com/kubereboot/kured)
- [descheduler](https://github.com/kubernetes-sigs/descheduler)
- [Argo Workflows](https://argoproj.github.io/argo-workflows/)
- [OPA Gatekeeper](https://open-policy-agent.github.io/gatekeeper/website/docs/)
- [client-go](https://github.com/kubernetes/client-go)
