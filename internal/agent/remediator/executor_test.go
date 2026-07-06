package remediator

import (
	"context"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func TestK8sExecutor_DryRun(t *testing.T) {
	exec := NewK8sExecutorWithClient(fake.NewSimpleClientset(), true)
	plan := models.RemediationPlan{
		Action: models.Action{Type: "restart_pod", Namespace: "demo", Target: "api-1"},
	}
	out, err := exec.Execute(context.Background(), plan)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "[dry-run]") {
		t.Fatalf("expected dry-run output, got %q", out)
	}
}

func TestK8sExecutor_RestartPod(t *testing.T) {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "api-1", Namespace: "demo"},
	}
	client := fake.NewSimpleClientset(pod)
	exec := NewK8sExecutorWithClient(client, false)

	plan := models.RemediationPlan{
		Action: models.Action{Type: "restart_pod", Namespace: "demo", Target: "api-1"},
	}
	out, err := exec.Execute(context.Background(), plan)
	if err != nil {
		t.Fatal(err)
	}
	if out == "" {
		t.Fatal("expected result message")
	}
	_, err = client.CoreV1().Pods("demo").Get(context.Background(), "api-1", metav1.GetOptions{})
	if err == nil {
		t.Fatal("expected pod to be deleted")
	}
}

func TestK8sExecutor_ScaleReplicas(t *testing.T) {
	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "api", Namespace: "demo"},
		Spec:       appsv1.DeploymentSpec{Replicas: int32Ptr(1)},
	}
	client := fake.NewSimpleClientset(dep)
	exec := NewK8sExecutorWithClient(client, false)

	plan := models.RemediationPlan{
		Action: models.Action{
			Type:       "scale_replicas",
			Namespace:  "demo",
			Target:     "api",
			Parameters: map[string]string{"replicas": "3"},
		},
	}
	if _, err := exec.Execute(context.Background(), plan); err != nil {
		t.Fatal(err)
	}
	updated, err := client.AppsV1().Deployments("demo").Get(context.Background(), "api", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if updated.Spec.Replicas == nil || *updated.Spec.Replicas != 3 {
		t.Fatalf("expected 3 replicas, got %v", updated.Spec.Replicas)
	}
}

func TestK8sExecutor_RollbackDeployment(t *testing.T) {
	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "api", Namespace: "demo"},
		Spec: appsv1.DeploymentSpec{
			Replicas: int32Ptr(2),
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "api"}},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": "api"}},
				Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "api", Image: "api:v2"}}},
			},
		},
	}
	rsOld := &appsv1.ReplicaSet{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "api-old",
			Namespace: "demo",
			Labels:    map[string]string{"app": "api"},
			Annotations: map[string]string{
				"deployment.kubernetes.io/revision": "1",
			},
		},
		Spec: appsv1.ReplicaSetSpec{
			Selector: dep.Spec.Selector,
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": "api"}},
				Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "api", Image: "api:v1"}}},
			},
		},
	}
	rsNew := rsOld.DeepCopy()
	rsNew.Name = "api-new"
	rsNew.Annotations["deployment.kubernetes.io/revision"] = "2"

	objs := []runtime.Object{dep, rsOld, rsNew}
	client := fake.NewSimpleClientset(objs...)
	exec := NewK8sExecutorWithClient(client, false)

	plan := models.RemediationPlan{
		Action: models.Action{Type: "rollback_deployment", Namespace: "demo", Target: "api"},
	}
	if _, err := exec.Execute(context.Background(), plan); err != nil {
		t.Fatal(err)
	}
	updated, err := client.AppsV1().Deployments("demo").Get(context.Background(), "api", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if updated.Spec.Template.Spec.Containers[0].Image != "api:v1" {
		t.Fatalf("expected rollback to v1, got %s", updated.Spec.Template.Spec.Containers[0].Image)
	}
}

func int32Ptr(v int32) *int32 { return &v }
