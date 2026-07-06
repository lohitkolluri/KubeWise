# False-Positive Gate & Predictive Pattern Matchers

## Problem

The KubeWise agent pipeline sends every statistical anomaly with score ≥ 0.3 and every pattern match with confidence ≥ 0.5 straight to the anomaly store, which the Correlator then passes to the LLM for remediation analysis. With an estimated ~29% false positive rate on Gaussian noise (at threshold 0.3), this wastes LLM tokens and degrades trust in the system.

Additionally, the three domain pattern matchers — OOMRisk, CrashLoopRisk, Degradation — are **reactive**: they require the failure event to have already occurred before firing. The `TimeToFailure` field in `PatternMatch` is never populated.

## Goals

1. **Zero wasted LLM calls**: No anomaly reaches the Correlator unless it passes a multi-layer gate that filters noise.
2. **Zero missed real positives**: Acute spikes, sustained issues, and correlated multi-metric anomalies all pass through without delay.
3. **Predictive patterns**: OOMRisk, CrashLoopRisk, and Degradation detect failures *before* they happen and populate `TimeToFailure`.

---

## Architecture

### Component: AnomalyGate

A new filter component sitting between `predictor.Run()`/`predictor.RunPatterns()` and `store.SaveAnomaly()`. The gate inspects each result and decides whether to persist it (and thus reach the LLM).

```
predictor.Run() ──┐
                  ├──> AnomalyGate.Filter() ──> store.SaveAnomaly() ──> correlator.RunOnce()
predictor.RunPatterns() ──┘
```

The gate is **not** in the Correlator path. It filters at the anomaly-ingestion layer so noise never reaches storage, keeps the anomaly store clean, and avoids wasted DB writes.

### Location

- **New package**: `KubeWise/internal/agent/gate/`
- **Files**: `anomaly_gate.go`, `anomaly_gate_test.go`
- **Integration point**: Called in `agent.go:runOnce()` before each `SaveAnomaly` call (both statistical and pattern paths).

---

## Gate Rules

| # | Rule | Condition | Action |
|---|------|-----------|--------|
| 1 | **High-score bypass** | `Score ≥ 0.8` | **PASS** — acute spike, immediate |
| 2 | **Pattern bypass** | `Type == "pattern"` | **PASS** — domain confidence already vetted |
| 3 | **Multi-metric correlation** | ≥2 distinct metric streams for same entity score ≥ 0.3 within a cross-scrape window | **PASS** — correlated metrics are a strong signal |
| 4 | **Sustainment** | Single metric stream at score ≥ 0.3 for 3 consecutive scrapes | **PASS** — sustained elevation is real |
| 5 | **Cooldown** | Same entity already passed within 5-minute window | **DROP** — deduplicate, avoid repeat LLM calls |

### Rule Precedence

Rules 1–3 are **bypass** rules — any match passes immediately regardless of rules 4–5. Rule 5 (cooldown) is checked after a PASS decision; a PASS can be downgraded to DROP if cooldown is active and the result is not an escalation (score higher than the previous pass for the same entity).

#### Escalation override for cooldown

A repeat anomaly for an entity in cooldown **still passes** if its score is higher than the score that was last recorded for that entity. This ensures that escalating severity is never suppressed.

### Sustainment Details

The sustainment check requires 3 consecutive scrapes with score ≥ 0.3. The window is defined as `2 * scrape_interval` (configurable via `Config.ScrapeInterval`). If a scrape is skipped or the entity has no data for a cycle, the counter resets.

The history is stored as:
```go
type entityHistory struct {
    scores     []float64  // last N scores, newest last
    lastSeen   time.Time  // timestamp of last recorded scrape
}
```

### Multi-Metric Correlation Details

Correlation is cross-scrape: the gate maintains a sliding window of anomalies by entity. If within a `CorrelationWindow` (default: 2 × scrape interval) two different metric streams for the same entity have scored ≥ 0.3, the single-metric sustainment requirement is bypassed.

Entity identity is determined by the `Entity` field in `PredictionResult`/`AnomalyRecord`. Two results for `"web-1"` with metric names `"pod_memory_usage"` and `"restart_rate"` count as correlated.

