package k8s

import (
	"context"
	"fmt"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/wait"
)

// PingCluster verifies the API server is reachable.
func (c *Client) PingCluster(ctx context.Context) error {
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
func (c *Client) DetectPrometheusURL(ctx context.Context, namespace string) (string, bool) {
	if namespace == "" {
		namespace = "monitoring"
	}
	candidates := []string{
		"kube-prometheus-stack-prometheus",
		"prometheus-kube-prometheus-prometheus",
		"prometheus-server",
		"prometheus",
	}
	svcs, err := c.clientset.CoreV1().Services(namespace).List(ctx, metav1.ListOptions{})
	if err != nil {
		return "", false
	}
	names := make(map[string]corev1.Service, len(svcs.Items))
	for _, s := range svcs.Items {
		names[s.Name] = s
	}
	for _, name := range candidates {
		if svc, ok := names[name]; ok {
			port := int32(9090)
			for _, p := range svc.Spec.Ports {
				if p.Port == 9090 || p.Name == "http-web" || p.Name == "web" {
					port = p.Port
					break
				}
			}
			host := fmt.Sprintf("%s.%s.svc.cluster.local", name, namespace)
			return fmt.Sprintf("http://%s:%d", host, port), true
		}
	}
	return "", false
}

// PatchConfigMapPrometheus updates the agent config ConfigMap prometheus_address field.
func (c *Client) PatchConfigMapPrometheus(ctx context.Context, namespace, url string) error {
	cm, err := c.clientset.CoreV1().ConfigMaps(namespace).Get(ctx, "kubewise-agent-config", metav1.GetOptions{})
	if err != nil {
		return fmt.Errorf("get configmap: %w", err)
	}
	raw, ok := cm.Data["config.yaml"]
	if !ok {
		return fmt.Errorf("configmap missing config.yaml")
	}
	lines := strings.Split(raw, "\n")
	out := make([]string, 0, len(lines))
	replaced := false
	for _, line := range lines {
		if strings.HasPrefix(strings.TrimSpace(line), "prometheus_address:") {
			out = append(out, "    prometheus_address: "+url)
			replaced = true
			continue
		}
		out = append(out, line)
	}
	if !replaced {
		return fmt.Errorf("config.yaml missing prometheus_address")
	}
	cm.Data["config.yaml"] = strings.Join(out, "\n")
	_, err = c.clientset.CoreV1().ConfigMaps(namespace).Update(ctx, cm, metav1.UpdateOptions{})
	return err
}
