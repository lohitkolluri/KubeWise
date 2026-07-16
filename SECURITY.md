# Security Policy

## Supported versions

| Version                  | Supported   |
| ------------------------ | ----------- |
| latest release on `main` | yes         |
| older tags               | best effort |

## Reporting a vulnerability

**Do not open public GitHub issues for security bugs.**

Email or DM the maintainer with:

1. Description of the issue and impact
2. Steps to reproduce
3. Affected component (agent API, remediation executor, Helm chart, etc.)

We aim to acknowledge reports within 72 hours and provide a fix or mitigation timeline.

## Deployment hardening (operators)

- Set `KUBEWISE_API_TOKEN` and `security.requireApiToken=true` (Helm) or `KUBEWISE_REQUIRE_API_TOKEN=true` in production.
- Use namespace-scoped RBAC (`rbac.clusterScoped=false`) when the agent only watches specific namespaces.
- Keep remediation in `dry-run` until approvals and audit workflows are validated.
- Do not commit `manifests/base/15-secret.yaml` (copy from `15-secret.example.yaml`) or API keys to git.

## Agent surface area

| Surface          | Risk                        | Mitigation                                          |
| ---------------- | --------------------------- | --------------------------------------------------- |
| HTTP API `:8080` | Data leak, config tampering | Bearer token auth, body size limits                 |
| K8s API (RBAC)   | Cluster mutation            | Tiered remediation, dry-run default, approvals      |
| LLM providers    | Prompt injection, egress    | In-cluster Ollama option, audit trail               |
| BoltDB store     | Local data at rest          | PVC with restricted permissions, non-root container |
