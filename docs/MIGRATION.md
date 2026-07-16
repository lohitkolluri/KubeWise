# Migration Guide

## General Upgrade Principles

- `kwctl` CLI is safe to upgrade independently of the in-cluster agent. Update the CLI at any time without affecting running predictions or remediation.
- **Helm users**: Run `helm upgrade` with your existing values file. The chart handles migration of feature flags and config keys.
- **Manifests users**: Re-apply the latest overlay from `manifests/` and restart the deployment with `kubectl rollout restart`.
- **Data backup**: Before upgrading the agent pod, back up the BoltDB database. The `kubewise-agent` pod stores data at `/data/kubewise.db`. Copy it out with `kubectl cp` or a pre-upgrade hook.

## Feature Flags

All feature flags default to `true` since v2. Set the corresponding env var to `false` to disable. For example: `KUBEWISE_FEATURE_RULE_ENGINE=false`.

| Flag | Env Var | Default | Effect |
|------|---------|---------|--------|
| ruleEngine | KUBEWISE_FEATURE_RULE_ENGINE | true | Deterministic rules before LLM path. Disable to force LLM-only remediation. |
| contextBuilder | KUBEWISE_FEATURE_CONTEXT_BUILDER | true | Prompt compression (~80% token reduction). Disable to send raw context to LLM. |
| llmRouter | KUBEWISE_FEATURE_LLM_ROUTER | true | Multi-model fallback chains. Disable to use a single LLM model. |
| semanticCache | KUBEWISE_FEATURE_SEMANTIC_CACHE | true | Hash-based incident dedup cache. Disable to force fresh LLM analysis each time. |
| eventsV2 | KUBEWISE_FEATURE_EVENTS_V2 | true | Shared-informer event collector. Disable to use legacy polling. |
| toolPlugins | KUBEWISE_FEATURE_TOOL_PLUGINS | true | Tool plugin system. Disable to prevent external tool execution. |
| observability | KUBEWISE_FEATURE_OBSERVABILITY | true | Loki+Tempo+Alloy stack. Disable if using external observability. |

## Breaking Changes Policy

- Breaking changes are communicated in GitHub Release notes.
- Tagged `vX.Y.Z` releases follow semver conventions.
- Pre-1.0: minor versions may include breaking changes (documented in release notes).
- Major config key changes or API contract changes will be announced at least one release prior.

## Upgrade Checklist

- [ ] Read release notes for the target version
- [ ] Back up BoltDB data from the running agent pod
- [ ] Review changed feature flags and their defaults
- [ ] Run `kwctl doctor` post-upgrade to verify connectivity
- [ ] Run `kwctl status` post-upgrade to verify agent health
- [ ] Check `kwctl logs -f` for startup errors
