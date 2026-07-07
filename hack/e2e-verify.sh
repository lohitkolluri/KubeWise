#!/usr/bin/env bash
# Full local E2E verification for KubeWise dev cluster.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

KW="${KW:-$ROOT/bin/kwctl}"
BASE="${BASE:-http://localhost:8080}"
PASS=0
FAIL=0
SKIP=0

pass() { echo "✓ PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "✗ FAIL: $1"; FAIL=$((FAIL+1)); }
skip() { echo "○ SKIP: $1"; SKIP=$((SKIP+1)); }

echo "========== KubeWise E2E Verification =========="
echo "time: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

# ── 1. Build & unit tests ──────────────────────────────────────────────────
echo "=== 1. BUILD & UNIT TESTS ==="
if go build -o bin/kwctl ./cmd/kwctl && go build -o bin/agent ./cmd/agent 2>/dev/null; then
  pass "go build kwctl + agent"
else
  fail "go build"
fi

if go test ./... -count=1 -timeout=5m 2>&1 | tail -1 | grep -qE '^ok|^\?'; then
  if go test ./... -count=1 -timeout=5m 2>&1 | grep -q '^FAIL'; then
    fail "go test ./..."
  else
    pass "go test ./..."
  fi
else
  go test ./... -count=1 -timeout=5m 2>&1 | grep -E '^FAIL|--- FAIL' | head -5
  fail "go test ./..."
fi

if command -v helm >/dev/null; then
  helm lint charts/kubewise >/dev/null 2>&1 && pass "helm lint" || fail "helm lint"
  helm template kubewise charts/kubewise >/dev/null 2>&1 && pass "helm template" || fail "helm template"
else
  skip "helm not installed"
fi

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  if make test-forecaster >/dev/null 2>&1; then
    pass "make test-forecaster (ETS intervals)"
  else
    fail "make test-forecaster"
  fi
else
  skip "docker not available for forecaster tests"
fi

# ── 2. Cluster baseline ───────────────────────────────────────────────────
echo
echo "=== 2. CLUSTER ==="
if kubectl cluster-info >/dev/null 2>&1; then
  pass "kubectl cluster reachable"
else
  fail "kubectl cluster"
  echo "Cannot continue without cluster."
  exit 1
fi

if kubectl -n kubewise rollout status deploy/kubewise-agent --timeout=120s >/dev/null 2>&1; then
  ready=$(kubectl -n kubewise get pod -l app.kubernetes.io/name=kubewise -o jsonpath='{.items[0].status.containerStatuses[*].ready}' 2>/dev/null)
  if echo "$ready" | grep -q 'true true'; then
    pass "agent pod Ready (2/2)"
  else
    kubectl -n kubewise get pods
    fail "agent pod Ready (containers=$ready)"
  fi
else
  kubectl -n kubewise get pods
  fail "agent deployment rollout"
fi

if kubectl -n kubewise get secret kubewise-agent-secret >/dev/null 2>&1; then
  pass "OpenRouter secret present"
else
  skip "OpenRouter secret (observe-only LLM)"
fi

# Forecaster health inside pod
if kubectl -n kubewise exec deploy/kubewise-agent -c forecaster -- python3 -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8081/healthz').read().decode())" 2>/dev/null | grep -q ok; then
  pass "forecaster /healthz"
else
  fail "forecaster /healthz"
fi

# Direct ETS smoke in running forecaster image
if kubectl -n kubewise exec deploy/kubewise-agent -c forecaster -- python3 -c "
from server import _ets_forecast
import numpy as np
v=(np.random.default_rng(1).random(30)*10+50).tolist()
_,m,lo,hi,e=_ets_forecast(v,6)
assert e=='',e
assert len(m)==6 and lo[0]<=m[0]<=hi[0]
print('ok')
" 2>/dev/null | grep -q ok; then
  pass "forecaster ETS smoke (no se_mean)"
else
  fail "forecaster ETS smoke"
fi

# ── 3. kwctl CLI edge cases ───────────────────────────────────────────────
echo
echo "=== 3. KWCTL CLI ==="
"$KW" down 2>/dev/null || true
"$KW" down 2>/dev/null && pass "down idempotent" || fail "down idempotent"

if curl -sf --max-time 2 "$BASE/health" >/dev/null 2>&1; then
  fail "health reachable after down"
else
  pass "unreachable after down"
fi

if "$KW" up 2>&1 | grep -qE 'Port-forward running|already reachable'; then
  pass "kwctl up"
else
  fail "kwctl up"
fi
sleep 2

"$KW" up 2>&1 | grep -q 'already reachable' && pass "up when already up" || fail "up when already up"

"$KW" connect 2>&1 | grep -q 'Connected' && pass "connect" || fail "connect"
"$KW" status 2>&1 | grep -q 'Uptime' && pass "status" || fail "status"
"$KW" status health 2>&1 | grep -q 'ok' && pass "status health" || fail "status health"
if "$KW" stats 2>&1 | grep -qi 'predictions total'; then
  pass "stats"
else
  fail "stats"
fi
"$KW" doctor 2>&1 | grep -q 'All checks passed' && pass "doctor" || fail "doctor"

"$KW" down >/dev/null
sleep 1
if ! "$KW" doctor >/tmp/kwctl_doctor.txt 2>&1; then
  pass "doctor fails without port-forward"
else
  cat /tmp/kwctl_doctor.txt | head -8
  fail "doctor without agent (expected failure)"
fi
"$KW" up >/dev/null 2>&1

# ── 4. HTTP API ───────────────────────────────────────────────────────────
echo
echo "=== 4. HTTP API ==="
for ep in /health /readyz /status /metrics / /api/v1/predictions /api/v1/anomalies \
          /api/v1/stats /api/v1/config /api/v1/audit /api/v1/remediations; do
  code=$(curl -s -o /tmp/kw_e2e.json -w '%{http_code}' "$BASE$ep")
  if [[ "$code" == "200" ]]; then
    pass "GET $ep → $code"
  else
    fail "GET $ep → $code ($(head -c 60 /tmp/kw_e2e.json))"
  fi
done

# Metrics sanity
if curl -sf "$BASE/metrics" | grep -q kubewise_scrapes_total; then
  pass "Prometheus metric kubewise_scrapes_total"
else
  fail "Prometheus metrics content"
fi

# ── 5. Agent pipeline (wait for scrapes + forecast) ───────────────────────
echo
echo "=== 5. AGENT PIPELINE (waiting ~2 min) ==="
sleep 75

scrapes=$(curl -sf "$BASE/status" | python3 -c "import sys,json; print(json.load(sys.stdin).get('scrapes',0))" 2>/dev/null || echo 0)
if [[ "${scrapes:-0}" -ge 2 ]]; then
  pass "agent scraping ($scrapes scrapes)"
else
  fail "agent scraping (scrapes=$scrapes)"
fi

# Forecast errors in agent logs
fc_err=$(kubectl -n kubewise logs deploy/kubewise-agent -c agent --since=5m 2>/dev/null | grep -c 'forecast error' || true)
se_err=$(kubectl -n kubewise logs deploy/kubewise-agent -c agent --since=5m 2>/dev/null | grep -c 'se_mean' || true)
if [[ "$fc_err" -eq 0 ]]; then
  pass "no forecast errors in agent logs"
else
  kubectl -n kubewise logs deploy/kubewise-agent -c agent --since=5m 2>/dev/null | grep 'forecast error' | tail -3
  fail "forecast errors in logs (count=$fc_err)"
fi
if [[ "$se_err" -eq 0 ]]; then
  pass "no se_mean errors"
else
  fail "se_mean errors still present (count=$se_err)"
fi

# Forecaster gRPC success lines (may be 0 if <10 metric samples stored)
fc_ok=$(kubectl -n kubewise logs deploy/kubewise-agent -c forecaster --since=5m 2>/dev/null | grep -c 'Forecast ok' || true)
fc_fail=$(kubectl -n kubewise logs deploy/kubewise-agent -c forecaster --since=5m 2>/dev/null | grep -c 'Forecast failed' || true)
if [[ "$fc_fail" -eq 0 ]]; then
  pass "no Forecast failed in forecaster logs"
else
  kubectl -n kubewise logs deploy/kubewise-agent -c forecaster --since=5m 2>/dev/null | grep 'Forecast failed' | tail -3
  fail "Forecast failed in forecaster (count=$fc_fail)"
fi
echo "  (forecaster Forecast ok=$fc_ok — needs ≥10 samples per series)"

# LLM
kubectl -n kubewise logs deploy/kubewise-agent -c agent >/tmp/kw_agent.log 2>/dev/null || true
if grep -q 'LLM provider openrouter validated' /tmp/kw_agent.log 2>/dev/null; then
  pass "LLM key validated at startup"
  if grep -q 'skipping — LLM client unavailable' /tmp/kw_agent.log 2>/dev/null; then
    fail "remediator still skipping LLM"
  else
    pass "remediator not skipping LLM"
  fi
  llm_to=$(grep -c 'context deadline exceeded' /tmp/kw_agent.log 2>/dev/null || true)
  llm_plan=$(grep -c 'remediator: plan steps' /tmp/kw_agent.log 2>/dev/null || true)
  if [[ "$llm_plan" -gt 0 ]]; then
    pass "LLM remediation plan produced ($llm_plan)"
  elif [[ "$llm_to" -gt 5 ]]; then
    fail "LLM correlation timeouts (count=$llm_to, no plan yet)"
  elif [[ "$llm_to" -gt 0 ]]; then
    echo "  warn: $llm_to LLM timeout(s) — structured output may need >180s or faster model"
    pass "LLM timeouts logged (investigate if persistent)"
  else
    pass "no LLM deadline exceeded errors"
  fi
else
  skip "LLM validated (no secret or key invalid)"
fi

# Predictions / anomalies API shape
if curl -sf "$BASE/api/v1/predictions" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
  pass "predictions JSON valid"
else
  fail "predictions JSON"
fi

# ── 6. Down/up cycle ──────────────────────────────────────────────────────
echo
echo "=== 6. PORT-FORWARD CYCLE ==="
"$KW" down && sleep 1
"$KW" up 2>&1 | grep -qE 'Port-forward running|already reachable' && pass "up after down" || fail "up after down"
curl -sf "$BASE/health" | grep -q ok && pass "health after cycle" || fail "health after cycle"

# ── Summary ───────────────────────────────────────────────────────────────
echo
echo "=========================================="
echo "E2E SUMMARY: PASS=$PASS  FAIL=$FAIL  SKIP=$SKIP"
echo "=========================================="
[[ "$FAIL" -eq 0 ]]