Metric streams are deduplicated — the same metric name scoring multiple times in the window counts as one stream.

---

## Predictive Pattern Changes

### OOMRisk (`patterns_oom.go`)

**Current**: Fires only after an `OOMKilled` event exists.

**Change**:
1. **Remove** the `hasOOMEvent` guard entirely.
2. Detect what memory limit to compare against:
   - First, check `resources.ResourceSnapshot` for a memory limit field (to be added — see below).
   - If no resource snapshot, use a default relative threshold: if usage trend is positive AND the rate of growth would exceed a reasonable bound within the horizon, fire.
3. Compute `TimeToFailure`:
   ```go
   if memoryLimit > 0 && growthRate > 0 {
       timeToFailure = time.Duration((memoryLimit - currentUsage) / growthRate) * scrapeInterval
   }
   ```
4. Update confidence:
   ```go
   usageRatio := currentUsage / memoryLimit
   confidence = 0.3 + usageRatio*0.3 + clamp(trend*2.0, 0, 0.35)
   // +0.2 if there was a prior OOM event (past history reinforces the prediction)
   ```

**ResourceSnapshot** needs a new field:
```go
type PodResource struct {
    Name       string
    Namespace  string
    CPULimit   float64
    MemLimit   float64
}
```

Populated from the collector or a new K8s API source.

### CrashLoopRisk (`patterns_crashloop.go`)

**Current**: Requires `CrashLoopBackOff` event or high `latestRate` + trend to fire.

**Change**:
1. **Remove** the early-return guard at line 44 (`if latestRate < 0.01 && trend < 0.1 && !hasCrashLoopEvent`).
2. Add a **prediction horizon** check: use `RateOfChange.Velocity()` (or the existing `estimateTrend()`) to detect accelerating restarts.
3. Compute `TimeToFailure`:
   ```go
   criticalRate := 1.0  // 1 restart per interval = active crash loop
   if slope > 0 {
       timeToFailure = time.Duration((criticalRate - latestRate) / slope) * scrapeInterval
   }
   ```
4. Update confidence:
   ```go
   if timeToFailure > 0 && timeToFailure < 10*scrapeInterval {
       confidence = 0.5 + 0.3*(1 - float64(timeToFailure)/(10*float64(scrapeInterval)))
   } else if latestRate >= criticalRate {
       confidence = 0.9  // already crashing
   }
   // +0.1 if in FailingPods, +0.1 if CrashLoopBackOff event existed
   ```

### Degradation (`patterns_degradation.go`)

**Current**: Fires on current node pressure values, no trending.

**Change**:
1. Track node pressure metrics (`node_disk_pressure`, `node_memory_pressure`) over time using a simple history buffer (similar to `predictor.history`).
2. If a node pressure metric is > 0 AND has been increasing over the last 3+ scrapes, compute a `TimeToFailure` based on when it would cross a critical threshold.
3. For `pod_not_ready`: add a sustainment check — fire only if unready for 2+ consecutive scrapes.
4. Confidence scaling:
   ```go
   confidence = 0.5 + sustainedDuration*0.1 + trendBoost*0.2
   ```

---

## Testing Plan

### AnomalyGate Tests

| Test | Input | Expected |
|------|-------|----------|
| `TestGate_SustainedScore` | Score 0.3 for 3 consecutive scrapes | Pass on 3rd scrape |
| `TestGate_SingleSpikeDropped` | Score 0.3 once, then < 0.3 | Drop |
| `TestGate_HighScoreBypass` | Score 0.9 on first scrape | Immediate pass |
| `TestGate_PatternBypass` | Type="pattern", Score=0.6 | Immediate pass |
| `TestGate_Cooldown` | Pass, then same entity within 5 min | Drop |
| `TestGate_CooldownEscalation` | Pass at 0.5, then same entity at 0.7 within cooldown | Pass (escalated) |
| `TestGate_MultiMetricCorrelation` | Metric-A at 0.4 in scrape 1, Metric-B at 0.5 in scrape 2, same entity | Pass (correlated) |
| `TestGate_MultiMetricDifferentEntities` | Metric-A entity-1 at 0.5, Metric-B entity-2 at 0.5 | No correlation, need sustainment |

### Predictive Pattern Tests

