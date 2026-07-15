package collector

// KubeWise Recording Rules
//
// KubeWise defines Prometheus recording rules that pre-compute frequently-used
// aggregations to reduce query load on the Prometheus server during each scrape
// cycle. These rules are applied at the Prometheus level via a PrometheusRule
// CRD (monitoring.coreos.com/v1) and are referenced by the KubeWise agent's
// built-in queries.
//
// Rule naming convention:
//
//	kw:<metric>:<aggregation>
//
// Rules defined:
//
//	kw:cpu_usage:rate5m            — 5m CPU usage rate (container scoped)
//	kw:memory_usage:working_set    — Working set memory (container scoped)
//	kw:restarts:rate5m             — 5m restart rate (pod scoped)
//	kw:pod_phase:failed            — Pods in Pending/Failed/Unknown phase
//	kw:node:memory_pressure        — Nodes with MemoryPressure condition
//	kw:node:disk_pressure          — Nodes with DiskPressure condition
//	kw:deployment:replicas_unavailable — Unavailable deployment replicas
//
// The agent also ingests metrics from its own queries() function in prometheus.go
// for anomaly detection. The recording rules are complementary — they provide
// pre-aggregated series for the PrometheusRule-based ingestion path.

// RecordingRulesQuery returns the set of recording rule PromQL expressions
// used by KubeWise, keyed by their rule name.
func RecordingRulesQuery(name string) (string, bool) {
	rules := map[string]string{
		"kw:cpu_usage:rate5m":            `rate(container_cpu_usage_seconds_total{container!=""}[5m])`,
		"kw:memory_usage:working_set":    `container_memory_working_set_bytes{container!=""}`,
		"kw:restarts:rate5m":             `rate(kube_pod_container_status_restarts_total[5m])`,
		"kw:pod_phase:failed":            `kube_pod_status_phase{phase=~"Pending|Failed|Unknown"}`,
		"kw:node:memory_pressure":        `kube_node_status_condition{condition="MemoryPressure",status="true"}`,
		"kw:node:disk_pressure":          `kube_node_status_condition{condition="DiskPressure",status="true"}`,
		"kw:deployment:replicas_unavailable": `kube_deployment_status_replicas_unavailable`,
	}
	expr, ok := rules[name]
	return expr, ok
}

// RecordingRuleNames returns all recording rule names in order.
func RecordingRuleNames() []string {
	return []string{
		"kw:cpu_usage:rate5m",
		"kw:memory_usage:working_set",
		"kw:restarts:rate5m",
		"kw:pod_phase:failed",
		"kw:node:memory_pressure",
		"kw:node:disk_pressure",
		"kw:deployment:replicas_unavailable",
	}
}
