package remediator

import (
	"context"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func TestInferDeploymentFromPod(t *testing.T) {
	tests := []struct {
		pod  string
		want string
	}{
		{"prometheus-5889c8bf56-whkcx", "prometheus"},
		{"kube-state-metrics-54df5ccb5d-4dznp", "kube-state-metrics"},
		{"nginx", ""},
		{"a-b-c", "a"},
	}
	for _, tc := range tests {
		if got := inferDeploymentFromPod(tc.pod); got != tc.want {
			t.Fatalf("inferDeploymentFromPod(%q) = %q, want %q", tc.pod, got, tc.want)
		}
	}
}

func TestGatherMissingPodStillReportsEventsAndSuccessor(t *testing.T) {
	oldPod := "prometheus-5889c8bf56-whkcx"
	newPod := "prometheus-5889c8bf56-x5gwn"
	ns := "monitoring"

	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "prometheus", Namespace: ns},
		Spec: appsv1.DeploymentSpec{
			Selector: &metav1.LabelSelector{
				MatchLabels: map[string]string{"app": "prometheus"},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": "prometheus"}},
			},
		},
		Status: appsv1.DeploymentStatus{
			Replicas:          1,
			ReadyReplicas:     1,
			AvailableReplicas: 1,
		},
	}
	successor := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      newPod,
			Namespace: ns,
			Labels:    map[string]string{"app": "prometheus"},
		},
		Spec: corev1.PodSpec{
			Containers: []corev1.Container{{Name: "prometheus"}},
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodRunning,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name:  "prometheus",
				Ready: true,
				State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}},
			}},
		},
	}
	event := &corev1.Event{
		ObjectMeta: metav1.ObjectMeta{Name: "evt-1", Namespace: ns},
		InvolvedObject: corev1.ObjectReference{
			Kind:      "Pod",
			Name:      oldPod,
			Namespace: ns,
		},
		Reason:        "BackOff",
		Type:          corev1.EventTypeWarning,
		Message:       "back-off restarting failed container",
		LastTimestamp: metav1.Now(),
	}

	client := fake.NewSimpleClientset(dep, successor, event)
	inv := NewInvestigator(client)

	out := inv.Gather(context.Background(), []models.AnomalyRecord{{
		Entity:    models.FormatEntity(ns, oldPod),
		Namespace: ns,
	}})
	if out == "" {
		t.Fatal("expected investigation output")
	}
	if !strings.Contains(out, "pod not found") {
		t.Fatalf("expected missing-pod message, got:\n%s", out)
	}
	if !strings.Contains(out, "BackOff") {
		t.Fatalf("expected historical events, got:\n%s", out)
	}
	if !strings.Contains(out, "successor workload: deployment/prometheus") {
		t.Fatalf("expected deployment successor, got:\n%s", out)
	}
	if !strings.Contains(out, newPod) {
		t.Fatalf("expected current pod %s, got:\n%s", newPod, out)
	}
}

func TestGatherExistingPodIncludesDescribe(t *testing.T) {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "web-abc", Namespace: "default"},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "web"}}},
		Status: corev1.PodStatus{
			Phase: corev1.PodRunning,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name:  "web",
				Ready: true,
				State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}},
			}},
		},
	}
	inv := NewInvestigator(fake.NewSimpleClientset(pod))
	out := inv.Gather(context.Background(), []models.AnomalyRecord{{
		Entity:    "default/web-abc",
		Namespace: "default",
	}})
	if !strings.Contains(out, "- phase: Running") {
		t.Fatalf("expected pod describe, got:\n%s", out)
	}
}

func TestGatherTimeoutStillBounded(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Millisecond)
	defer cancel()
	inv := NewInvestigator(fake.NewSimpleClientset())
	_ = inv.Gather(ctx, []models.AnomalyRecord{{
		Entity:    "default/missing",
		Namespace: "default",
	}})
}
