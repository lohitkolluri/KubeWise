# KubeWise — standard developer targets
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

ROOT_DIR    := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
BIN_DIR     ?= $(ROOT_DIR)/bin
AGENT_IMAGE ?= kubewise/agent:dev
FCST_IMAGE  ?= kubewise/forecaster:dev
KIND_CLUSTER ?= kubewise
KUBEWISE_NAMESPACE ?= kubewise
TARGETARCH  ?= $(shell uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')

export ROOT_DIR BIN_DIR AGENT_IMAGE FCST_IMAGE KIND_CLUSTER KUBEWISE_NAMESPACE TARGETARCH

bootstrap: ## One-click install script (curl | bash)
	@./hack/bootstrap.sh --yes

dev: ## Local kind cluster + build + deploy (set OPENROUTER_API_KEY for LLM)
	@./hack/bootstrap.sh --local --yes

install-cluster: ## kwctl install --yes (requires kubectl)
	@go run ./cmd/kwctl install --yes

.PHONY: help build build-agent build-kwctl test test-forecaster e2e lint fmt vet golangci helm-lint \
        docker-agent docker-forecaster docker-all bootstrap dev install-cluster \
        kind-up deploy-dev deploy apply-manifests rollout port-forward clean

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_.-]+:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

build: build-agent build-kwctl ## Build agent (linux) and kwctl (host)

build-agent: ## Cross-compile agent for TARGETARCH (linux)
	@source scripts/lib.sh && go_build_agent "$(BIN_DIR)/agent"

build-kwctl: ## Build kwctl for host OS/arch
	@source scripts/lib.sh && go_build_kwctl "$(BIN_DIR)/kwctl"

test: ## Run all Go tests
	go test ./... -count=1

test-forecaster: docker-forecaster ## Run forecaster ETS unit tests in container
	docker run --rm $(FCST_IMAGE) python -m unittest test_server.py -v

e2e: ## Full local E2E verification (cluster + CLI + API + forecaster)
	@./hack/e2e-verify.sh

vet: ## Run go vet
	go vet ./...

fmt: ## Check gofmt (fails if unformatted)
	@test -z "$$(gofmt -l . | grep -v '^$$')" || (gofmt -l . && exit 1)

lint: vet fmt ## Static checks (vet + fmt)

golangci: ## Run golangci-lint (install: go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest)
	@golangci-lint run ./...

helm-lint: ## Validate Helm chart templates
	@helm lint ./charts/kubewise
	@helm template kubewise ./charts/kubewise >/dev/null

docker-agent: ## Build agent container image
	@source scripts/lib.sh && docker_build_agent "$(AGENT_IMAGE)"

docker-forecaster: ## Build forecaster sidecar image
	@source scripts/lib.sh && docker_build_forecaster "$(FCST_IMAGE)"

docker-all: docker-agent docker-forecaster ## Build all images

kind-up: ## Create kind cluster + Prometheus + KubeWise
	@./hack/kind-cluster.sh

deploy-dev: ## Build images, load into kind, apply manifests, rollout
	@./hack/deploy-dev.sh

deploy: apply-manifests rollout ## Apply manifests and restart agent

apply-manifests: ## kubectl apply -k overlays/dev
	@source scripts/lib.sh && kubectl_apply_manifests dev

rollout: ## Restart agent deployment
	@source scripts/lib.sh && rollout_agent

port-forward: ## (Fallback) manual port-forward to localhost:8080
	@echo "If installed via Helm:      kubectl -n $(KUBEWISE_NAMESPACE) port-forward svc/kubewise 8080:8080"
	@echo "If installed via manifests: kubectl -n $(KUBEWISE_NAMESPACE) port-forward svc/kubewise-agent 8080:8080"

clean: ## Remove build artifacts
	rm -rf "$(BIN_DIR)"
