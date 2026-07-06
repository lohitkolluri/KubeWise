# KubeWise

**Know before it breaks.** KubeWise watches your Kubernetes cluster, spots trouble early, and helps you respond — from a terminal UI or plain CLI commands.

## Architecture

[![KubeWise architecture](docs/architecture.svg)](docs/architecture.svg)

**Scrape loop (every ~30s):** collect → predict → gate → store → forecast → remediate.

**Default mode is observe** — remediations are logged and audited; nothing changes in the cluster until you switch to live mode and approve risky actions.

---

## Install

You need **kubectl** pointed at a cluster and permission to create namespaces and RBAC.

**One command** (no clone required):

```bash
curl -fsSL https://raw.githubusercontent.com/lohitkolluri/KubeWise/main/hack/bootstrap.sh | bash
```

Or with the CLI:

```bash
curl -fsSL https://raw.githubusercontent.com/lohitkolluri/KubeWise/main/hack/install.sh | bash
kwctl install --yes
```

Optional — enable LLM-backed remediation:

```bash
OPENROUTER_API_KEY=sk-... kwctl install --yes
```

**On your laptop** (kind + local images, no registry):

```bash
git clone https://github.com/lohitkolluri/KubeWise.git && cd KubeWise
./hack/bootstrap.sh --local
```

---

## Use it

Port-forward the agent (keep this running in a terminal):

```bash
kubectl -n kubewise port-forward svc/kubewise-agent 8080:8080
```

Open the control center — just run `kwctl` in a TTY, or:

```bash
kwctl ui
```

Check that everything is connected:

```bash
kwctl connect
kwctl status
```

---

## kwctl

Running `kwctl` with no arguments opens the **interactive control center** (tabs for dashboard, predictions, anomalies, audit, approvals, config, logs). Press `?` for shortcuts, `ctrl+p` for the command palette.

| Command             | What it does                               |
| ------------------- | ------------------------------------------ |
| `kwctl install`     | Deploy the agent into your current cluster |
| `kwctl ui`          | Full-screen control center                 |
| `kwctl status`      | Agent health, uptime, scrape count         |
| `kwctl predict`     | Active failure predictions                 |
| `kwctl anomalies`   | Recent detected anomalies                  |
| `kwctl config`      | View or update agent settings              |
| `kwctl remediation` | Remediation audit log                      |
| `kwctl logs`        | Agent pod logs (`-f` to follow)            |
| `kwctl watch`       | Live-updating status view                  |
| `kwctl profile`     | Save agent URL, namespace, output format   |
| `kwctl connect`     | Ping the agent API                         |

Output formats: `-o table` (default), `json`, or `yaml`.

Profiles live at `~/.config/kwctl/config.yaml`.

---

## Agent API

Base URL is usually `http://localhost:8080` after port-forwarding.

| Method        | Path                             | Description                 |
| ------------- | -------------------------------- | --------------------------- |
| `GET`         | `/health`                        | Liveness                    |
| `GET`         | `/status`                        | Uptime, scrapes, gate stats |
| `GET`         | `/api/v1/predictions`            | Predictions                 |
| `GET`         | `/api/v1/anomalies`              | Anomalies (`?limit=N`)      |
| `GET`         | `/api/v1/audit`                  | Remediation audit log       |
| `GET`         | `/api/v1/approvals`              | Pending T3 approvals        |
| `GET` / `PUT` | `/api/v1/config`                 | Agent configuration         |
| `GET` / `PUT` | `/api/v1/remediation/mode`       | Live vs observe mode        |
| `POST`        | `/api/v1/approvals/{id}/approve` | Approve a remediation       |
| `POST`        | `/api/v1/approvals/{id}/reject`  | Reject a remediation        |

---

## How it works

The agent pod runs two containers: a **Go agent** and a **Python forecaster sidecar**.

| Stage         | What happens                                                                                                                         |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| **Collect**   | Pull metrics from Prometheus; watch K8s events and workload state                                                                    |
| **Predict**   | Score anomalies (EWMA, Z-score, rate-of-change) and match patterns (OOM, crash loop, degradation)                                    |
| **Gate**      | Drop noise — cooldowns, sustainment, deduplication                                                                                   |
| **Forecast**  | Sidecar projects metric trends (ETS) from stored history                                                                             |
| **Remediate** | LLM correlates anomalies into a plan; multi-step runbooks; post-fix verification; T1/T2 auto-execute in live mode, T3 needs approval |
| **Store**     | Everything persists in embedded bbolt — no external database                                                                         |

You interact via **kwctl** (TUI or commands) or the **HTTP API** on port 8080.

---

## Develop

```bash
make help          # all make targets
make kind-up       # kind + Prometheus + manifests
make deploy-dev    # rebuild images and rollout
make test          # unit tests
make port-forward  # localhost:8080
```

Layout:

| Path                                    | Purpose                  |
| --------------------------------------- | ------------------------ |
| `cmd/agent`                             | In-cluster agent         |
| `cmd/kwctl`                             | CLI                      |
| `manifests/base`                        | Kustomize base           |
| `manifests/overlays/{dev,install,prod}` | Environment overlays     |
| `hack/bootstrap.sh`                     | One-click installer      |
| `docs/architecture.svg`                 | Architecture diagram     |
| `forecaster-sidecar`                    | gRPC forecasting service |

Requires Go 1.26+, Docker, and for local clusters: [kind](https://kind.sigs.k8s.io/) + Helm.

---

## License

MIT — see [LICENSE](LICENSE).
