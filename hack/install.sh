#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-lohitkolluri/KubeWise}"
VERSION="${VERSION:-latest}"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"

detect_arch() {
  local arch
  arch="$(uname -m)"
  case "$arch" in
    x86_64) echo "amd64" ;;
    aarch64|arm64) echo "arm64" ;;
    *) echo "unsupported: $arch" >&2; exit 1 ;;
  esac
}

detect_os() {
  local os
  os="$(uname -s)"
  case "$os" in
    Darwin) echo "darwin" ;;
    Linux) echo "linux" ;;
    *) echo "unsupported: $os" >&2; exit 1 ;;
  esac
}

OS="$(detect_os)"
ARCH="$(detect_arch)"
BINARY="kwctl"

if [ "$VERSION" = "latest" ]; then
  DOWNLOAD_URL="https://github.com/${REPO}/releases/latest/download/${BINARY}_${OS}_${ARCH}.tar.gz"
else
  DOWNLOAD_URL="https://github.com/${REPO}/releases/download/${VERSION}/${BINARY}_${OS}_${ARCH}.tar.gz"
fi

echo "=== Installing kwctl ${VERSION} (${OS}/${ARCH}) ==="
echo "Downloading from: ${DOWNLOAD_URL}"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR}"' EXIT

curl -fsSL "${DOWNLOAD_URL}" -o "${TMPDIR}/kwctl.tar.gz"
tar -xzf "${TMPDIR}/kwctl.tar.gz" -C "${TMPDIR}"

install "${TMPDIR}/kwctl" "${INSTALL_DIR}/kwctl"

echo "=== Installed to ${INSTALL_DIR}/kwctl ==="
kwctl --help
