#!/usr/bin/env bash
# KubeWise one-click installer — cluster + optional kwctl CLI.
#
#   curl -fsSL https://raw.githubusercontent.com/lohitkolluri/KubeWise/v2/hack/bootstrap.sh | bash
#
# Options (env or flags):
#   --yes / -y          non-interactive
#   --local             kind + build images (laptop dev)
#   OPENROUTER_API_KEY  optional LLM key
#   KUBEWISE_REF        git ref for remote manifests (default: v2)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/lib.sh
source "${SCRIPT_DIR}/../scripts/lib.sh"

REPO="${REPO:-lohitkolluri/KubeWise}"
REF="${KUBEWISE_REF:-v2}"
OVERLAY="${OVERLAY:-install}"
NAMESPACE="${KUBEWISE_NAMESPACE:-kubewise}"
YES=0
LOCAL=0
INSTALL_KWCTL="${INSTALL_KWCTL:-1}"
WITH_PROMETHEUS="${WITH_PROMETHEUS:-auto}"

usage() {
  cat <<EOF
KubeWise bootstrap — install agent into Kubernetes with minimal setup.

Usage:
  bootstrap.sh [options]

Options:
  -y, --yes       non-interactive
  --local         dev: create/use kind, build images, deploy (requires docker+kind)
  -h, --help      show help

Environment:
  OPENROUTER_API_KEY     optional LLM API key
  KUBEWISE_API_TOKEN     optional agent HTTP API token
  KUBEWISE_REF           manifest git ref (default: v2)
  INSTALL_KWCTL=0        skip kwctl CLI install

Examples:
  curl -fsSL .../hack/bootstrap.sh | bash
  OPENROUTER_API_KEY=sk-... ./hack/bootstrap.sh --yes
  ./hack/bootstrap.sh --local
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) YES=1; shift ;;
    --local) LOCAL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

install_kwctl_cli() {
  [[ "${INSTALL_KWCTL}" == "1" ]] || return 0
  if command -v kwctl >/dev/null 2>&1; then
    log "kwctl already installed: $(command -v kwctl)"
    return 0
  fi
  log "installing kwctl CLI"
  bash "${SCRIPT_DIR}/install.sh" || warn "kwctl install failed — build from source or download a release manually"
}

local_dev_install() {
  require_cmd kind docker kubectl make
  export KUBEWISE_NAMESPACE="${NAMESPACE}"
  if ! kind get clusters 2>/dev/null | grep -qx "${KIND_CLUSTER:-kubewise}"; then
  log "creating kind cluster (first run may take a few minutes)"
    bash "${SCRIPT_DIR}/kind-cluster.sh"
  fi
  make -C "${ROOT_DIR}" deploy-dev OVERLAY=dev
  save_kwctl_profile
  print_done
}

remote_cluster_install() {
  require_cmd kubectl curl
  install_kwctl_cli

  log "checking cluster access"
  kubectl cluster-info >/dev/null

  local kustomize_url="github.com/${REPO}/manifests/overlays/${OVERLAY}?ref=${REF}"
  log "applying manifests: ${kustomize_url}"
  kubectl apply -k "${kustomize_url}"

  apply_secret
  patch_prometheus_if_found

  log "waiting for agent rollout"
  kubectl -n "${NAMESPACE}" rollout status deployment/kubewise-agent --timeout=180s

  save_kwctl_profile
  print_done
}

apply_secret() {
  local key="${OPENROUTER_API_KEY:-}"
  local tok="${KUBEWISE_API_TOKEN:-}"
  if [[ -z "${key}" && -z "${tok}" ]]; then
    log "no OPENROUTER_API_KEY — observe-only mode (dry-run remediation)"
    return 0
  fi
  local args=(create secret generic kubewise-agent-secret -n "${NAMESPACE}" --dry-run=client -o yaml)
  [[ -n "${key}" ]] && args+=(--from-literal=openrouter_api_key="${key}")
  [[ -n "${tok}" ]] && args+=(--from-literal=api_token="${tok}")
  kubectl "${args[@]}" | kubectl apply -f -
  log "secret applied"
}

patch_prometheus_if_found() {
  [[ "${WITH_PROMETHEUS}" == "0" ]] && return 0
  local ns="${PROMETHEUS_NAMESPACE:-monitoring}"
  local url=""
  for name in kube-prometheus-stack-prometheus prometheus-kube-prometheus-prometheus prometheus-server prometheus; do
    if kubectl get svc "${name}" -n "${ns}" >/dev/null 2>&1; then
      url="http://${name}.${ns}.svc.cluster.local:9090"
      break
    fi
  done
  if [[ -z "${url}" ]]; then
    if [[ "${WITH_PROMETHEUS}" == "auto" && "${YES}" -eq 0 ]]; then
      warn "Prometheus not found in namespace ${ns}"
      warn "Install kube-prometheus-stack or set prometheus_address via kwctl config later"
    fi
    return 0
  fi
  log "patching Prometheus URL: ${url}"
  kubectl -n "${NAMESPACE}" get configmap kubewise-agent-config -o yaml | \
    sed "s|prometheus_address:.*|prometheus_address: ${url}|" | \
    kubectl apply -f -
}

save_kwctl_profile() {
  command -v kwctl >/dev/null 2>&1 || return 0
  kwctl profile set \
    "agent-url=http://localhost:8080" \
    "agent-namespace=${NAMESPACE}" 2>/dev/null || true
}

print_done() {
  cat <<EOF

╔══════════════════════════════════════════════════════════╗
║  KubeWise installed                                      ║
╚══════════════════════════════════════════════════════════╝

  1) Port-forward (keep this running):
     kubectl -n ${NAMESPACE} port-forward svc/kubewise-agent 8080:8080

  2) Open the control center:
     kwctl ui

  Or verify: kwctl connect

EOF
}

if [[ "${LOCAL}" -eq 1 ]]; then
  local_dev_install
else
  remote_cluster_install
fi
