#!/usr/bin/env bash
# Install kwctl from GitHub Releases.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/lib.sh
source "${SCRIPT_DIR}/../scripts/lib.sh"

REPO="${REPO:-lohitkolluri/KubeWise}"
VERSION="${VERSION:-latest}"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
BINARY="kwctl"

require_cmd curl tar install

OS="$(detect_os)"
ARCH="$(detect_arch)"

if [[ "${VERSION}" == "latest" ]]; then
  DOWNLOAD_URL="https://github.com/${REPO}/releases/latest/download/${BINARY}_${OS}_${ARCH}.tar.gz"
else
  DOWNLOAD_URL="https://github.com/${REPO}/releases/download/${VERSION}/${BINARY}_${OS}_${ARCH}.tar.gz"
fi

log "installing kwctl ${VERSION} (${OS}/${ARCH})"
log "download: ${DOWNLOAD_URL}"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR}"' EXIT

curl -fsSL "${DOWNLOAD_URL}" -o "${TMPDIR}/${BINARY}.tar.gz"
tar -xzf "${TMPDIR}/${BINARY}.tar.gz" -C "${TMPDIR}"

install -m 0755 "${TMPDIR}/${BINARY}" "${INSTALL_DIR}/${BINARY}"

log "installed ${INSTALL_DIR}/${BINARY}"
"${INSTALL_DIR}/${BINARY}" version 2>/dev/null || "${INSTALL_DIR}/${BINARY}" --help | head -3
