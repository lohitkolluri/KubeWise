package collector

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/prometheus/client_golang/api"
	v1 "github.com/prometheus/client_golang/api/prometheus/v1"
	"github.com/prometheus/common/model"

	"github.com/lohitkolluri/KubeWise/internal/agent/store"
)

// MetricResult represents a single metric query result.
type MetricResult struct {
	Name   string
	Values []MetricPoint
}

// MetricPoint represents a single data point from Prometheus.
type MetricPoint struct {
	Timestamp time.Time
	Value     float64
	Labels    map[string]string
}

// PrometheusCollector collects metrics from a Prometheus server.
type PrometheusCollector struct {
	addr   string
	api    v1.API
	client api.Client
	store  *store.Store
}

// NewPrometheusCollector creates a new collector connecting to the given Prometheus address.
func NewPrometheusCollector(addr string, s *store.Store) (*PrometheusCollector, error) {
	client, err := api.NewClient(api.Config{
		Address: addr,
	})
	if err != nil {
		return nil, fmt.Errorf("create prometheus client: %w", err)
	}
	return &PrometheusCollector{
		addr:   addr,
		api:    v1.NewAPI(client),
		client: client,
		store:  s,
	}, nil
}

// promQuery defines a named PromQL query to execute.
type promQuery struct {
	Name  string
	Query string
}

// queries returns the 17 PromQL queries used for failure prediction.
func queries() []promQuery {
	return []promQuery{
		{Name: "pod_cpu_usage", Query: `rate(container_cpu_usage_seconds_total{container!=""}[5m])`},
		{Name: "pod_memory_usage", Query: `container_memory_working_set_bytes{container!=""}`},
		{Name: "restart_rate", Query: `rate(kube_pod_container_status_restarts_total[5m])`},
		{Name: "oomkilled", Query: `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}`},
		{Name: "crashloop", Query: `kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}`},
		{Name: "imagepull_backoff", Query: `kube_pod_container_status_waiting_reason{reason="ImagePullBackOff"}`},
		{Name: "pod_not_ready", Query: `kube_pod_status_phase{phase=~"Pending|Failed|Unknown"}`},
		{Name: "node_memory_pressure", Query: `kube_node_status_condition{condition="MemoryPressure",status="true"}`},
		{Name: "node_disk_pressure", Query: `kube_node_status_condition{condition="DiskPressure",status="true"}`},
		{Name: "node_load_5m", Query: `node_load5`},
		{Name: "deployment_replicas_unavailable", Query: `kube_deployment_status_replicas_unavailable`},
		{Name: "tcp_retransmit_rate", Query: `rate(node_netstat_Tcp_RetransSegs[5m])`},
		{Name: "dns_failure_rate", Query: `rate(coredns_dns_responses_total{rcode="SERVFAIL"}[5m])`},
		{Name: "node_disk_usage", Query: `(1 - node_filesystem_free_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100`},
		{Name: "cpu_throttled", Query: `rate(container_cpu_cfs_throttled_seconds_total{container!=""}[5m])`},
		{Name: "network_errors", Query: `rate(container_network_receive_errors_total[5m])`},
		{Name: "pod_ready_ratio", Query: `avg by (namespace) (kube_pod_status_ready{condition="true"})`},
	}
}

// CollectMetrics executes all PromQL queries and stores the results.
func (c *PrometheusCollector) CollectMetrics(ctx context.Context) ([]MetricResult, error) {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()

	var results []MetricResult
	for _, q := range queries() {
		result, err := c.execQuery(ctx, q.Name, q.Query)
		if err != nil {
			log.Printf("prometheus: query %q failed: %v", q.Name, err)
			continue
		}
		results = append(results, result)

		for _, pt := range result.Values {
			if err := c.store.AppendMetricSeries(result.Name, pt.Labels, pt.Value, pt.Timestamp); err != nil {
				log.Printf("store: append metric %q failed: %v", result.Name, err)
			}
		}
	}
	return results, nil
}

// CollectQuery executes a single named PromQL query.
func (c *PrometheusCollector) CollectQuery(ctx context.Context, name, query string) (*MetricResult, error) {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()

	result, err := c.execQuery(ctx, name, query)
	if err != nil {
		return nil, err
	}
	for _, pt := range result.Values {
		if err := c.store.AppendMetricSeries(result.Name, pt.Labels, pt.Value, pt.Timestamp); err != nil {
			return nil, fmt.Errorf("store append: %w", err)
		}
	}
	return &result, nil
}

func (c *PrometheusCollector) execQuery(ctx context.Context, name, query string) (MetricResult, error) {
	result, warnings, err := c.api.Query(ctx, query, time.Now())
	if err != nil {
		return MetricResult{}, fmt.Errorf("promql query %q: %w", name, err)
	}
	if len(warnings) > 0 {
		log.Printf("prometheus: warnings for %q: %v", name, warnings)
	}

	switch vec := result.(type) {
	case model.Vector:
		return vectorToResult(name, vec), nil
	case model.Matrix:
		return matrixToResult(name, vec), nil
	default:
		return MetricResult{}, fmt.Errorf("unsupported result type for %q: %T", name, result)
	}
}

func promTimestamp(ts model.Time) time.Time {
	ms := int64(ts)
	return time.Unix(ms/1000, (ms%1000)*int64(time.Millisecond))
}

func vectorToResult(name string, vec model.Vector) MetricResult {
	r := MetricResult{Name: name}
	for _, s := range vec {
		pt := MetricPoint{
			Timestamp: promTimestamp(s.Timestamp),
			Value:     float64(s.Value),
			Labels:    labelMap(s.Metric),
		}
		r.Values = append(r.Values, pt)
	}
	return r
}

func matrixToResult(name string, mat model.Matrix) MetricResult {
	r := MetricResult{Name: name}
	for _, ss := range mat {
		for _, p := range ss.Values {
			pt := MetricPoint{
				Timestamp: promTimestamp(p.Timestamp),
				Value:     float64(p.Value),
				Labels:    labelMap(ss.Metric),
			}
			r.Values = append(r.Values, pt)
		}
	}
	return r
}

func labelMap(m model.Metric) map[string]string {
	labels := make(map[string]string, len(m))
	for k, v := range m {
		labels[string(k)] = string(v)
	}
	return labels
}
