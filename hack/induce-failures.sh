#!/usr/bin/env bash
# Deploy synthetic failure workloads and watch KubeWise detect them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="${ROOT}/hack/test-workloads.yaml"
NS=kw-test
AGENT_URL="${AGENT_URL:-http://localhost:8080}"
WAIT_SECS="${WAIT_SECS:-120}"

echo "==> Applying test workloads (${MANIFEST})"
kubectl apply -f "$MANIFEST"

echo
echo "==> Pod status (kw-test)"
kubectl get pods -n "$NS" -o wide

echo
echo "==> Waiting ${WAIT_SECS}s for KubeWise to scrape + detect..."
sleep "$WAIT_SECS"

echo
echo "==> Failure signals in kw-test"
kubectl get events -n "$NS" --sort-by='.lastTimestamp' 2>/dev/null | tail -15

if curl -sf --max-time 3 "${AGENT_URL}/health" >/dev/null 2>&1; then
  echo
  echo "==> KubeWise anomalies (kw-test)"
  curl -sf "${AGENT_URL}/api/v1/anomalies?limit=20" | python3 -c "
import sys, json
data = json.load(sys.stdin)
rows = [a for a in data if a.get('namespace') == 'kw-test' or 'kw-test' in (a.get('entity') or '')]
if not rows:
    rows = data[:10]
for a in rows[:15]:
    print(f\"  {a.get('pattern','?'):20} {a.get('entity','?'):35} score={a.get('score',0):.2f}\")
print(f'  ({len(rows)} kw-test-related / {len(data)} total)')
"

  echo
  echo "==> KubeWise predictions"
  curl -sf "${AGENT_URL}/api/v1/predictions?limit=20" | python3 -c "
import sys, json
data = json.load(sys.stdin)
rows = [p for p in data if 'kw-test' in (p.get('entity') or '')]
if not rows:
    rows = data[:10]
for p in rows[:10]:
    print(f\"  {p.get('metric_name','?'):22} {p.get('entity','?'):35} score={p.get('score',0):.2f}\")
print(f'  ({len(rows)} kw-test-related / {len(data)} total)')
"

  echo
  echo "==> Recent audit / remediation"
  curl -sf "${AGENT_URL}/api/v1/audit?limit=10" 2>/dev/null | python3 -c "
import sys, json
for a in json.load(sys.stdin)[:8]:
    print(f\"  {a.get('status','?'):12} {a.get('action_type','?'):18} {a.get('entity','?')}\")
" 2>/dev/null || echo "  (no audit entries yet)"
else
  echo
  echo "==> Agent not reachable at ${AGENT_URL}"
  echo "    Run: kwctl up   then re-run this script"
  echo "    Or:  AGENT_URL=http://... $0"
fi

echo
echo "==> Cleanup when done:"
echo "    kubectl delete namespace ${NS}"
