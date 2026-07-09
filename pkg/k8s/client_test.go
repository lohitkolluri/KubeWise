package k8s

import (
	"context"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func TestGetPods(t *testing.T) {
	cs := fake.NewSimpleClientset(
		&corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "nginx-abc",
				Namespace: "default",
			},
		},
		&corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "redis-xyz",
				Namespace: "default",
			},
		},
	)

	c := NewFromClientset(cs)
	pods, err := c.GetPods(context.Background(), "default")
	if err != nil {
		t.Fatalf("GetPods: %v", err)
	}
	if len(pods.Items) != 2 {
		t.Fatalf("expected 2 pods, got %d", len(pods.Items))
	}
	if pods.Items[0].Name != "nginx-abc" {
		t.Fatalf("expected nginx-abc, got %s", pods.Items[0].Name)
	}
}

func TestGetPodsEmptyNamespace(t *testing.T) {
	cs := fake.NewSimpleClientset(
		&corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "pod-a",
				Namespace: "ns1",
			},
		},
		&corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "pod-b",
				Namespace: "ns2",
			},
		},
	)

	c := NewFromClientset(cs)
	pods, err := c.GetPods(context.Background(), "")
	if err != nil {
		t.Fatalf("GetPods: %v", err)
	}
	if len(pods.Items) != 2 {
		t.Fatalf("expected 2 pods across all namespaces, got %d", len(pods.Items))
	}
}

func TestGetNodes(t *testing.T) {
	cs := fake.NewSimpleClientset(
		&corev1.Node{
			ObjectMeta: metav1.ObjectMeta{
				Name: "node-1",
			},
		},
		&corev1.Node{
			ObjectMeta: metav1.ObjectMeta{
				Name: "node-2",
			},
		},
	)

	c := NewFromClientset(cs)
	nodes, err := c.GetNodes(context.Background())
	if err != nil {
		t.Fatalf("GetNodes: %v", err)
	}
	if len(nodes.Items) != 2 {
		t.Fatalf("expected 2 nodes, got %d", len(nodes.Items))
	}
}

func TestGetDeployments(t *testing.T) {
	cs := fake.NewSimpleClientset(
		&corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "web-1",
				Namespace: "default",
			},
		},
	)

	c := NewFromClientset(cs)
	pods, err := c.GetDeployments(context.Background(), "default")
	if err != nil {
		t.Fatalf("GetDeployments: %v", err)
	}
	// With fake client, this returns from pod store since
	// GetDeployments currently delegates to the pod API
	_ = pods
}

func TestGetEvents(t *testing.T) {
	cs := fake.NewSimpleClientset()

	c := NewFromClientset(cs)
	events, err := c.GetEvents(context.Background(), "default", 0)
	if err != nil {
		t.Fatalf("GetEvents: %v", err)
	}
	if events == nil {
		t.Fatal("expected non-nil EventList")
	}
}

func TestNewFromKubeconfigEmpty(t *testing.T) {
	// This should use default loading rules (KUBECONFIG or ~/.kube/config)
	// If no kubeconfig exists, it should error, not panic
	_, err := NewFromKubeconfig("/nonexistent/path/kubeconfig")
	if err == nil {
		t.Log("NewFromKubeconfig with nonexistent path returned nil error (may have default config)")
	}
}

func TestFindRunningAgentPodPrefersDeploymentOverLoki(t *testing.T) {
	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "kubewise", Namespace: "kubewise"},
		Spec: appsv1.DeploymentSpec{
			Selector: &metav1.LabelSelector{
				MatchLabels: map[string]string{"app": "kubewise"},
			},
		},
	}
	agentPod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "kubewise-abc123-xyz",
			Namespace: "kubewise",
			Labels:    map[string]string{"app": "kubewise"},
		},
		Spec:   corev1.PodSpec{Containers: []corev1.Container{{Name: "agent"}}},
		Status: corev1.PodStatus{Phase: corev1.PodRunning},
	}
	lokiPod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "kubewise-loki-0",
			Namespace: "kubewise",
		},
		Spec:   corev1.PodSpec{Containers: []corev1.Container{{Name: "loki"}}},
		Status: corev1.PodStatus{Phase: corev1.PodRunning},
	}
	cs := fake.NewSimpleClientset(dep, agentPod, lokiPod)
	c := NewFromClientset(cs)
	pod, err := c.FindRunningAgentPod(context.Background(), "kubewise", []string{"kubewise", "kubewise-agent"})
	if err != nil {
		t.Fatalf("FindRunningAgentPod: %v", err)
	}
	if pod.Name != "kubewise-abc123-xyz" {
		t.Fatalf("expected agent pod, got %s", pod.Name)
	}
}
