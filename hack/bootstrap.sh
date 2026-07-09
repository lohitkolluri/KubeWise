#!/usr/bin/env bash
# KubeWise one-click installer — cluster + optional kwctl CLI.
#
#   curl -fsSL https://raw.githubusercontent.com/lohitkolluri/KubeWise/main/hack/bootstrap.sh | bash
#
# Options (env or flags):
#   --yes / -y          non-interactive
#   --local             kind + build images (laptop dev)
#   OPENROUTER_API_KEY  optional LLM key
#   KUBEWISE_REF        git ref for remote manifests (default: main)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/lib.sh
source "${SCRIPT_DIR}/../scripts/lib.sh"

REPO="${REPO:-lohitkolluri/KubeWise}"
REF="${KUBEWISE_REF:-main}"
OVERLAY="${OVERLAY:-install}"
NAMESPACE="${KUBEWISE_NAMESPACE:-kubewise}"
YES=0
LOCAL=0
KUSTOMIZE=0
INSTALL_KWCTL="${INSTALL_KWCTL:-1}"
WITH_PROMETHEUS="${WITH_PROMETHEUS:-auto}"
# CHART_VERSION: leave empty to auto-detect the latest release tag from GitHub.
# Set explicitly to pin a specific chart version.
CHART_VERSION="${CHART_VERSION:-}"

usage() {
  cat <<EOF
KubeWise bootstrap — install agent into Kubernetes with minimal setup.

Usage:
  bootstrap.sh [options]

Options:
  -y, --yes       non-interactive
  --local         dev: create/use kind, build images, deploy (requires docker+kind)
  --kustomize     install via kubectl apply -k instead of Helm (legacy)
  -h, --help      show help

Environment:
  OPENROUTER_API_KEY     optional LLM API key
  KUBEWISE_API_TOKEN     optional agent HTTP API token
  KUBEWISE_REF           manifest git ref (default: main)
  CHART_VERSION          Helm chart version to install (default: latest release)
  INSTALL_KWCTL=0        skip kwctl CLI install

Examples:
  curl -fsSL .../hack/bootstrap.sh | bash
  OPENROUTER_API_KEY=sk-... ./hack/bootstrap.sh --yes
  ./hack/bootstrap.sh --kustomize
  ./hack/bootstrap.sh --local
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) YES=1; shift ;;
    --local) LOCAL=1; shift ;;
    --kustomize) KUSTOMIZE=1; shift ;;
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
  require_cmd kind docker kubectl make helm
  if ! docker info >/dev/null 2>&1; then
    die "Docker is not running. Start Docker Desktop, then run: ./hack/bootstrap.sh --local --yes"
  fi
  export KUBEWISE_NAMESPACE="${NAMESPACE}"
  ensure_kind_cluster
  make -C "${ROOT_DIR}" deploy-dev OVERLAY=dev
  apply_secret
  patch_prometheus_if_found
  if [[ -n "${OPENROUTER_API_KEY:-}" || -n "${KUBEWISE_API_TOKEN:-}" ]]; then
    log "restarting agent to pick up secrets"
    kubectl -n "${NAMESPACE}" rollout restart deployment/kubewise-agent
    kubectl -n "${NAMESPACE}" rollout status deployment/kubewise-agent --timeout=180s
  fi
  save_kwctl_profile
  print_done
  echo ""
  echo "  Quick start:  kwctl up && kwctl ui"
}

ensure_kind_cluster() {
  local cluster="${KIND_CLUSTER:-kubewise}"
  if kind get clusters 2>/dev/null | grep -qx "${cluster}"; then
    if kubectl cluster-info >/dev/null 2>&1; then
      log "using existing kind cluster ${cluster}"
      return 0
    fi
    warn "kind cluster ${cluster} is not reachable — recreating"
    kind delete cluster --name "${cluster}" || true
  fi
  log "creating kind cluster (first run may take several minutes)"
  bash "${SCRIPT_DIR}/kind-cluster.sh"
}

fetch_latest_version() {
  curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | grep '"tag_name"' | cut -d'"' -f4 | sed 's/^v//'
}

