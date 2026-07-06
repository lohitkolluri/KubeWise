# KubeWise Future Work & Roadmap

> Research-backed priorities for what SREs, platform engineers, and enterprises want from in-cluster Kubernetes AI tooling — and where KubeWise should go next.

**Last updated:** July 2026
**Research method:** Parallel CLI deep research (`ultra-fast` processor) + targeted web searches across r/sre, r/kubernetes, CNCF ecosystem, and competitor docs.

---

## Implementation Status (July 2026)

Recently shipped based on this research:

| Feature                                       | Status              | Inspired by                  |
| --------------------------------------------- | ------------------- | ---------------------------- |
| Pluggable LLM providers (OpenRouter + Ollama) | **Done**            | K8sGPT multi-backend         |
| Slack + generic webhook notifications         | **Done**            | Robusta / Botkube ChatOps    |
| Prediction outcome tracking + `/api/v1/stats` | **Done**            | KubeWise ROI differentiation |
| Dry-run remediation + tiered approvals        | **Already shipped** | r/sre trust model            |
| Helm chart + `kwctl install --helm`           | **Done**            | Enterprise install           |
| Namespace-scoped RBAC + watchNamespaces       | **Done**            | Least-privilege deploy       |
| BoltDB indexes (anomalies + audit)            | **Done**            | API hot-path performance     |
| Fail-closed API auth (`KUBEWISE_REQUIRE_API_TOKEN`) | **Done**        | Enterprise security          |
| MCP server mode                               | Deferred            | K8sGPT MCP                   |
| ReAct investigation loop                      | Planned (P1)        | HolmesGPT                    |

---

## Executive Summary

