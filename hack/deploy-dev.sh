#!/usr/bin/env bash
# Build images, load into kind, apply manifests, and rollout the agent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/lib.sh
source "${SCRIPT_DIR}/../scripts/lib.sh"

BIN_DIR="${BIN_DIR:-${ROOT_DIR}/bin}"
AGENT_IMAGE="${AGENT_IMAGE:-kubewise/agent:dev}"
FCST_IMAGE="${FCST_IMAGE:-kubewise/forecaster:dev}"
KIND_CLUSTER="${KIND_CLUSTER:-kubewise}"
OVERLAY="${OVERLAY:-dev}"

require_cmd docker kubectl

mkdir -p "${BIN_DIR}"

docker_build_agent "${AGENT_IMAGE}"
go_build_kwctl "${BIN_DIR}/kwctl"
docker_build_forecaster "${FCST_IMAGE}"

kind_load_images "${AGENT_IMAGE}" "${FCST_IMAGE}"
kubectl_apply_manifests "${OVERLAY}"
rollout_agent

log "done — try: ${BIN_DIR}/kwctl status (after port-forward)"
log "port-forward: kubectl -n kubewise port-forward svc/kubewise-agent 8080:8080"