install_via_kustomize() {
  require_cmd kubectl curl
  log "checking cluster access"
  kubectl cluster-info >/dev/null

  local kustomize_url="github.com/${REPO}/manifests/overlays/${OVERLAY}?ref=${REF}"
  log "applying manifests: ${kustomize_url}"
  kubectl apply -k "${kustomize_url}"

  apply_secret
  patch_prometheus_if_found

  log "waiting for agent rollout"
  kubectl -n "${NAMESPACE}" rollout status deployment/kubewise-agent --timeout=180s
}

install_via_helm() {
  require_cmd helm kubectl

  local version="${CHART_VERSION}"
  if [[ -z "${version}" ]]; then
    log "detecting latest release version"
    version="$(fetch_latest_version)" || die "failed to detect latest release — set CHART_VERSION explicitly"
  fi

  local chart_ref="oci://ghcr.io/lohitkolluri/charts/kubewise"
  log "installing kubewise ${version} from GHCR (Helm)"
  log "chart: ${chart_ref}"

  local helm_args=(
    upgrade --install kubewise "${chart_ref}"
    --namespace "${NAMESPACE}" --create-namespace
    --version "${version}"
    --wait --timeout 5m
  )
  [[ -n "${OPENROUTER_API_KEY:-}" ]] && helm_args+=(
    --set "secrets.openrouterApiKey=${OPENROUTER_API_KEY}"
  )
  [[ -n "${KUBEWISE_API_TOKEN:-}" ]] && helm_args+=(
    --set "secrets.apiToken=${KUBEWISE_API_TOKEN}"
    --set "security.requireApiToken=true"
  )

  helm "${helm_args[@]}"

  log "waiting for agent rollout"
  kubectl -n "${NAMESPACE}" rollout status deployment/kubewise --timeout=180s
}

remote_cluster_install() {
  install_kwctl_cli
  if [[ "${KUSTOMIZE}" -eq 1 ]]; then
    install_via_kustomize
  else
    install_via_helm
  fi
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
  local args=(create secret generic kubewise-secret -n "${NAMESPACE}" --dry-run=client -o yaml)
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
  # ConfigMap name differs between install modes:
  # - Manifests: kubewise-agent-config
  # - Helm:      kubewise-config (chart fullname + "-config")
  local cm=""
  for name in kubewise-agent-config kubewise-config; do
    if kubectl -n "${NAMESPACE}" get configmap "${name}" >/dev/null 2>&1; then
      cm="${name}"
      break
    fi
  done
  if [[ -z "${cm}" ]]; then
    warn "configmap not found (expected kubewise-agent-config or kubewise-config) — skipping Prometheus patch"
    return 0
  fi
  kubectl -n "${NAMESPACE}" get configmap "${cm}" -o yaml | \
    sed "s|prometheus_address:.*|prometheus_address: ${url}|" | \
    kubectl apply -f -
}

save_kwctl_profile() {
  local kwctl_bin=""
  if [[ -x "${ROOT_DIR}/bin/kwctl" ]]; then
    kwctl_bin="${ROOT_DIR}/bin/kwctl"
  elif command -v kwctl >/dev/null 2>&1; then
    kwctl_bin="$(command -v kwctl)"
  fi
  [[ -n "${kwctl_bin}" ]] || return 0
  "${kwctl_bin}" profile set \
    "agent-url=http://localhost:8080" \
    "agent-namespace=${NAMESPACE}" 2>/dev/null || true
}

print_done() {
  cat <<EOF

╔══════════════════════════════════════════════════════════╗
║  KubeWise installed                                      ║
╚══════════════════════════════════════════════════════════╝

  1) Connect to the agent API:
     kwctl up

  2) Open the control center:
     kwctl ui

  Or verify: kwctl connect

  Manual port-forward (fallback):
    - Helm install:      kubectl -n ${NAMESPACE} port-forward svc/kubewise 8080:8080
    - Manifests install: kubectl -n ${NAMESPACE} port-forward svc/kubewise-agent 8080:8080

EOF
}

if [[ "${LOCAL}" -eq 1 ]]; then
  local_dev_install
else
  remote_cluster_install
fi
