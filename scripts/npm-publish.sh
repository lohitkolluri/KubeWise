#!/usr/bin/env bash
# Assemble platform npm packages from GoReleaser dist/ and publish to npm.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
source "${ROOT_DIR}/scripts/lib.sh"

TAG="${1:-}"
DIST_DIR="${2:-${ROOT_DIR}/dist}"
DRY_RUN="${DRY_RUN:-false}"

if [[ -z "${TAG}" ]]; then
  die "usage: npm-publish.sh <tag> [dist-dir]  (e.g. v0.2.0)"
fi

VERSION="${TAG#v}"
if [[ -z "${VERSION}" ]]; then
  die "invalid tag: ${TAG}"
fi

require_cmd tar npm node

STAGING="$(mktemp -d)"
trap 'rm -rf "${STAGING}"' EXIT

log "staging npm packages for version ${VERSION}"

# Colon-separated: npm-package-name:archive:os:cpu
PLATFORMS=(
  "kwctl-darwin-arm64:kwctl_darwin_arm64.tar.gz:darwin:arm64"
  "kwctl-darwin-amd64:kwctl_darwin_amd64.tar.gz:darwin:x64"
  "kwctl-linux-arm64:kwctl_linux_arm64.tar.gz:linux:arm64"
  "kwctl-linux-amd64:kwctl_linux_amd64.tar.gz:linux:x64"
)

publish_pkg() {
  local dir="$1"
  if [[ "${DRY_RUN}" == "true" ]]; then
    log "[dry-run] would publish $(basename "${dir}")"
    return 0
  fi
  log "publishing $(basename "${dir}")"
  (cd "${dir}" && npm publish --access public)
}

for entry in "${PLATFORMS[@]}"; do
  IFS=: read -r pkg_name archive npm_os npm_cpu <<< "${entry}"
  archive_path="${DIST_DIR}/${archive}"
  [[ -f "${archive_path}" ]] || die "missing release archive: ${archive_path}"

  pkg_dir="${STAGING}/${pkg_name}"
  mkdir -p "${pkg_dir}/bin"
  tar -xzf "${archive_path}" -C "${pkg_dir}/bin" kwctl
  chmod 755 "${pkg_dir}/bin/kwctl"

  cat > "${pkg_dir}/package.json" <<EOF
{
  "name": "${pkg_name}",
  "version": "${VERSION}",
  "description": "kwctl binary for ${npm_os}/${npm_cpu}",
  "license": "Apache-2.0",
  "repository": {
    "type": "git",
    "url": "git+https://github.com/lohitkolluri/KubeWise.git"
  },
  "os": ["${npm_os}"],
  "cpu": ["${npm_cpu}"],
  "preferUnplugged": true,
  "files": ["bin"]
}
EOF

  publish_pkg "${pkg_dir}"
done

kwctl_dir="${STAGING}/kwctl"
mkdir -p "${kwctl_dir}/bin"
cp "${ROOT_DIR}/npm/kwctl/bin/kwctl.js" "${kwctl_dir}/bin/kwctl.js"
chmod 755 "${kwctl_dir}/bin/kwctl.js"

cat > "${kwctl_dir}/package.json" <<EOF
{
  "name": "kwctl",
  "version": "${VERSION}",
  "description": "CLI for KubeWise — predictive Kubernetes monitoring and remediation",
  "license": "Apache-2.0",
  "repository": {
    "type": "git",
    "url": "git+https://github.com/lohitkolluri/KubeWise.git"
  },
  "homepage": "https://github.com/lohitkolluri/KubeWise#readme",
  "bugs": {
    "url": "https://github.com/lohitkolluri/KubeWise/issues"
  },
  "keywords": ["kubernetes", "k8s", "sre", "monitoring", "cli", "kubewise"],
  "engines": {
    "node": ">=16"
  },
  "bin": {
    "kwctl": "bin/kwctl.js"
  },
  "files": ["bin"],
  "optionalDependencies": {
    "kwctl-darwin-arm64": "${VERSION}",
    "kwctl-darwin-amd64": "${VERSION}",
    "kwctl-linux-arm64": "${VERSION}",
    "kwctl-linux-amd64": "${VERSION}"
  }
}
EOF

publish_pkg "${kwctl_dir}"

cli_dir="${STAGING}/kubewise-cli"
mkdir -p "${cli_dir}/bin"
cp "${ROOT_DIR}/npm/kubewise-cli/bin/kwctl.js" "${cli_dir}/bin/kwctl.js"
cp "${ROOT_DIR}/npm/kubewise-cli/bin/kubewise-cli.js" "${cli_dir}/bin/kubewise-cli.js"
chmod 755 "${cli_dir}/bin/"*.js

cat > "${cli_dir}/package.json" <<EOF
{
  "name": "kubewise-cli",
  "version": "${VERSION}",
  "description": "KubeWise CLI (kwctl) — install via npm",
  "license": "Apache-2.0",
  "repository": {
    "type": "git",
    "url": "git+https://github.com/lohitkolluri/KubeWise.git"
  },
  "homepage": "https://github.com/lohitkolluri/KubeWise#readme",
  "bugs": {
    "url": "https://github.com/lohitkolluri/KubeWise/issues"
  },
  "keywords": ["kubernetes", "k8s", "sre", "monitoring", "cli", "kubewise", "kwctl"],
  "engines": {
    "node": ">=16"
  },
  "bin": {
    "kwctl": "bin/kwctl.js",
    "kubewise-cli": "bin/kubewise-cli.js"
  },
  "files": ["bin"],
  "dependencies": {
    "kwctl": "${VERSION}"
  }
}
EOF

publish_pkg "${cli_dir}"

log "npm publish complete for ${VERSION}"
