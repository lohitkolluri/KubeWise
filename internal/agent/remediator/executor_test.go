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

	"github.com/lohitkolluri/KubeWise/internal/tools"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// stubToolPlugin is a minimal ToolPlugin implementation that records the last
// action it received and returns a canned result.
type stubToolPlugin struct {
	name        string
	lastAction  models.ToolAction
	result      *models.ToolResult
	validateErr error
}

func (p *stubToolPlugin) Name() string                          { return p.name }
func (p *stubToolPlugin) Capabilities() []models.ToolCapability { return nil }
func (p *stubToolPlugin) Validate(_ models.ToolAction) error    { return p.validateErr }
func (p *stubToolPlugin) Execute(_ context.Context, a models.ToolAction) (*models.ToolResult, error) {
	p.lastAction = a
	if p.result != nil {
		return p.result, nil
	}
	return &models.ToolResult{Success: true, Stdout: "ok"}, nil
}

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

func TestK8sExecutor_ToolRouteDispatch(t *testing.T) {
	client := fake.NewSimpleClientset()
	exec := NewK8sExecutorWithClient(client, false)

	plugin := &stubToolPlugin{name: "helm"}
	reg := tools.NewRegistry()
	if err := reg.Register(plugin); err != nil {
		t.Fatal(err)
	}
	exec.SetToolRegistry(reg)

	plan := models.RemediationPlan{
		Action: models.Action{
			Type:       "helm_upgrade",
			Namespace:  "demo",
			Target:     "my-release",
			Parameters: map[string]string{"chart": "./charts/app"},
		},
	}
	out, err := exec.Execute(context.Background(), plan)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "ok") {
		t.Fatalf("expected result containing 'ok', got %q", out)
	}
	if plugin.lastAction.Tool != "helm" {
		t.Fatalf("expected tool 'helm', got %q", plugin.lastAction.Tool)
	}
	if plugin.lastAction.Command != "upgrade" {
		t.Fatalf("expected command 'upgrade', got %q", plugin.lastAction.Command)
	}
	if plugin.lastAction.Args["release"] != "my-release" {
		t.Fatalf("expected release 'my-release', got %q", plugin.lastAction.Args["release"])
	}
	if plugin.lastAction.Args["chart"] != "./charts/app" {
		t.Fatalf("expected chart './charts/app', got %q", plugin.lastAction.Args["chart"])
	}
	if plugin.lastAction.Args["namespace"] != "demo" {
		t.Fatalf("expected namespace 'demo', got %q", plugin.lastAction.Args["namespace"])
	}
}

func TestK8sExecutor_ToolRouteNoRegistry(t *testing.T) {
	client := fake.NewSimpleClientset()
	exec := NewK8sExecutorWithClient(client, false)

	plan := models.RemediationPlan{
		Action: models.Action{Type: "helm_upgrade", Namespace: "demo", Target: "r1"},
	}
	_, err := exec.Execute(context.Background(), plan)
	if err == nil {
		t.Fatal("expected error when tool registry is nil")
	}
	if !strings.Contains(err.Error(), "tool plugin registry not configured") {
		t.Fatalf("expected 'not configured' error, got: %v", err)
	}
}

func TestK8sExecutor_ToolRouteUnregistered(t *testing.T) {
	client := fake.NewSimpleClientset()
	exec := NewK8sExecutorWithClient(client, false)
	// Registry exists but helm is not registered.
	exec.SetToolRegistry(tools.NewRegistry())

	plan := models.RemediationPlan{
		Action: models.Action{Type: "helm_upgrade", Namespace: "demo", Target: "r1"},
	}
	_, err := exec.Execute(context.Background(), plan)
	if err == nil {
		t.Fatal("expected error for unregistered plugin")
	}
	if !strings.Contains(err.Error(), "not registered") {
		t.Fatalf("expected 'not registered' error, got: %v", err)
	}
}

func TestK8sExecutor_ToolRouteDryRun(t *testing.T) {
	client := fake.NewSimpleClientset()
	exec := NewK8sExecutorWithClient(client, true)

	plan := models.RemediationPlan{
		Action: models.Action{Type: "helm_upgrade", Namespace: "demo", Target: "r1"},
	}
	out, err := exec.Execute(context.Background(), plan)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "[dry-run]") {
		t.Fatalf("expected dry-run output for tool action, got %q", out)
	}
}

func TestK8sExecutor_buildToolAction_Helm(t *testing.T) {
	exec := NewK8sExecutorWithClient(fake.NewSimpleClientset(), false)
	plan := models.RemediationPlan{
		Action: models.Action{
			Type:       "helm_upgrade",
			Namespace:  "demo",
			Target:     "my-release",
			Parameters: map[string]string{"chart": "./charts/app", "values": "prod.yaml"},
		},
	}
	route := toolRoute{tool: "helm", command: "upgrade"}
	action := exec.buildToolAction(plan, route)

	if action.Tool != "helm" {
		t.Fatalf("expected tool 'helm', got %q", action.Tool)
	}
	if action.Command != "upgrade" {
		t.Fatalf("expected 'upgrade', got %q", action.Command)
	}
	if action.Args["release"] != "my-release" {
		t.Fatalf("expected release 'my-release', got %q", action.Args["release"])
	}
	if action.Args["chart"] != "./charts/app" {
		t.Fatalf("expected chart './charts/app', got %q", action.Args["chart"])
	}
	if action.Args["values"] != "prod.yaml" {
		t.Fatalf("expected values 'prod.yaml', got %q", action.Args["values"])
	}
	if action.Args["namespace"] != "demo" {
		t.Fatalf("expected namespace 'demo', got %q", action.Args["namespace"])
	}
}

func TestK8sExecutor_buildToolAction_ArgoCD(t *testing.T) {
	exec := NewK8sExecutorWithClient(fake.NewSimpleClientset(), false)
	plan := models.RemediationPlan{
		Action: models.Action{
			Type:       "argocd_sync",
			Namespace:  "argocd",
			Target:     "guestbook",
			Parameters: map[string]string{"revision": "main", "prune": "true"},
		},
	}
	route := toolRoute{tool: "argocd", command: "app sync"}
	action := exec.buildToolAction(plan, route)

	if action.Tool != "argocd" {
		t.Fatalf("expected tool 'argocd', got %q", action.Tool)
	}
	if action.Command != "app sync" {
		t.Fatalf("expected 'app sync', got %q", action.Command)
	}
	if action.Args["app"] != "guestbook" {
		t.Fatalf("expected app 'guestbook', got %q", action.Args["app"])
	}
	if action.Args["revision"] != "main" {
		t.Fatalf("expected revision 'main', got %q", action.Args["revision"])
	}
	if action.Args["prune"] != "true" {
		t.Fatalf("expected prune 'true', got %q", action.Args["prune"])
	}
	if action.Args["namespace"] != "argocd" {
		t.Fatalf("expected namespace 'argocd', got %q", action.Args["namespace"])
	}
}

func TestK8sExecutor_ToolRoute_ExistingK8sActionsUnaffected(t *testing.T) {
	// Verify that existing k8s actions still work without a tool registry.
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "api-1", Namespace: "demo"},
	}
	client := fake.NewSimpleClientset(pod)
	exec := NewK8sExecutorWithClient(client, false)

	plan := models.RemediationPlan{
		Action: models.Action{Type: "restart_pod", Namespace: "demo", Target: "api-1"},
	}
	_, err := exec.Execute(context.Background(), plan)
	if err != nil {
		t.Fatal(err)
	}
}

func int32Ptr(v int32) *int32 { return &v }
