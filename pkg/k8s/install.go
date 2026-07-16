package k8s

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/wait"
)

// BackendType identifies a monitoring backend provider.
type BackendType string

const (
	// BackendVictoriaMetrics identifies VictoriaMetrics as the monitoring backend.
	BackendVictoriaMetrics BackendType = "victoria-metrics"
	// BackendPrometheus identifies Prometheus as the monitoring backend.
	BackendPrometheus BackendType = "prometheus"
	// BackendVictoriaLogs identifies VictoriaLogs as the log backend.
	BackendVictoriaLogs BackendType = "victoria-logs"
	// BackendLoki identifies Loki as the log backend.
	BackendLoki BackendType = "loki"
	// BackendTempo identifies Tempo as the tracing backend.
	BackendTempo BackendType = "tempo"
)

// DetectedBackend describes a discovered monitoring backend in the cluster.
type DetectedBackend struct {
	Type         BackendType `json:"type"`
	Namespace    string      `json:"namespace"`
	Service      string      `json:"service"`
	URL          string      `json:"url"`          // Query/read endpoint
	PushURL      string      `json:"pushUrl"`      // Write/push endpoint (for Alloy pushes to VL)
	OTLPEndpoint string      `json:"otlpEndpoint"` // OTLP gRPC endpoint (for Tempo traces)
}

// ObservabilityReport captures all discovered monitoring backends.
type ObservabilityReport struct {
	Metrics *DetectedBackend `json:"metrics,omitempty"`
	Logs    *DetectedBackend `json:"logs,omitempty"`
	Traces  *DetectedBackend `json:"traces,omitempty"`
}

// PingCluster verifies the API server is reachable.
func (c *Client) PingCluster(_ context.Context) error {
	_, err := c.clientset.Discovery().ServerVersion()
	if err != nil {
		return fmt.Errorf("kubernetes API unreachable: %w", err)
	}
	return nil
}

// WaitForDeploymentAvailable blocks until a deployment has a ready replica or ctx expires.
func (c *Client) WaitForDeploymentAvailable(ctx context.Context, namespace, name string) error {
	return wait.PollUntilContextCancel(ctx, 3*time.Second, true, func(ctx context.Context) (bool, error) {
		dep, err := c.clientset.AppsV1().Deployments(namespace).Get(ctx, name, metav1.GetOptions{})
		if err != nil {
			return false, err
		}
		for _, cond := range dep.Status.Conditions {
			if cond.Type == appsv1.DeploymentAvailable && cond.Status == corev1.ConditionTrue {
				return dep.Status.ReadyReplicas > 0, nil
			}
		}
		return false, nil
	})
}

// DetectPrometheusURL returns an in-cluster Prometheus HTTP URL when a common install is found.
//
// Deprecated: use DetectMetricsBackend which checks VictoriaMetrics first.
func (c *Client) DetectPrometheusURL(ctx context.Context, namespace string) (string, bool) {
	be := c.DetectMetricsBackend(ctx, []string{namespace})
	if be == nil {
		return "", false
	}
	return be.URL, true
}

// ---------------------------------------------------------------------------
// Observability backend detection — priority order per signal
// ---------------------------------------------------------------------------

// observabilityNamespaces is the default set of namespaces to probe.
var observabilityNamespaces = []string{"monitoring", "observability", "kubewise"}

// DetectAll probes metrics, logs, and traces backends across common namespaces.
func (c *Client) DetectAll(ctx context.Context, preferredNS string) *ObservabilityReport {
	namespaces := c.discoverObservabilityNamespaces(ctx, preferredNS)
	return &ObservabilityReport{
		Metrics: c.DetectMetricsBackend(ctx, namespaces),
		Logs:    c.DetectLogsBackend(ctx, namespaces),
		Traces:  c.DetectTracesBackend(ctx, namespaces),
	}
}

