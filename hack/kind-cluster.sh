#!/usr/bin/env bash
# Bootstrap a local kind cluster with Prometheus and KubeWise.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/lib.sh
source "${SCRIPT_DIR}/../scripts/lib.sh"

KIND_CLUSTER="${KIND_CLUSTER:-kubewise}"
PROMETHEUS_NAMESPACE="${PROMETHEUS_NAMESPACE:-monitoring}"
OVERLAY="${OVERLAY:-dev}"
INSTALL_PROMETHEUS="${INSTALL_PROMETHEUS:-true}"

require_cmd kind kubectl helm

if kind get clusters 2>/dev/null | grep -qx "${KIND_CLUSTER}"; then
  warn "kind cluster ${KIND_CLUSTER} already exists — skipping create"
else
  log "creating kind cluster ${KIND_CLUSTER}"
  kind create cluster --name "${KIND_CLUSTER}" --config - <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
EOF
fi

if [[ "${INSTALL_PROMETHEUS}" == "true" ]]; then
  log "installing kube-prometheus-stack"
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update
  helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
    --namespace "${PROMETHEUS_NAMESPACE}" \
    --create-namespace \
    --wait \
    --timeout 10m
else
  warn "skipping kube-prometheus-stack (INSTALL_PROMETHEUS=${INSTALL_PROMETHEUS})"
fi

kubectl_apply_manifests "${OVERLAY}"
kubectl -n "${KUBEWISE_NAMESPACE:-kubewise}" wait --for=condition=Available deployment/kubewise-agent --timeout=180s

log "cluster ready"
log "next: make deploy-dev   # build + load images + rollout"
log "      make port-forward  # localhost:8080"