The Kubernetes AI/SRE market has shifted from **reactive monitoring** to **agentic investigation and remediation**. Public demand is high — CNCF-adjacent projects like [K8sGPT](https://k8sgpt.ai/) (~7.9k GitHub stars) and [Coroot](https://coroot.com/) (~7.8k stars) show strong appetite for open, in-cluster tooling.

However, **trust is the bottleneck**, not features. Engineers at KubeCon 2026 consistently said they would try AI investigation and draft fixes, but are **not ready to grant production write access** without guardrails ([r/sre discussion](https://www.reddit.com/r/sre/comments/1u232k6/ai_sre_tools_in_2026_updated_list_what_i_actually/)). Tools that win adoption first are read-only by default, show reasoning, integrate with existing incident workflows, and require human approval for any cluster mutation.

**KubeWise is well-positioned** as a "Secure Autonomous Operator" — it already combines predictive detection, tiered remediation (T1–T4), human approvals, and observe-only mode. The opportunity is to double down on trust, integrations, and differentiation (predictive + safe write) rather than racing toward full autonomy.

**Recommended positioning:** Not "Autonomous SRE" (too scary) and not "Diagnostic Copilot" (too limited) — **"Know before it breaks, fix only when you approve."**

---

## What Professionals Want (Research Findings)

### Must-Have (table stakes for adoption)

| Need                                              | Why it matters                                                                     | KubeWise today                                                   |
| ------------------------------------------------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| Context-aware diagnostics                         | Engineers lose most time correlating signals across kubectl, logs, metrics, events | Partial — LLM gets describe/events/logs; Prometheus metrics only |
| Read-only / observe mode by default               | Trust prerequisite before any write access                                         | **Yes** — remediation mode toggle                                |
| Human-in-the-loop for risky actions               | Production write access is a non-starter without approval gates                    | **Yes** — T3 escalate + approvals API                            |
| In-cluster / BYOC deployment                      | Data residency, privacy, no egress costs                                           | **Yes** — agent runs in-cluster                                  |
| Multi-LLM backend support                         | Cost, privacy, model quality tradeoffs                                             | **Partial** — OpenRouter only (many models, but single provider) |
| Natural language explanations                     | Reduces onboarding time, accelerates triage                                        | **Yes** — LLM runbooks with evidence                             |
| Audit trail                                       | Compliance, postmortems, blameless culture                                         | **Yes** — audit API + TUI view                                   |
| Slack / PagerDuty / incident workflow integration | Where engineers already work during incidents                                      | **No**                                                           |
| RBAC least-privilege                              | Security teams block cluster-admin agents                                          | **Partial** — scoped ClusterRole, no JIT/sandbox                 |

### Nice-to-Have (differentiation)

| Need                                          | Market signal                                                                                                                         | KubeWise today                                                |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| Predictive failure detection (before crash)   | Dynatrace, Kedify, enterprise AIOps pushing "predictive ops"                                                                          | **Yes** — statistical + pattern matchers + forecaster sidecar |
| Agentic multi-step investigation (ReAct loop) | HolmesGPT, kagent, IncidentFox trend                                                                                                  | **Partial** — multi-step runbooks, not full ReAct             |
| Auto-generated postmortems                    | incident.io, Rootly, PagerDuty workflows                                                                                              | **No**                                                        |
| MCP (Model Context Protocol) server           | K8sGPT MCP mode, standardizing AI tool access                                                                                         | **No**                                                        |
| eBPF deep telemetry                           | Coroot, Cilium — kernel-level visibility                                                                                              | **No**                                                        |
| Multi-cluster management                      | Standard for platform teams at scale                                                                                                  | **No** — single cluster per agent                             |
| Cost / FinOps signals                         | Cast AI, Kubecost demand                                                                                                              | **No**                                                        |
| GitOps-aware remediation (PR-based fixes)     | Aurora, many "AI SRE" vendors                                                                                                         | **No**                                                        |
| SLO/error-budget alerting                     | Shift from threshold alerts to SLO-based ([Sherlocks 2026 tool roundup](https://www.sherlocks.ai/best-sre-and-devops-tools-for-2026)) | **No**                                                        |

### What engineers explicitly do _not_ want (yet)

From [r/sre KubeCon 2026 thread](https://www.reddit.com/r/sre/comments/1u232k6/ai_sre_tools_in_2026_updated_list_what_i_actually/):

- Fully autonomous production remediation without evidence trail
- Black-box AI decisions with no cited log lines / events / metrics
- Agents with broad write RBAC and no sandbox
- Another SaaS observability bill on top of Datadog/Grafana

Quote pattern from the field: _"We would try an investigation. We would let it draft a fix. We would maybe let it open a PR. We are not giving it production write access yet."_

---

## Competitive Landscape

| Tool                    | Value prop                    | Deployment        | Write access       | Gap vs KubeWise                                 |
| ----------------------- | ----------------------------- | ----------------- | ------------------ | ----------------------------------------------- |
| **K8sGPT**              | AI diagnostics, 50+ analyzers | CLI / Operator    | Read-only          | No prediction, no remediation, no tiered safety |
| **HolmesGPT**           | Agentic investigation (ReAct) | Operator / SaaS   | Read-only          | No safe execution path                          |
| **Robusta**             | Alert enrichment + automation | In-cluster / SaaS | Rule-based actions | Less predictive; Prometheus-alert-dependent     |
| **Komodor**             | Autonomous AI SRE (Klaudia)   | SaaS              | Full agentic       | Expensive; node-based pricing; not BYOC-first   |
| **Coroot**              | OSS observability + eBPF APM  | In-cluster        | Read-only          | No remediation loop                             |
| **Botkube**             | ChatOps / Slack bot           | Slack/Teams       | Limited            | Shallow root-cause automation                   |
| **Shoreline**           | Fleet auto-remediation        | SaaS / Enterprise | Template workflows | Rigid paths; not LLM-native                     |
| **Datadog / Dynatrace** | Full-stack observability + AI | SaaS              | Varies             | High cost; not in-cluster-first                 |
| **IncidentFox**         | OSS AI SRE investigation      | CLI / Slack       | Read-only          | New entrant; no in-cluster agent                |

**KubeWise unique intersection today:** Predictive detection + LLM runbooks + tiered remediation + observe-only default + in-cluster agent. Few competitors cover all five.

---

## KubeWise Strengths (Build On These)

1. **"Know before it breaks"** — Adaptive-median scoring, changepoint detection, OOM/crashloop/degradation pattern matchers, and a Python forecaster sidecar. This is a real differentiator vs read-only copilots.
2. **Tiered remediation safety model** — T1 (auto) → T4 (reject) with blast-radius promotion and cooldowns. Aligns with enterprise "guardrails" demand.
3. **Observe-first default** — Matches the #1 adoption pattern from the field.
4. **Rich investigation context for LLM** — Pod describe, events, log tails fed into prompts with structured JSON output and verification steps.
5. **Developer experience** — `kwctl` TUI, npm install, one-command `kwctl install`, port-forward + connect flow.
6. **Open source (MIT)** — Matches OSS-first market preference.

---

## Gaps & Opportunities

### Gap 1: Incident workflow integrations (highest adoption leverage)

Engineers live in **Slack + PagerDuty + incident.io** during outages. KubeWise is CLI/TUI-only today. Honeycomb engineers described on-call Slack group sync as receiving _"the most direct, instant amount of positive feedback in years"_ for small workflow friction fixes ([Honeycomb blog](https://www.honeycomb.io/blog/syncing-pagerduty-schedules-slack-groups)).

**Opportunity:** Push predictions and approval requests into Slack; page on critical predictions via PagerDuty webhook.

### Gap 2: Trust & safety hardening

2025 incidents involving autonomous agents causing data loss have made security teams skeptical. Enterprises want least-privilege RBAC, JIT credentials, namespace-scoped agents, and dry-run simulation.

**Opportunity:** KubeWise already has tiers — extend with dry-run mode, policy-as-code (OPA), per-namespace agent deployments, and remediation previews in the TUI.

### Gap 3: Investigation depth

Competitors are adding ReAct loops and MCP servers. K8sGPT's MCP mode exposes 12+ tools to AI clients ([K8sGPT MCP docs](https://docs.k8sgpt.ai/)).

**Opportunity:** Expose KubeWise agent as an MCP server; add multi-turn investigation before remediation plan generation.

### Gap 4: Telemetry breadth

Most AI K8s tools rely on `kubectl` output. eBPF-based tools (Coroot) see kernel-level issues KubeWise cannot.

**Opportunity:** Optional eBPF collector sidecar or OpenTelemetry trace/log correlation (OTel deps already in `go.sum`).

### Gap 5: Multi-cluster & platform team scale

Platform teams manage 5–50 clusters. Single-agent-per-cluster does not scale operationally.

**Opportunity:** Central control plane (lightweight) or `kwctl` multi-profile federation without requiring a SaaS.

### Gap 6: Outcomes & ROI proof

Enterprises struggle to quantify predictive detection ROI vs better alerting. Benchmark is **MTTR reduction** and **prevented incidents**.

**Opportunity:** Built-in metrics: predictions that came true, remediations that succeeded, time-saved estimates, exportable reports.

---

## Strategic Positioning

```
                    HIGH AUTONOMY
                         │
         Komodor/Aurora    │    (feared zone)
                         │
    ─────────────────────┼─────────────────────
    READ-ONLY            │         SAFE WRITE
                         │
         K8sGPT          │    ★ KubeWise target
         HolmesGPT       │      (predict + approve + act)
         Coroot           │
                         │
                    LOW AUTONOMY
```

**Tagline refinement:** "Know before it breaks — fix only when you approve."

**Target personas:**

- **Primary:** Platform / SRE teams (5–50 clusters) drowning in alert fatigue who want prediction without giving up control
- **Secondary:** DevOps leads at growth-stage companies adopting K8s without budget for Datadog + Komodor
- **Tertiary:** OSS contributors / CNCF community building on predictive + agentic K8s tooling

**Pricing direction (when commercializing):**

- Free OSS for small clusters (≤3 nodes or single namespace) — matches K8sGPT/Coroot expectation
- Enterprise: node-based or namespace-based pricing (not per-host Datadog model)
- Optional BYOC support tier for regulated industries

---

## Prioritized Roadmap

### P0 — Trust & Adoption (next 1–2 quarters)

These unblock real production trials.

| #    | Task                                                                                              | Rationale                                  | Effort |
| ---- | ------------------------------------------------------------------------------------------------- | ------------------------------------------ | ------ |
| P0-1 | **Slack integration** — post predictions, approval requests, remediation outcomes to a channel    | #1 workflow gap; highest adoption leverage | M      |
| P0-2 | **Webhook / generic notification sink** — PagerDuty Events API v2, incident.io, custom URL        | Enterprise incident workflow requirement   | S      |
| P0-3 | **Dry-run remediation mode** — show exact `kubectl` equivalent without applying                   | Trust builder before live mode             | S      |
| P0-4 | **Remediation preview in TUI** — diff view of planned patches/scales before approve               | Human-in-the-loop UX                       | M      |
| P0-5 | **Namespace-scoped agent option** — Helm value to limit RBAC + watch scope                        | Security team approval                     | M      |
| P0-6 | **Multi-LLM provider support** — direct OpenAI, Anthropic, Ollama/local in addition to OpenRouter | Privacy + cost; table stakes               | M      |
| P0-7 | **Helm chart publish** — official chart repo, values docs, upgrade path                           | Enterprise install expectation             | S      |

### P1 — Differentiation (2–4 quarters)

Deepen the moat around "predictive + safe act."

| #    | Task                                                                                           | Rationale                              | Effort |
| ---- | ---------------------------------------------------------------------------------------------- | -------------------------------------- | ------ |
| P1-1 | **MCP server mode** — expose predictions, anomalies, investigation, approvals as MCP tools     | AI ecosystem standard; K8sGPT parity+  | L      |
| P1-2 | **ReAct-style investigation loop** — multi-turn log/metric/event queries before plan           | HolmesGPT-class investigation          | L      |
| P1-3 | **OpenTelemetry integration** — correlate traces/logs with predictions                         | Context depth beyond Prometheus        | L      |
| P1-4 | **Auto-postmortem generator** — markdown export from audit trail + LLM summary                 | Nice-to-have that drives renewals      | M      |
| P1-5 | **Outcome metrics dashboard** — prediction accuracy, MTTR, prevented incidents                 | ROI proof for enterprise sales         | M      |
| P1-6 | **GitHub PR remediation** — for config fixes, open PR instead of live patch (T3 path)          | Matches "draft a fix" adoption pattern | L      |
| P1-7 | **Forecaster improvements** — more metric types, seasonal patterns, confidence intervals in UI | Strengthen core differentiator         | M      |
| P1-8 | **High availability** — leader election RBAC already stubbed; active-passive agent HA          | Production hardening                   | M      |

### P2 — Platform Scale (4+ quarters)

| #    | Task                                                                                     | Rationale             | Effort |
| ---- | ---------------------------------------------------------------------------------------- | --------------------- | ------ |
| P2-1 | **Multi-cluster `kwctl` federation** — manage profiles, aggregate status across clusters | Platform team scale   | L      |
| P2-2 | **Optional eBPF telemetry sidecar** — kernel-level signals (Coroot-class)                | Telemetry gap         | XL     |
| P2-3 | **Policy-as-code (OPA/Gatekeeper)** — remediation policies enforced before execution     | Enterprise governance | L      |
| P2-4 | **SLO / error budget integration** — alert on budget burn, not raw thresholds            | Modern SRE practice   | L      |
| P2-5 | **Cost anomaly detection** — pod resource waste, rightsizing suggestions                 | FinOps adjacency      | M      |
| P2-6 | **Operator CRD mode** — `Prediction` / `Remediation` CRDs for GitOps workflows           | CNCF ecosystem fit    | L      |
| P2-7 | **Agent sandbox (gVisor/Firecracker)** — isolated execution environment for remediations | Advanced trust layer  | XL     |

---

## Key Tasks (Immediate Next Sprint)

If picking **five things to start this week**:

1. **Slack webhook notifier** — `POST` predictions above confidence threshold to Slack incoming webhook; config via `AgentConfig`
2. **Dry-run flag on remediation executor** — log intended action, skip K8s API call, record in audit as `dry_run: true`
3. **Helm chart** — wrap existing `manifests/` into a chart with documented values (`remediation.enabled`, `namespaces`, `openrouter.model`)
4. **Ollama provider** — local LLM for air-gapped clusters (no API key required)
5. **Prediction accuracy tracking** — when a pattern match fires and pod actually OOMs/crashloops within ETA window, record `prediction_hit` metric

---

## Success Metrics

| Metric                                          | Target (6 months) | Target (12 months)            |
| ----------------------------------------------- | ----------------- | ----------------------------- |
| GitHub stars                                    | 500               | 2,000                         |
| npm weekly downloads (`kwctl`)                  | 100               | 1,000                         |
| Prediction precision (true positive rate)       | >70%              | >85%                          |
| Median time from anomaly → approved remediation | <10 min           | <5 min                        |
| % users in observe-only mode                    | >80%              | >60% (more trust → more live) |
| Integration adoption (Slack/webhook configured) | 30% of installs   | 60%                           |

---

## Risks & Mitigations

| Risk                                     | Mitigation                                                                |
| ---------------------------------------- | ------------------------------------------------------------------------- |
| "Another AI hype tool" perception        | Lead with prediction accuracy metrics, not LLM marketing                  |
| LLM hallucination causes bad remediation | Keep T3+ approval, confidence threshold (0.7), verification loop, dry-run |
| Prometheus-only limits signal quality    | OTel + optional eBPF roadmap                                              |
| Komodor/Datadog add prediction           | OSS + in-cluster + BYOC moat; be cheaper and more controllable            |
| Security team blocks agent RBAC          | Namespace-scoped deploy, least-privilege docs, dry-run default            |

---

## Research Sources

### Deep research (Parallel CLI)

- Run ID: `trun_f0a57723f9344f76acd4b46b572158e9`
- [Monitoring URL](https://platform.parallel.ai/play/deep-research/trun_f0a57723f9344f76acd4b46b572158e9)
- Local output: `/tmp/kubewise-market-research.json`

### Web search highlights

- [AI SRE tools in 2026 — r/sre KubeCon takeaways](https://www.reddit.com/r/sre/comments/1u232k6/ai_sre_tools_in_2026_updated_list_what_i_actually/) (Jun 2026)
- [AI agents for Kubernetes — fragmented knowledge problem](https://www.plural.sh/blog/ai-agents-for-kubernetes/)
- [K8sGPT for troubleshooting — Palark analysis](https://palark.com/blog/k8sgpt-ai-troubleshooting-kubernetes/) (Dec 2025)
- [Predictive Kubernetes operations — Dynatrace](https://docs.dynatrace.com/docs/observe/infrastructure-observability/kubernetes-app/use-cases/predictive-operations) (Feb 2026)
- [Best DevOps & SRE Tools 2026 — Sherlocks](https://www.sherlocks.ai/best-sre-and-devops-tools-for-2026) (Jul 2026)
- [Observability evolution — Cloud Native Now](https://cloudnativenow.com/editorial-calendar/best-of-2025/the-observability-evolution-how-ai-and-open-source-are-taming-kubernetes-complexity-2/) (Dec 2025)
- [Kubernetes adoption statistics 2025 — Edge Delta](https://edgedelta.com/company/knowledge-center/kubernetes-adoption-statistics) (Mar 2025)
- [PagerDuty + Slack incident workflow](https://www.pagerduty.com/integrations/slack)
- [K8sGPT Operator docs](https://docs.k8sgpt.ai/reference/operator/overview)

---

## Appendix: Current Feature Inventory

For gap-analysis reference — what ships today.

**Agent (in-cluster)**

- Prometheus metric scraping (~30s interval)
- Statistical anomaly detection (adaptive median, changepoint, Hoeffding bounds)
- Pattern matchers: OOM, crashloop, degradation
- Python forecaster sidecar (gRPC)
- Anomaly gate (noise filtering)
- SQLite local store (predictions, anomalies, audit, config)
- LLM remediation via OpenRouter (multi-step runbooks, verification)
- Tiered remediation executor (T1–T4, cooldowns, approvals)
- HTTP API (`/api/v1/predictions`, `/anomalies`, `/audit`, `/approvals`, `/config`, `/remediation/mode`)

**CLI (`kwctl`)**

- TUI control center (predictions, anomalies, audit, approvals, config, logs)
- `install`, `connect`, `status`, `predict`, `anomalies`, `remediation`, `logs`, `watch`
- Profiles (`~/.config/kwctl/config.yaml`)
- npm distribution (`kwctl`, `kubewise-cli`)

**Not yet built**

- PagerDuty / incident.io integrations (Slack/webhook notifications shipped)
- MCP server
- Multi-cluster
- eBPF / OTel correlation
- HA (leader election RBAC exists, not implemented)
- Web UI (TUI only)
- SLO/error budgets
- GitOps / PR-based remediation
