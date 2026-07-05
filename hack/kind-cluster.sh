#!/usr/bin/env bash
set -euo pipefail

KIND_CLUSTER="${KIND_CLUSTER:-kubewise}"
PROMETHEUS_NAMESPACE="${PROMETHEUS_NAMESPACE:-monitoring}"

echo "=== Creating kind cluster: ${KIND_CLUSTER} ==="
kind create cluster --name "${KIND_CLUSTER}" --config - <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
EOF

echo "=== Installing Prometheus (kube-prometheus-stack) ==="
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace "${PROMETHEUS_NAMESPACE}" \
  --create-namespace \
  --wait \
  --timeout 10m

echo "=== Deploying KubeWise agent ==="
kubectl apply -f manifests/

echo "=== Waiting for agent deployment to be ready ==="
kubectl -n kubewise wait --for=condition=Available deployment/kubewise-agent --timeout=120s

echo "=== Done ==="
echo "Run: kubectl -n kubewise port-forward svc/kubewise-agent 8080:8080"
