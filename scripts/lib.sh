#!/usr/bin/env bash
# Shared helpers for KubeWise shell scripts.
# shellcheck shell=bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

require_cmd() {
  local cmd
  for cmd in "$@"; do
    command -v "${cmd}" >/dev/null 2>&1 || die "required command not found: ${cmd}"
  done
}

detect_os() {
  case "$(uname -s)" in
    Darwin) echo "darwin" ;;
    Linux)  echo "linux" ;;
    *) die "unsupported OS: $(uname -s)" ;;
  esac
}

detect_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo "amd64" ;;
    aarch64|arm64) echo "arm64" ;;
    *) die "unsupported architecture: $(uname -m)" ;;
  esac
}

# Target platform for in-cluster agent images (linux container arch).
detect_target_platform() {
  local arch="${TARGETARCH:-}"
  if [[ -z "${arch}" ]]; then
    case "$(uname -m)" in
      x86_64|amd64) arch="amd64" ;;
      aarch64|arm64) arch="arm64" ;;
      *) arch="amd64" ;;
    esac
  fi
  echo "linux/${arch}"
}

go_build_agent() {
  local out="${1:-${ROOT_DIR}/bin/agent}"
  local platform
  platform="$(detect_target_platform)"
  local goos="${platform%/*}"
  local goarch="${platform#*/}"
  mkdir -p "$(dirname "${out}")"
  log "building agent (${goos}/${goarch}) -> ${out}"
  CGO_ENABLED=0 GOOS="${goos}" GOARCH="${goarch}" \
    go build -trimpath -ldflags="-s -w -X github.com/lohitkolluri/KubeWise/internal/version.Version=dev" -o "${out}" "${ROOT_DIR}/cmd/agent"
}

go_build_kwctl() {
  local out="${1:-${ROOT_DIR}/bin/kwctl}"
  mkdir -p "$(dirname "${out}")"
  log "building kwctl ($(detect_os)/$(detect_arch)) -> ${out}"
  CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X github.com/lohitkolluri/KubeWise/internal/version.Version=dev" -o "${out}" "${ROOT_DIR}/cmd/kwctl"
}

docker_build_agent() {
  local tag="${1:-kubewise/agent:dev}"
  local platform
  platform="$(detect_target_platform)"
  log "building agent image ${tag} (${platform})"
  docker build --platform "${platform}" -t "${tag}" \
    -f "${ROOT_DIR}/docker/agent/Dockerfile" "${ROOT_DIR}"
}

docker_build_forecaster() {
  local tag="${1:-kubewise/forecaster:dev}"
  local platform
  platform="$(detect_target_platform)"
  log "building forecaster image ${tag} (${platform})"
  docker build --platform "${platform}" -t "${tag}" "${ROOT_DIR}/forecaster-sidecar"
}

kind_load_images() {
  local cluster="${KIND_CLUSTER:-kubewise}"
  require_cmd kind
  shift || true
  for img in "$@"; do
    log "loading ${img} into kind cluster ${cluster}"
    kind load docker-image "${img}" --name "${cluster}"
  done
}

kubectl_apply_manifests() {
  local overlay="${1:-dev}"
  require_cmd kubectl
  log "applying kustomize overlay ${overlay}"
  kubectl apply -k "${ROOT_DIR}/manifests/overlays/${overlay}"
}

rollout_agent() {
  require_cmd kubectl
  local ns="${KUBEWISE_NAMESPACE:-kubewise}"
  kubectl -n "${ns}" rollout restart deployment/kubewise-agent
  kubectl -n "${ns}" rollout status deployment/kubewise-agent --timeout=180s
}
