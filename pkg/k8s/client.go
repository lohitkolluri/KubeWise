package k8s

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

// Client wraps a Kubernetes clientset for cluster operations.
type Client struct {
	clientset kubernetes.Interface
}

// NewInCluster creates a Client using in-cluster config (for running inside a pod).
func NewInCluster() (*Client, error) {
	config, err := rest.InClusterConfig()
	if err != nil {
		return nil, fmt.Errorf("in-cluster config: %w", err)
	}
	cs, err := kubernetes.NewForConfig(config)
	if err != nil {
		return nil, fmt.Errorf("create clientset: %w", err)
	}
	return &Client{clientset: cs}, nil
}

// NewFromKubeconfig creates a Client from a kubeconfig file path.
// An empty path uses the default loading rules (KUBECONFIG env, ~/.kube/config).
func NewFromKubeconfig(kubeconfigPath string) (*Client, error) {
	return NewFromKubeconfigContext(kubeconfigPath, "")
}

// NewFromKubeconfigContext creates a Client with an optional kubeconfig context override.
func NewFromKubeconfigContext(kubeconfigPath, context string) (*Client, error) {
	loadingRules := clientcmd.NewDefaultClientConfigLoadingRules()
	if kubeconfigPath != "" {
		loadingRules.ExplicitPath = kubeconfigPath
	}
	configOverrides := &clientcmd.ConfigOverrides{}
	if context != "" {
		configOverrides.CurrentContext = context
	}
	kubeConfig := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(loadingRules, configOverrides)
	config, err := kubeConfig.ClientConfig()
	if err != nil {
		return nil, fmt.Errorf("kubeconfig: %w", err)
	}
	cs, err := kubernetes.NewForConfig(config)
	if err != nil {
		return nil, fmt.Errorf("create clientset: %w", err)
	}
	return &Client{clientset: cs}, nil
}

// NewFromClientset creates a Client from an existing kubernetes.Interface (useful for testing).
func NewFromClientset(cs kubernetes.Interface) *Client {
	return &Client{clientset: cs}
}

// GetPods returns all pods in the given namespace (use "" for all namespaces).
func (c *Client) GetPods(ctx context.Context, namespace string) (*corev1.PodList, error) {
	pods, err := c.clientset.CoreV1().Pods(namespace).List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, fmt.Errorf("list pods: %w", err)
	}
	return pods, nil
}

// GetNodes returns all cluster nodes.
func (c *Client) GetNodes(ctx context.Context) (*corev1.NodeList, error) {
	nodes, err := c.clientset.CoreV1().Nodes().List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, fmt.Errorf("list nodes: %w", err)
	}
	return nodes, nil
}

// GetDeployments returns all deployments in the given namespace (use "" for all namespaces).
func (c *Client) GetDeployments(ctx context.Context, namespace string) (*appsv1.DeploymentList, error) {
	deployments, err := c.clientset.AppsV1().Deployments(namespace).List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, fmt.Errorf("list deployments: %w", err)
	}
	return deployments, nil
}

// GetEvents returns events in the given namespace since the specified duration ago.
func (c *Client) GetEvents(ctx context.Context, namespace string, since time.Duration) (*corev1.EventList, error) {
	events, err := c.clientset.CoreV1().Events(namespace).List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, fmt.Errorf("list events: %w", err)
	}
	cutoff := time.Now().UTC().Add(-since)
	filtered := &corev1.EventList{}
	for _, e := range events.Items {
		if since > 0 && e.LastTimestamp.Time.Before(cutoff) {
			continue
		}
		filtered.Items = append(filtered.Items, e)
	}
	return filtered, nil
}

// Clientset returns the underlying kubernetes.Interface.
func (c *Client) Clientset() kubernetes.Interface {
	return c.clientset
}

// FindRunningPod returns the first Running pod in namespace whose name has the given prefix.
func (c *Client) FindRunningPod(ctx context.Context, namespace, namePrefix string) (*corev1.Pod, error) {
	pods, err := c.GetPods(ctx, namespace)
	if err != nil {
		return nil, err
	}
	for i := range pods.Items {
		p := &pods.Items[i]
		if strings.HasPrefix(p.Name, namePrefix) && p.Status.Phase == corev1.PodRunning {
			return p, nil
		}
	}
	return nil, fmt.Errorf("no running pod with prefix %q in namespace %s", namePrefix, namespace)
}

// GetPodLogs returns the tail of logs from a pod container.
func (c *Client) GetPodLogs(ctx context.Context, namespace, podName, container string, tailLines int64) (string, error) {
	opts := &corev1.PodLogOptions{TailLines: &tailLines}
	if container != "" {
		opts.Container = container
	}
	req := c.clientset.CoreV1().Pods(namespace).GetLogs(podName, opts)
	stream, err := req.Stream(ctx)
	if err != nil {
		return "", fmt.Errorf("stream logs: %w", err)
	}
	defer func() { _ = stream.Close() }()
	data, err := io.ReadAll(stream)
	if err != nil {
		return "", fmt.Errorf("read logs: %w", err)
	}
	return string(data), nil
}

// StreamPodLogs follows pod logs and invokes fn for each line until ctx is cancelled.
func (c *Client) StreamPodLogs(ctx context.Context, namespace, podName, container string, tailLines int64, fn func(string)) error {
	opts := &corev1.PodLogOptions{TailLines: &tailLines, Follow: true}
	if container != "" {
		opts.Container = container
	}
	req := c.clientset.CoreV1().Pods(namespace).GetLogs(podName, opts)
	stream, err := req.Stream(ctx)
	if err != nil {
		return fmt.Errorf("stream logs: %w", err)
	}
	defer func() { _ = stream.Close() }()
	scanner := bufio.NewScanner(stream)
	for scanner.Scan() {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		fn(scanner.Text())
	}
	return scanner.Err()
}

// RolloutRestart triggers a rolling restart of a deployment.
func (c *Client) RolloutRestart(ctx context.Context, namespace, name string) error {
	dep, err := c.clientset.AppsV1().Deployments(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return fmt.Errorf("get deployment: %w", err)
	}
	if dep.Spec.Template.Annotations == nil {
		dep.Spec.Template.Annotations = map[string]string{}
	}
	dep.Spec.Template.Annotations["kubectl.kubernetes.io/restartedAt"] = time.Now().UTC().Format(time.RFC3339)
	_, err = c.clientset.AppsV1().Deployments(namespace).Update(ctx, dep, metav1.UpdateOptions{})
	if err != nil {
		return fmt.Errorf("restart deployment: %w", err)
	}
	return nil
}

// GetDeployment returns a deployment by name.
func (c *Client) GetDeployment(ctx context.Context, namespace, name string) (*appsv1.Deployment, error) {
	return c.clientset.AppsV1().Deployments(namespace).Get(ctx, name, metav1.GetOptions{})
}
