# syntax=docker/dockerfile:1

# ── Build stage (optional cross-compile via pre-built bin/agent) ──────────────
FROM --platform=$BUILDPLATFORM golang:1.26-bookworm AS builder
ARG TARGETOS=linux
ARG TARGETARCH=amd64
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags="-s -w" -o /out/agent ./cmd/agent

# ── Runtime ──────────────────────────────────────────────────────────────────
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=builder /out/agent /agent
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/agent"]
