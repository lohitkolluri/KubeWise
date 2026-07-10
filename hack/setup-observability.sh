#!/usr/bin/env bash
# KubeWise Observability Stack Installer
#
# Auto-detects existing Prometheus, Loki, and Tempo services.
# Installs only what's missing — with minimal resource profiles.
# Outputs connection URLs for KubeWise configuration.
#
# Usage:
#   ./hack/setup-observability.sh               # detect + install missing components
#   ./hack/setup-observability.sh --dry-run      # show what would happen, no changes
#   PROM_NS=monitoring LOKI_NS=kubewise ./hack/setup-observability.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Defaults ──────────────────────────────────────────────────────────────────
PROM_NS="${PROM_NS:-monitoring}"
LOKI_NS="${LOKI_NS:-kubewise}"
TEMPO_NS="${TEMPO_NS:-kubewise}"
KUBEWISE_NS="${KUBEWISE_NS:-kubewise}"
DRY_RUN=0
VERBOSE=0
INSTALL_PROMETHEUS="${INSTALL_PROMETHEUS:-auto}"
INSTALL_LOKI="${INSTALL_LOKI:-auto}"
INSTALL_TEMPO="${INSTALL_TEMPO:-auto}"

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { printf "${GREEN}==>${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}WARN:${NC} %s\n" "$*" >&2; }
err()  { printf "${RED}ERROR:${NC} %s\n" "$*" >&2; }
info() { printf "${CYAN}  ·${NC} %s\n" "$*"; }
header() { printf "\n${BOLD}%s${NC}\n" "$*"; }

# ── Help ──────────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
KubeWise Observability Stack Installer — auto-detect + minimal install

Detects existing Prometheus, Loki, and Tempo. Installs only what's
missing, with resource-optimized configurations optimized for development.

Usage:
  $0 [options]

Options:
  --dry-run       Show what would be installed, no changes
  --verbose       Show Helm output during install
  --prom-ns       Prometheus namespace (default: monitoring)
  --loki-ns       Loki namespace (default: kubewise)
  --tempo-ns      Tempo namespace (default: kubewise)
  -h, --help      Show this help
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --verbose) VERBOSE=1; shift ;;
    --prom-ns) PROM_NS="$2"; shift 2 ;;
    --loki-ns) LOKI_NS="$2"; shift 2 ;;
    --tempo-ns) TEMPO_NS="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) err "unknown option: $1"; usage ;;
  esac
done

require_cmd() {
  for cmd in "$@"; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
      err "required command not found: ${cmd}"
      exit 1
    fi
  done
}

run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "[DRY-RUN] $*"
    return 0
  fi
  "$@"
}

# ═══════════════════════════════════════════════════════════════════════════════
# Service detection helpers
# ═══════════════════════════════════════════════════════════════════════════════

# detect_service <namespace> <name-pattern...> → svc.namespace.svc.cluster.local:port
detect_service() {
  local ns="$1"; shift
  local names=("$@")
  for name in "${names[@]}"; do
    local svc
    svc="$(kubectl get svc -n "${ns}" "${name}" -o name 2>/dev/null || true)"
    if [[ -n "${svc}" ]]; then
      local ports
      ports="$(kubectl get "${svc}" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || echo "")"
      if [[ -n "${ports}" ]]; then
        echo "${name}.${ns}.svc.cluster.local:${ports}"
        return 0
      fi
    fi
  done
  return 1
}

detect_prometheus() {
  local candidates=(
    "kube-prometheus-stack-prometheus"
    "prometheus-kube-prometheus-prometheus"
    "prometheus-server"
    "prometheus"
    "prometheus-operated"
  )
  detect_service "${PROM_NS}" "${candidates[@]}" || detect_service "monitoring" "${candidates[@]}"
}

detect_loki() {
  local candidates=(
    "kubewise-loki"
    "kubewise-loki-gateway"
    "loki-gateway"
    "loki"
    "loki-headless"
  )
  detect_service "${LOKI_NS}" "${candidates[@]}" || detect_service "monitoring" "loki" "loki-gateway"
}

detect_tempo() {
  local candidates=(
    "kubewise-tempo"
    "kubewise-tempo-query-frontend"
    "tempo"
    "tempo-query-frontend"
    "tempo-headless"
  )
  detect_service "${TEMPO_NS}" "${candidates[@]}" || detect_service "monitoring" "tempo" "tempo-query-frontend"
}

# ═══════════════════════════════════════════════════════════════════════════════
# Minimal kube-prometheus-stack values (no Grafana, no AlertManager, lean Prom)
# ═══════════════════════════════════════════════════════════════════════════════
generate_prometheus_values() {
  cat <<'YAML'
# Minimal kube-prometheus-stack for KubeWise dev
# No Grafana, no AlertManager, reduced resources

alertmanager:
  enabled: false

grafana:
  enabled: false

prometheus:
  prometheusSpec:
    resources:
      requests:
        cpu: 200m
        memory: 512Mi
      limits:
        cpu: 500m
        memory: 1Gi
    retention: 6h
    retentionSize: 2GB
    scrapeInterval: 30s
    evaluationInterval: 30s
    replicas: 1
    # Only scrape what KubeWise needs
    ruleNamespaceSelector: {}
    ruleSelectorNilUsesHelmValues: false
    serviceMonitorNamespaceSelector: {}
    serviceMonitorSelectorNilUsesHelmValues: false
    podMonitorNamespaceSelector: {}
    podMonitorSelectorNilUsesHelmValues: false
    probeNamespaceSelector: {}
    probeSelectorNilUsesHelmValues: false
    walCompression: true

prometheusOperator:
  resources:
    requests:
      cpu: 50m
      memory: 128Mi
    limits:
      cpu: 200m
      memory: 256Mi
  admissionWebhooks:
    enabled: false
  tls:
    enabled: false

kubeStateMetrics:
  enabled: false

nodeExporter:
  enabled: false

# Keep only essential default rules — disable heavy dashboards
defaultRules:
  create: false
YAML
}