// DetectMetricsBackend checks for VictoriaMetrics first, then Prometheus.
// Returns the first backend found across the probe namespaces.
func (c *Client) DetectMetricsBackend(ctx context.Context, namespaces []string) *DetectedBackend {
	for _, ns := range namespaces {
		svcs, err := c.clientset.CoreV1().Services(ns).List(ctx, metav1.ListOptions{})
		if err != nil {
			continue
		}
		// Priority 1: VictoriaMetrics
		for _, svc := range svcs.Items {
			if isVictoriaMetricsSvc(svc.Name) {
				port := findServicePort(svc, 8428)
				url := fmt.Sprintf("http://%s.%s.svc.cluster.local:%d", svc.Name, ns, port)
				return &DetectedBackend{
					Type: BackendVictoriaMetrics, Namespace: ns, Service: svc.Name, URL: url,
				}
			}
		}
		// Priority 2: Prometheus
		for _, svc := range svcs.Items {
			if isPrometheusSvc(svc.Name) {
				port := findServicePort(svc, 9090)
				url := fmt.Sprintf("http://%s.%s.svc.cluster.local:%d", svc.Name, ns, port)
				return &DetectedBackend{
					Type: BackendPrometheus, Namespace: ns, Service: svc.Name, URL: url,
				}
			}
		}
	}
	return nil
}

// DetectLogsBackend checks for VictoriaLogs first, then Loki.
// Returns the backend with query URL (agent loki_url) and push URL (Alloy endpoint).
func (c *Client) DetectLogsBackend(ctx context.Context, namespaces []string) *DetectedBackend {
	for _, ns := range namespaces {
		svcs, err := c.clientset.CoreV1().Services(ns).List(ctx, metav1.ListOptions{})
		if err != nil {
			continue
		}
		// Priority 1: VictoriaLogs
		for _, svc := range svcs.Items {
			if isVictoriaLogsSvc(svc.Name) {
				port := findServicePort(svc, 9428)
				base := fmt.Sprintf("http://%s.%s.svc.cluster.local:%d", svc.Name, ns, port)
				return &DetectedBackend{
					Type:      BackendVictoriaLogs,
					Namespace: ns, Service: svc.Name,
					URL:     base + "/select/loki",
					PushURL: base,
				}
			}
		}
		// Priority 2: Loki
		for _, svc := range svcs.Items {
			if isLokiSvc(svc.Name) {
				port := findServicePort(svc, 3100)
				url := fmt.Sprintf("http://%s.%s.svc.cluster.local:%d", svc.Name, ns, port)
				return &DetectedBackend{
					Type: BackendLoki, Namespace: ns, Service: svc.Name,
					URL: url, PushURL: url,
				}
			}
		}
	}
	return nil
}

// DetectTracesBackend checks for Tempo (the only traces backend).
func (c *Client) DetectTracesBackend(ctx context.Context, namespaces []string) *DetectedBackend {
	for _, ns := range namespaces {
		svcs, err := c.clientset.CoreV1().Services(ns).List(ctx, metav1.ListOptions{})
		if err != nil {
			continue
		}
		for _, svc := range svcs.Items {
			if isTempoSvc(svc.Name) {
				httpPort := findServicePortByValue(svc, 3200)
				otlpPort := findServicePortByValue(svc, 4318)
				url := fmt.Sprintf("http://%s.%s.svc.cluster.local:%d", svc.Name, ns, httpPort)
				otlp := fmt.Sprintf("http://%s.%s.svc.cluster.local:%d", svc.Name, ns, otlpPort)
				return &DetectedBackend{
					Type: BackendTempo, Namespace: ns, Service: svc.Name,
					URL: url, OTLPEndpoint: otlp,
				}
			}
		}
	}
	return nil
}

// patchConfigMapField is a helper that replaces a YAML key under the first two levels of
// indentation in a ConfigMap "config.yaml" data field. Returns true if a replacement was made.
func patchConfigMapField(raw, key, value string) (string, bool) {
	lines := strings.Split(raw, "\n")
	out := make([]string, 0, len(lines))
	replaced := false
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, key+":") {
			indent := len(line) - len(strings.TrimLeft(line, " \t"))
			out = append(out, strings.Repeat(" ", indent)+key+": "+value)
			replaced = true
			continue
		}
		out = append(out, line)
	}
	return strings.Join(out, "\n"), replaced
}