**OOMRisk** (new tests — existing tests adapted for no-event requirement):

| Test | Input | Expected |
|------|-------|----------|
| `TestOOMRisk_PredictsOOM` | Memory climbing 60%→80%→95% of limit, no prior OOM event | Match, confidence ≥ 0.7, TimeToFailure > 0 |
| `TestOOMRisk_UnderLimit` | Memory flat at 30% of limit, no trend | No match |
| `TestOOMRisk_VolatileMemory` | Memory spiking then returning to baseline | No match |
| `TestOOMRisk_WithPriorOOM` | Prior OOM event + rising memory | Match with boosted confidence |

**CrashLoopRisk** (new tests — existing tests adapted):

| Test | Input | Expected |
|------|-------|----------|
| `TestCrashLoop_PredictsCrash` | Restart rate 0.05→0.15→0.4, no CrashLoopBackOff event | Match, confidence ≥ 0.6, TimeToFailure > 0 |
| `TestCrashLoop_StableRestarts` | Restart rate flat at 0.01 | No match |
| `TestCrashLoop_AlreadyCrashing` | Restart rate ≥ 1.0 | Match, TimeToFailure = 0 |

**Degradation** (new tests):

| Test | Input | Expected |
|------|-------|----------|
| `TestDegradation_PredictsPressure` | Disk pressure 0→0.5→0.8 over 3 scrapes, no current pressure | Match |
| `TestDegradation_TransientNotReady` | Pod not_ready single scrape then recovers | No match (sustainment) |
| `TestDegradation_SustainedNotReady` | Pod not_ready for 3 consecutive scrapes | Match |

---

## Implementation Plan

### Phase 1: AnomalyGate (gate + tests)

1. Create `internal/agent/gate/anomaly_gate.go` with `AnomalyGate` struct and `Filter()` method.
2. Implement sustainment, high-score bypass, pattern bypass, cooldown, multi-metric correlation.
3. Write comprehensive tests.
4. Integrate into `agent.go:runOnce()` — call `gate.Filter()` before each `SaveAnomaly`.

### Phase 2: Predictive OOMRisk

1. Add `PodResources` to `ResourceSnapshot`.
2. Modify `OOMPattern.Match()` — remove `hasOOMEvent` gate, add memory limit comparison, compute `TimeToFailure`.
3. Adapt existing tests (expect matches without events now).
4. Add new predictive-specific tests.

### Phase 3: Predictive CrashLoopRisk

1. Modify `CrashLoopPattern.Match()` — remove early-return guard, compute acceleration-based `TimeToFailure`.
2. Adapt existing tests.
3. Add predictive-specific tests.

### Phase 4: Predictive Degradation

1. Add history tracking for node pressure metrics.
2. Add sustainment check for `pod_not_ready`.
3. Add `TimeToFailure` computation for node pressure.
4. Add predictive-specific tests.

### Phase 5: Integration verification

1. Run full test suite.
2. Verify gate + predictive patterns interact correctly (predictive patterns bypass gate via pattern bypass rule).
3. Verify cooldown prevents duplicate Correlator calls.

---

## AnomalyGate API

```go
package gate

type Config struct {
    Threshold          float64       // Minimum score to consider (default: 0.3)
    SustainCount       int           // Consecutive scrapes needed (default: 3)
    CooldownDuration   time.Duration // Per-entity cooldown (default: 5 min)
    ScrapeInterval     time.Duration // For TimeToFailure and window calculations
    CorrelationWindow  time.Duration // Multi-metric correlation window (default: 2 × ScrapeInterval)
    HighScoreBypass    float64       // Score above this bypasses sustainment (default: 0.8)
    BypassPatterns     bool          // Pattern-type results bypass gate (default: true)
}

type AnomalyGate struct {
    config    Config
    history   map[string]*entityHistory   // entity key → score history
    cooldowns map[string]time.Time        // entity key → cooldown expiry
    metrics   map[string]map[string]time.Time // entity → metricName → lastSeen (for correlation)
    mu        sync.Mutex
}

type Result struct {
    Pass   bool
    Reason string  // which rule triggered the decision
}

func (g *AnomalyGate) Filter(entity string, metricName string, score float64, resultType string, now time.Time) Result
```
