# Feature Flag Migration Guide

Upgrade your KubeWise deployment with zero downtime. Every new capability is
**opt-in via feature flags**, all defaulting to `false`. A deployment with all
flags off behaves identically to one without them.

## Upgrade path

### 1. Deploy with all flags off (safe first step)

```bash
helm upgrade kubewise ./charts/kubewise \
  --namespace kubewise \
  --reuse-values
```

Or via kwctl:

```bash
kwctl install --helm --yes
```

Verify: `kwctl doctor` reports all-clear, existing predictions and alerts
continue uninterrupted.

### 2. Enable features one at a time

Each flag is independent. Enable only what you need.

```bash
helm upgrade kubewise ./charts/kubewise \
  --set agent.features.ruleEngine=true
```

| Flag | Env var | What it does | Risk |
|---|---|---|---|
| `ruleEngine` | `KUBEWISE_FEATURE_RULE_ENGINE` | Deterministic rules catch known anomaly patterns before LLM invocation | Low — rules are a fast-path superset of existing detection |
| `llmRouter` | `KUBEWISE_FEATURE_LLM_ROUTER` | Routes simple incidents to cheap models, complex ones to capable models | Low — response quality maintained or improved |
| `semanticCache` | `KUBEWISE_FEATURE_SEMANTIC_CACHE` | Caches LLM results for semantically similar incidents | Low — cache miss falls through to normal LLM call |
| `contextBuilder` | `KUBEWISE_FEATURE_CONTEXT_BUILDER` | Compresses prompt context to reduce token usage | Low — compressed prompts still contain all signals |
| `eventsV2` | `KUBEWISE_FEATURE_EVENTS_V2` | Shared-informer-based event collector (replaces raw watch) | Medium — changes event ingestion path |
| `toolPlugins` | `KUBEWISE_FEATURE_TOOL_PLUGINS` | Enables kubectl/helm/argocd remediation plugins | Medium — grants agent kubectl access in live mode |
| `observability` | `KUBEWISE_FEATURE_OBSERVABILITY` | Deploys Loki + Tempo + Alloy for logs and traces | Medium — adds resource footprint |

**Recommended enablement order:** `ruleEngine` → `semanticCache` → `llmRouter` →
`contextBuilder` → `eventsV2` → `toolPlugins` → `observability`

### 3. Verify after each flag

```bash
kwctl doctor          # cluster health
kwctl status          # agent status + feature state
curl localhost:8080/status   # API health
```

Check that existing dashboards and alerts still fire correctly.

## Feature flag reference

Flags are configured via Helm values or environment variables on the agent pod:

```yaml
# values.yaml
agent:
  features:
    ruleEngine: false
    contextBuilder: false
    llmRouter: false
    semanticCache: false
    eventsV2: false
    toolPlugins: false
    observability: false
```

Environment variable equivalents (set on the agent container):

```yaml
env:
  - name: KUBEWISE_FEATURE_RULE_ENGINE
    value: "true"
```

### How flags interact

- `ruleEngine` + `semanticCache`: Rule engine catches deterministic patterns;
  cache deduplicates LLM calls for the rest. Both can be on simultaneously.
- `llmRouter` + `contextBuilder`: Router picks the model tier; builder
  compresses the prompt before sending. Naturally complementary.
- `eventsV2` is independent — it only affects how Kubernetes events are
  collected, not how they are processed downstream.
- `toolPlugins` only matters when `agent.remediation.mode` is not `off`.
- `observability` is fully standalone.

## New API endpoints

Available when any feature flag is enabled:

| Endpoint | Description |
|---|---|
| `GET /api/v1/pipeline/stats` | Pipeline stage counters (rule engine hits, cache hits, LLM calls) |
| `GET /api/v1/cache/stats` | Semantic cache hit rate and size |

## Rollback

### Option A: Disable flags (no redeploy needed for env-var-based flags)

Set all flags back to `false` and restart the agent pod:

```bash
helm upgrade kubewise ./charts/kubewise \
  --set agent.features.ruleEngine=false \
  --set agent.features.semanticCache=false
```

### Option B: Full rollback

```bash
helm rollback kubewise 1 --namespace kubewise
```

### Data safety

- Feature flag state is ephemeral (env-var-based). Pod restart with flags off
  returns to baseline behavior instantly.
- The semantic cache bucket (`incident_cache`) is only written when
  `semanticCache=true`. Disabling the flag stops writes; stale cache
  entries are harmless.

## Breaking changes

None. The feature-gated additions are **fully backward compatible**:

- All existing API routes unchanged
- All existing config keys unchanged
- All existing store schemas unchanged
- All existing CLI commands unchanged
- Helm chart values structure unchanged (new keys added under `agent.features.*`)