// PatchConfigMapObservability updates the agent ConfigMap with detected metrics, logs, and
// traces endpoints. Only non-nil report entries are patched.
func (c *Client) PatchConfigMapObservability(ctx context.Context, namespace string, report *ObservabilityReport) error {
	cm, err := c.clientset.CoreV1().ConfigMaps(namespace).Get(ctx, "kubewise-agent-config", metav1.GetOptions{})
	if err != nil {
		return fmt.Errorf("get configmap: %w", err)
	}
	raw, ok := cm.Data["config.yaml"]
	if !ok {
		return errors.New("configmap missing config.yaml")
	}
	patches := map[string]string{}
	if report.Metrics != nil {
		patches["prometheus_address"] = report.Metrics.URL
	}
	if report.Logs != nil {
		patches["loki_url"] = report.Logs.URL
		// Some logs backends have a separate push URL; include it as an inline Loki endpoint.
	}
	if report.Traces != nil {
		patches["tempo_url"] = report.Traces.URL
	}
	if len(patches) == 0 {
		return nil
	}
	for key, val := range patches {
		var replaced bool
		raw, replaced = patchConfigMapField(raw, key, val)
		if !replaced {
			return fmt.Errorf("config.yaml missing %s", key)
		}
	}
	cm.Data["config.yaml"] = raw
	_, err = c.clientset.CoreV1().ConfigMaps(namespace).Update(ctx, cm, metav1.UpdateOptions{})
	return err
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// discoverObservabilityNamespaces builds the probe list: preferred + defaults.
func (c *Client) discoverObservabilityNamespaces(_ context.Context, preferred string) []string {
	seen := map[string]bool{}
	var out []string
	add := func(ns string) {
		if ns != "" && !seen[ns] {
			seen[ns] = true
			out = append(out, ns)
		}
	}
	add(preferred)
	for _, ns := range observabilityNamespaces {
		add(ns)
	}
	return out
}

// Service-name pattern matchers — ordered by specificity.

func isVictoriaMetricsSvc(name string) bool {
	// Helm chart victoria-metrics-single → "victoria-metrics-single"
	// VM operator → "vmsingle-*" or "vm-*"
	if strings.Contains(name, "victoria-metrics") {
		return true
	}
	if strings.HasPrefix(name, "vmsingle-") {
		return true
	}
	return false
}

func isPrometheusSvc(name string) bool {
	// kube-prometheus-stack / prometheus-operator / standalone
	switch name {
	case "prometheus", "prometheus-server", "prometheus-operated":
		return true
	}
	if strings.Contains(name, "kube-prometheus-stack") {
		return true
	}
	if strings.HasPrefix(name, "prometheus-") {
		return true
	}
	// Avoid matching "victoria-logs" service names that don't contain "prometheus"
	return false
}

func isVictoriaLogsSvc(name string) bool {
	if strings.Contains(name, "victoria-logs") {
		return true
	}
	if strings.HasPrefix(name, "vlsingle-") {
		return true
	}
	return false
}

func isLokiSvc(name string) bool {
	switch name {
	case "loki", "loki-gateway", "loki-headless":
		return true
	}
	if strings.HasPrefix(name, "loki-") || strings.HasSuffix(name, "-loki") || strings.HasSuffix(name, "-loki-gateway") {
		return true
	}
	// KubeWise managed Loki (kubewise-loki-*)
	if strings.HasPrefix(name, "kubewise-loki") {
		return true
	}
	return false
}

func isTempoSvc(name string) bool {
	switch name {
	case "tempo", "tempo-query-frontend":
		return true
	}
	if strings.HasPrefix(name, "tempo-") {
		return true
	}
	if strings.HasPrefix(name, "kubewise-tempo") {
		return true
	}
	return false
}

// findServicePort picks the best port for a metrics/logs service.
func findServicePort(svc corev1.Service, fallback int32) int32 {
	preferred := []string{"http", "http-web", "web", "metrics"}
	for _, name := range preferred {
		for _, p := range svc.Spec.Ports {
			if p.Name == name {
				return p.Port
			}
		}
	}
	// Match by value
	return findServicePortByValue(svc, fallback)
}

func findServicePortByValue(svc corev1.Service, fallback int32) int32 {
	for _, p := range svc.Spec.Ports {
		if p.Port == fallback {
			return p.Port
		}
	}
	if len(svc.Spec.Ports) > 0 {
		return svc.Spec.Ports[0].Port
	}
	return fallback
}
