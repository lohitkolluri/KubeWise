package collector

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func TestPodStateConversion(t *testing.T) {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-pod",
			Namespace: "default",
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodRunning,
			Conditions: []corev1.PodCondition{
				{Type: corev1.PodReady, Status: corev1.ConditionTrue},
			},
			ContainerStatuses: []corev1.ContainerStatus{
				{
					Name:         "main",
					RestartCount: 2,
				},
			},
		},
	}

	state := podToState(pod)
	if state.Name != "test-pod" {
		t.Fatalf("expected name test-pod, got %s", state.Name)
	}
	if state.Phase != "Running" {
		t.Fatalf("expected Running phase, got %s", state.Phase)
	}
	if !state.Ready {
		t.Fatal("expected pod to be ready")
	}
	if state.RestartCount != 2 {
		t.Fatalf("expected restart count 2, got %d", state.RestartCount)
	}
}

func TestNodeStateConversion(t *testing.T) {
	node := &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{
			Name: "node-1",
		},
		Status: corev1.NodeStatus{
			Conditions: []corev1.NodeCondition{
				{Type: corev1.NodeReady, Status: corev1.ConditionTrue},
			},
		},
	}

	state := nodeToState(node)
	if state.Name != "node-1" {
		t.Fatalf("expected node-1, got %s", state.Name)
	}
	if !state.Ready {
		t.Fatal("expected node to be ready")
	}
}

func TestGetFailingPods(t *testing.T) {
	// Create real pods and use fake clientset
	cs := fake.NewSimpleClientset(
		&corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: "good-pod", Namespace: "default"},
			Status: corev1.PodStatus{
				Phase: corev1.PodRunning,
				ContainerStatuses: []corev1.ContainerStatus{
					{Name: "main", RestartCount: 0},
				},
			},
		},
		&corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: "crash-pod", Namespace: "default"},
			Status: corev1.PodStatus{
				Phase: corev1.PodFailed,
				ContainerStatuses: []corev1.ContainerStatus{
					{Name: "main", RestartCount: 10},
				},
			},
		},
		&corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: "pending-pod", Namespace: "default"},
			Status: corev1.PodStatus{
				Phase: corev1.PodPending,
			},
		},
	)

	_ = cs // Informers need a real watch which fake doesn't fully support without extra setup
	// Testing state conversion directly instead
	rc := NewResourcesCollector(cs, nil)
	if rc == nil {
		t.Fatal("expected non-nil ResourcesCollector")
	}

	// Manually inject pod states to test filtering
	rc.mu.Lock()
	rc.pods["default/good-pod"] = PodState{Name: "good-pod", Namespace: "default", Phase: "Running", Ready: true}
	rc.pods["default/crash-pod"] = PodState{Name: "crash-pod", Namespace: "default", Phase: "CrashLoopBackOff", Ready: false, RestartCount: 10}
	rc.pods["default/pending-pod"] = PodState{Name: "pending-pod", Namespace: "default", Phase: "Pending", Ready: false}
	rc.mu.Unlock()

	failing := rc.GetFailingPods()
	if len(failing) != 2 {
		t.Fatalf("expected 2 failing pods, got %d", len(failing))
	}

	failingNames := make(map[string]bool)
	for _, p := range failing {
		failingNames[p.Name] = true
	}
	if !failingNames["crash-pod"] {
		t.Error("expected crash-pod to be in failing list")
	}
	if !failingNames["pending-pod"] {
		t.Error("expected pending-pod to be in failing list")
	}
	if failingNames["good-pod"] {
		t.Error("good-pod should not be in failing list")
	}
}

func TestGetUnhealthyNodes(t *testing.T) {
	rc := NewResourcesCollector(fake.NewSimpleClientset(), nil)
	rc.mu.Lock()
	rc.nodes["healthy"] = NodeState{Name: "healthy", Ready: true}
	rc.nodes["sick"] = NodeState{Name: "sick", Ready: false}
	rc.mu.Unlock()

	unhealthy := rc.GetUnhealthyNodes()
	if len(unhealthy) != 1 {
		t.Fatalf("expected 1 unhealthy node, got %d", len(unhealthy))
	}
	if unhealthy[0].Name != "sick" {
		t.Fatalf("expected sick node, got %s", unhealthy[0].Name)
	}
}
