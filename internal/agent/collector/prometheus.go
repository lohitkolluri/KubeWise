package collector

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/prometheus/client_golang/api"
	v1 "github.com/prometheus/client_golang/api/prometheus/v1"
	"github.com/prometheus/common/model"

	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	nsutil "github.com/lohitkolluri/KubeWise/pkg/namespace"
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
	addr            string
	api             v1.API
	store           *store.Store
	watchNamespaces []string
}

// NewPrometheusCollector creates a new collector connecting to the given Prometheus address.
// watchNamespaces limits metric points with a namespace label; empty means all namespaces.
func NewPrometheusCollector(addr string, s *store.Store, watchNamespaces []string) (*PrometheusCollector, error) {
	client, err := api.NewClient(api.Config{
		Address: addr,
	})
	if err != nil {
		return nil, fmt.Errorf("create prometheus client: %w", err)
	}
	return &PrometheusCollector{
		addr:            addr,
		api:             v1.NewAPI(client),
		store:           s,
		watchNamespaces: watchNamespaces,
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

type queryOutcome struct {
	name   string
	result MetricResult
	err    error
}

// CollectMetrics executes all PromQL queries concurrently and stores the results.
func (c *PrometheusCollector) CollectMetrics(ctx context.Context) ([]MetricResult, error) {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()

	qs := queries()
	outcomes := make(chan queryOutcome, len(qs))
	var wg sync.WaitGroup
	for _, q := range qs {
		wg.Add(1)
		go func(q promQuery) {
			defer wg.Done()
			result, err := c.execQuery(ctx, q.Name, q.Query)
			outcomes <- queryOutcome{name: q.Name, result: result, err: err}
		}(q)
	}
	go func() {
		wg.Wait()
		close(outcomes)
	}()

	var results []MetricResult
	failCount := 0
	for o := range outcomes {
		if o.err != nil {
			failCount++
			slog.Error("prometheus: query failed", "query", o.name, "error", o.err)
			continue
		}
		filtered := c.filterResult(o.result)
		results = append(results, filtered)
		for _, pt := range filtered.Values {
			if err := c.store.AppendMetricSeries(filtered.Name, pt.Labels, pt.Value, pt.Timestamp); err != nil {
				slog.Error("store: append metric failed", "metric", filtered.Name, "error", err)
			}
		}
	}
	if len(results) == 0 {
		return nil, fmt.Errorf("all %d prometheus queries failed", len(qs))
	}
	if failCount > 0 {
		return results, fmt.Errorf("%d/%d prometheus queries failed", failCount, len(qs))
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
	filtered := c.filterResult(result)
	for _, pt := range filtered.Values {
		if err := c.store.AppendMetricSeries(filtered.Name, pt.Labels, pt.Value, pt.Timestamp); err != nil {
			return nil, fmt.Errorf("store append: %w", err)
		}
	}
	return &filtered, nil
}

func (c *PrometheusCollector) filterResult(r MetricResult) MetricResult {
	if len(c.watchNamespaces) == 0 {
		return r
	}
	out := MetricResult{Name: r.Name}
	for _, pt := range r.Values {
		if ns := pt.Labels["namespace"]; ns != "" && !nsutil.InScope(ns, c.watchNamespaces) {
			continue
		}
		out.Values = append(out.Values, pt)
	}
	return out
}

func (c *PrometheusCollector) execQuery(ctx context.Context, name, query string) (MetricResult, error) {
	result, warnings, err := c.api.Query(ctx, query, time.Now())
	if err != nil {
		return MetricResult{}, fmt.Errorf("promql query %q: %w", name, err)
	}
	if result == nil {
		return MetricResult{}, fmt.Errorf("promql query %q: nil result (possible error response with 200 status)", name)
	}
	if len(warnings) > 0 {
		slog.Warn("prometheus: query warning", "query", name, "warnings", warnings)
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
