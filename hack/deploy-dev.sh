#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="${BIN_DIR:-bin}"
IMAGE_TAG="${IMAGE_TAG:-kubewise/agent:dev}"
KIND_CLUSTER="${KIND_CLUSTER:-kubewise}"

mkdir -p "${BIN_DIR}"

echo "=== Building agent binary ==="
go build -o "${BIN_DIR}/agent" ./cmd/agent/

echo "=== Building kwctl CLI ==="
go build -o "${BIN_DIR}/kwctl" ./cmd/kwctl/

echo "=== Building Docker image ==="
docker build -t "${IMAGE_TAG}" -f - . <<'DOCKERFILE'
FROM gcr.io/distroless/static-debian12:nonroot
COPY bin/agent /agent
USER 65532:65532
ENTRYPOINT ["/agent"]
DOCKERFILE

echo "=== Loading image into kind ==="
kind load docker-image "${IMAGE_TAG}" --name "${KIND_CLUSTER}"

echo "=== Applying manifests ==="
kubectl apply -f manifests/

echo "=== Restarting agent to pick up new image ==="
kubectl -n kubewise rollout restart deployment/kubewise-agent
kubectl -n kubewise rollout status deployment/kubewise-agent --timeout=120s

echo "=== Done ==="
echo "CLI: bin/kwctl status"
