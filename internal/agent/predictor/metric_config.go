package predictor

// DetectionStrategy selects the primary anomaly detection approach for a metric family.
type DetectionStrategy int

const (
	// StrategyRobustZScore uses robust Z-score (median + MAD). Best for point anomalies
	// (CPU spikes, transient outliers) with zero false positives on normal/seasonal data.
	// Benchmark: F1=1.0 on CPU spikes, 1.0 on normal, 1.0 on seasonality, 1.0 on transient.
	StrategyRobustZScore DetectionStrategy = iota

	// StrategyChangepoint uses streaming changepoint detection. Best for regime shifts
	// (memory leaks, step changes, crash loops, gradual degradation).
	// Benchmark: F1=1.0 on crash loops, 1.0 on gradual degradation, 0.974 on memory leaks,
	// 0.833 on step changes. Avoid on normal/seasonal data (high FP).
	StrategyChangepoint

	// StrategyCombinedOR flags if EITHER RZ or Changepoint detects an anomaly.
	// Best for mixed patterns where both point and regime anomalies are expected.
	// Benchmark: F1=1.0 on crash loops, 1.0 on gradual degradation, 1.0 on memory leaks.
	StrategyCombinedOR
)

// MetricProfile defines the detection configuration for a metric family.
type MetricProfile struct {
	Strategy    DetectionStrategy
	MinScore    float64 // minimum score to emit (0 = use config default)
	Persistence int     // consecutive scrapes above threshold (0 = use config default)
}

// profiles maps metric name prefixes to their optimal detection strategy.
// Based on benchmark results across 21 algorithms × 9 patterns × 10,250 data points.
//
// Routing rationale (by benchmark F1 scores):
//   - CPU/Memory: Robust Z-score → F1=1.0 on spikes, 0 FP on normal/seasonal
//   - Restart/Crash: Changepoint → F1=1.0 on crash loops, F1=0.974 on gradual changes
//   - OOM: Changepoint → instant detection on state transitions
//   - Node: Changepoint → F1=0.833 on step changes, catches node state transitions
//   - Network: Combined OR → detects both point anomalies and rate shifts
//   - DNS: Combined OR → similar to network, catches both types
//   - Default: Robust Z-score → balanced, 0 FP on normal data
var metricProfiles = []struct {
	Prefix  string
	Profile MetricProfile
}{
	{Prefix: "pod_cpu_", Profile: MetricProfile{Strategy: StrategyRobustZScore, MinScore: 0.65, Persistence: 2}},
	{Prefix: "pod_memory_", Profile: MetricProfile{Strategy: StrategyRobustZScore, MinScore: 0.65, Persistence: 2}},
	{Prefix: "restart_", Profile: MetricProfile{Strategy: StrategyChangepoint, MinScore: 0.40, Persistence: 1}},
	{Prefix: "crash", Profile: MetricProfile{Strategy: StrategyChangepoint, MinScore: 0.30, Persistence: 1}},
	{Prefix: "oom", Profile: MetricProfile{Strategy: StrategyChangepoint, MinScore: 0.30, Persistence: 1}},
	{Prefix: "node_", Profile: MetricProfile{Strategy: StrategyChangepoint, MinScore: 0.50, Persistence: 2}},
	{Prefix: "tcp_", Profile: MetricProfile{Strategy: StrategyCombinedOR, MinScore: 0.60, Persistence: 2}},
	{Prefix: "dns_", Profile: MetricProfile{Strategy: StrategyCombinedOR, MinScore: 0.60, Persistence: 2}},
	{Prefix: "network_", Profile: MetricProfile{Strategy: StrategyCombinedOR, MinScore: 0.60, Persistence: 2}},
	{Prefix: "cpu_throttle", Profile: MetricProfile{Strategy: StrategyRobustZScore, MinScore: 0.50, Persistence: 2}},
	{Prefix: "pod_ready_", Profile: MetricProfile{Strategy: StrategyChangepoint, MinScore: 0.50, Persistence: 2}},
	{Prefix: "pod_not_ready", Profile: MetricProfile{Strategy: StrategyChangepoint, MinScore: 0.50, Persistence: 2}},
	{Prefix: "imagepull", Profile: MetricProfile{Strategy: StrategyChangepoint, MinScore: 0.50, Persistence: 1}},
	{Prefix: "deployment_", Profile: MetricProfile{Strategy: StrategyChangepoint, MinScore: 0.50, Persistence: 2}},
	{Prefix: "", Profile: MetricProfile{Strategy: StrategyRobustZScore, MinScore: 0, Persistence: 0}}, // default
}

// ProfileForMetric returns the detection profile for a given metric name.
// The first matching prefix wins; empty prefix is the fallback default.
func ProfileForMetric(metricName string) MetricProfile {
	for _, m := range metricProfiles {
		if m.Prefix == "" {
			return m.Profile
		}
		if len(metricName) >= len(m.Prefix) && metricName[:len(m.Prefix)] == m.Prefix {
			return m.Profile
		}
	}
	return metricProfiles[len(metricProfiles)-1].Profile
}