# ═══════════════════════════════════════════════════════════════════════════════
# Main detection + install flow
# ═══════════════════════════════════════════════════════════════════════════════

main() {
  header "╔══════════════════════════════════════════════════════════╗"
  header "║  KubeWise Observability Stack Setup                     ║"
  header "╚══════════════════════════════════════════════════════════╝"
  echo ""

  require_cmd kubectl helm

  PROMETHEUS_URL=""
  LOKI_URL=""
  TEMPO_URL=""

  # ── 1. Prometheus ──────────────────────────────────────────────────────────
  header "1. Prometheus (metrics)"

  PROMETHEUS_URL="$(detect_prometheus || true)"
  if [[ -n "${PROMETHEUS_URL}" ]]; then
    log "Found existing Prometheus → http://${PROMETHEUS_URL}"
  else
    if [[ "${INSTALL_PROMETHEUS}" == "skip" ]]; then
      warn "Prometheus not found, skipping install (INSTALL_PROMETHEUS=skip)"
      PROMETHEUS_URL=""
    else
      log "No Prometheus found — installing minimal kube-prometheus-stack in ${PROM_NS}"
      local values_file
      values_file="$(mktemp)"
      generate_prometheus_values > "${values_file}"

      run helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
        --namespace "${PROM_NS}" --create-namespace \
        --values "${values_file}" \
        --wait --timeout 10m \
        $([[ "${VERBOSE}" -eq 1 ]] && echo "" || echo "--quiet")

      rm -f "${values_file}"

      # Re-detect after install
      PROMETHEUS_URL="$(detect_prometheus || true)"
      if [[ -z "${PROMETHEUS_URL}" ]]; then
        # Construct URL from common patterns
        PROMETHEUS_URL="kube-prometheus-stack-prometheus.${PROM_NS}.svc.cluster.local:9090"
      fi
      log "Prometheus installed → http://${PROMETHEUS_URL}"
    fi
  fi

  # ── 2. Loki (via KubeWise Helm chart subchart) ────────────────────────────
  # Loki is bundled with the KubeWise Helm chart - install when KubeWise is deployed
  header "2. Loki (logs)"

  LOKI_URL="$(detect_loki || true)"
  if [[ -n "${LOKI_URL}" ]]; then
    log "Found existing Loki → http://${LOKI_URL}"
  else
    log "Loki not found — will be installed as KubeWise Helm chart dependency"
    info "Loki will be installed in ${LOKI_NS} namespace during KubeWise Helm deployment"
    LOKI_URL="kubewise-loki-gateway.${LOKI_NS}.svc.cluster.local"
  fi

  # ── 3. Tempo (via KubeWise Helm chart subchart) ───────────────────────────
  header "3. Tempo (traces)"

  TEMPO_URL="$(detect_tempo || true)"
  if [[ -n "${TEMPO_URL}" ]]; then
    log "Found existing Tempo → http://${TEMPO_URL}"
  else
    log "Tempo not found — will be installed as KubeWise Helm chart dependency"
    info "Tempo will be installed in ${TEMPO_NS} namespace during KubeWise Helm deployment"
    TEMPO_URL="kubewise-tempo.${TEMPO_NS}.svc.cluster.local:3200"
  fi

  # ── 4. Summary ────────────────────────────────────────────────────────────
  echo ""
  header "╔══════════════════════════════════════════════════════════╗"
  header "║  Observability Stack Summary                             ║"
  header "╚══════════════════════════════════════════════════════════╝"
  echo ""
  printf "  ${BOLD}%-12s${NC} %s\n" "Component" "URL"
  printf "  ${BOLD}%-12s${NC} %s\n" "─────────" "─────────────────────────────────"
  printf "  %-12s http://%s\n" "Prometheus" "${PROMETHEUS_URL:-<not available>}"
  printf "  %-12s http://%s\n" "Loki" "${LOKI_URL:-<not available>}"
  printf "  %-12s http://%s\n" "Tempo" "${TEMPO_URL:-<not available>}"
  echo ""

  # Export for sourcing
  if [[ -z "${DRY_RUN:-}" || "${DRY_RUN}" -eq 0 ]]; then
    export PROMETHEUS_URL LOKI_URL TEMPO_URL
    # Write to a temp file for sourcing by parent scripts
    cat > /tmp/kubewise-observability.env <<EOF
PROMETHEUS_URL=${PROMETHEUS_URL}
LOKI_URL=${LOKI_URL}
TEMPO_URL=${TEMPO_URL}
EOF
    log "Connection URLs exported — source with: source /tmp/kubewise-observability.env"
  fi
}

main "$@"
