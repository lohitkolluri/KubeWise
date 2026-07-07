# syntax=docker/dockerfile:1

# ── Build stage (optional cross-compile via pre-built bin/agent) ──────────────
FROM --platform=$BUILDPLATFORM golang:1.26-bookworm AS builder
ARG TARGETOS
ARG TARGETARCH
ARG VERSION=dev
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build -trimpath \
    -ldflags="-s -w -X github.com/lohitkolluri/KubeWise/internal/version.Version=${VERSION}" \
    -o /out/agent ./cmd/agent

# ── Runtime (match target platform from build) ───────────────────────────────
FROM --platform=$TARGETPLATFORM gcr.io/distroless/static-debian12:nonroot
COPY --from=builder /out/agent /agent
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/agent"]
