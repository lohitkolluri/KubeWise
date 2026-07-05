package k8s

import (
	"context"
	"fmt"
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
	loadingRules := clientcmd.NewDefaultClientConfigLoadingRules()
	if kubeconfigPath != "" {
		loadingRules.ExplicitPath = kubeconfigPath
	}
	configOverrides := &clientcmd.ConfigOverrides{}
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
	now := time.Now()
	fieldSelector := fmt.Sprintf("lastTimestamp>=%s", now.Add(-since).Format(time.RFC3339))
	events, err := c.clientset.CoreV1().Events(namespace).List(ctx, metav1.ListOptions{
		FieldSelector: fieldSelector,
	})
	if err != nil {
		return nil, fmt.Errorf("list events: %w", err)
	}
	return events, nil
}

// Clientset returns the underlying kubernetes.Interface.
func (c *Client) Clientset() kubernetes.Interface {
	return c.clientset
}
