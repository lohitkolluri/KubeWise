# Kubernetes Monitoring Stack (setupv2)

This directory now uses Helm for the core monitoring stack and kubectl for additional exporters and metrics.

## Components
- **kube-prometheus-stack (Helm):**
  - Prometheus Operator
  - Prometheus
  - Alertmanager
  - Grafana
  - node-exporter
  - kube-state-metrics
  - Prometheus Adapter
  - ServiceMonitors/PrometheusRules
- **cAdvisor** (manifest)
- **Custom Exporters** (manifest)
- **CoreDNS metrics** (manifest)

## Setup
Run the following script to set up the monitoring stack:

```sh
./Setup.sh
```

This will:
1. Create the `monitoring` namespace (if not present)
2. Install the kube-prometheus-stack via Helm
3. Wait for the Prometheus Operator to be ready
4. Apply manifests for cAdvisor, custom exporters, and CoreDNS metrics

## Access
- **Grafana:** `kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80` → [http://localhost:3000](http://localhost:3000)
- **Prometheus:** `kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090` → [http://localhost:9090](http://localhost:9090)
- **Alertmanager:** `kubectl port-forward -n monitoring svc/kube-prometheus-stack-alertmanager 9093:9093` → [http://localhost:9093](http://localhost:9093)

## References
- https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack
- https://github.com/prometheus-operator/prometheus-operator 