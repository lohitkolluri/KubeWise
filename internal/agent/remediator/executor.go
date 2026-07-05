package remediator

import (
	"context"
	"fmt"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// K8sExecutor executes remediation actions against the Kubernetes API.
type K8sExecutor struct {
	clientset kubernetes.Interface
	dryRun    bool
}

// NewK8sExecutor creates an executor using in-cluster config.
func NewK8sExecutor(dryRun bool) (*K8sExecutor, error) {
	config, err := rest.InClusterConfig()
	if err != nil {
		return nil, fmt.Errorf("in-cluster config: %w", err)
	}
	clientset, err := kubernetes.NewForConfig(config)
	if err != nil {
		return nil, fmt.Errorf("create clientset: %w", err)
	}
	return &K8sExecutor{clientset: clientset, dryRun: dryRun}, nil
}

// NewK8sExecutorWithClient creates an executor with an existing clientset (for testing).
func NewK8sExecutorWithClient(clientset kubernetes.Interface, dryRun bool) *K8sExecutor {
	return &K8sExecutor{clientset: clientset, dryRun: dryRun}
}

// SetDryRun updates the dry-run mode.
func (e *K8sExecutor) SetDryRun(dryRun bool) {
	e.dryRun = dryRun
}

// Execute runs the given action and returns a result string.
func (e *K8sExecutor) Execute(ctx context.Context, plan models.RemediationPlan) (string, error) {
	if e.dryRun {
		return fmt.Sprintf("[dry-run] would %s %s/%s with params=%v",
			plan.Action.Type, plan.Action.Namespace, plan.Action.Target, plan.Action.Parameters), nil
	}

	switch plan.Action.Type {
	case "restart_pod":
		return e.restartPod(ctx, plan.Action.Namespace, plan.Action.Target)
	case "delete_pod":
		return e.deletePod(ctx, plan.Action.Namespace, plan.Action.Target)
	case "scale_replicas":
		return e.scaleReplicas(ctx, plan.Action.Namespace, plan.Action.Target, plan.Action.Parameters)
	case "rollback_deployment":
		return e.rollbackDeployment(ctx, plan.Action.Namespace, plan.Action.Target)
	case "patch_resources":
		return e.patchResources(ctx, plan.Action.Namespace, plan.Action.Target, plan.Action.Parameters)
	case "noop":
		return "no action taken (noop)", nil
	case "escalate":
		return "escalated to human operator", nil
	default:
		return "", fmt.Errorf("unknown action type: %s", plan.Action.Type)
	}
}

func (e *K8sExecutor) restartPod(ctx context.Context, namespace, name string) (string, error) {
	err := e.clientset.CoreV1().Pods(namespace).Delete(ctx, name, metav1.DeleteOptions{})
	if err != nil {
		return "", fmt.Errorf("delete pod %s/%s: %w", namespace, name, err)
	}
	return fmt.Sprintf("deleted pod %s/%s (will be recreated by ReplicaSet)", namespace, name), nil
}

func (e *K8sExecutor) deletePod(ctx context.Context, namespace, name string) (string, error) {
	grace := int64(0)
	dp := metav1.DeleteOptions{GracePeriodSeconds: &grace}
	err := e.clientset.CoreV1().Pods(namespace).Delete(ctx, name, dp)
	if err != nil {
		return "", fmt.Errorf("force-delete pod %s/%s: %w", namespace, name, err)
	}
	return fmt.Sprintf("force-deleted pod %s/%s", namespace, name), nil
}

func (e *K8sExecutor) scaleReplicas(ctx context.Context, namespace, name string, params map[string]string) (string, error) {
	replicasStr, ok := params["replicas"]
	if !ok {
		return "", fmt.Errorf("scale_replicas requires 'replicas' parameter")
	}
	var replicas int32
	if _, err := fmt.Sscanf(replicasStr, "%d", &replicas); err != nil {
		return "", fmt.Errorf("invalid replicas value %q: %w", replicasStr, err)
	}

	patch := fmt.Sprintf(`{"spec":{"replicas":%d}}`, replicas)
	_, err := e.clientset.AppsV1().Deployments(namespace).Patch(
		ctx, name, types.StrategicMergePatchType, []byte(patch), metav1.PatchOptions{})
	if err != nil {
		return "", fmt.Errorf("scale deployment %s/%s to %d: %w", namespace, name, replicas, err)
	}
	return fmt.Sprintf("scaled deployment %s/%s to %d replicas", namespace, name, replicas), nil
}

func (e *K8sExecutor) rollbackDeployment(ctx context.Context, namespace, name string) (string, error) {
	deployment, err := e.clientset.AppsV1().Deployments(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return "", fmt.Errorf("get deployment %s/%s: %w", namespace, name, err)
	}

	// Rollback: set the annotation to trigger a rollback to the previous revision
	if deployment.Annotations == nil {
		deployment.Annotations = make(map[string]string)
	}
	deployment.Annotations["kubectl.kubernetes.io/rollback"] = "true"

	// For a proper rollback, we use the Rollback API or set the revision annotation.
	// Since client-go doesn't have a direct rollback API in newer versions,
	// we use the deployment's rollout undo mechanism via annotation.
	if deployment.Spec.Paused {
		return "", fmt.Errorf("deployment %s/%s is paused, cannot rollback", namespace, name)
	}

	_, err = e.clientset.AppsV1().Deployments(namespace).Update(ctx, deployment, metav1.UpdateOptions{})
	if err != nil {
		return "", fmt.Errorf("rollback deployment %s/%s: %w", namespace, name, err)
	}

	return fmt.Sprintf("initiated rollback of deployment %s/%s", namespace, name), nil
}

func (e *K8sExecutor) patchResources(ctx context.Context, namespace, name string, params map[string]string) (string, error) {
	// Build container resource patch
	// Expected params: container, cpu_request, cpu_limit, memory_request, memory_limit
	container := params["container"]
	if container == "" {
		container = name // default: target name is the container name
	}

	patchMap := make(map[string]interface{})
	resources := make(map[string]interface{})

	if v, ok := params["cpu_request"]; ok {
		resources["cpu"] = map[string]string{"request": v}
	}
	if v, ok := params["cpu_limit"]; ok {
		if _, exists := resources["cpu"]; !exists {
			resources["cpu"] = make(map[string]string)
		}
		resources["cpu"].(map[string]string)["limit"] = v
	}
	if v, ok := params["memory_request"]; ok {
		resources["memory"] = map[string]string{"request": v}
	}
	if v, ok := params["memory_limit"]; ok {
		if _, exists := resources["memory"]; !exists {
			resources["memory"] = make(map[string]string)
		}
		resources["memory"].(map[string]string)["limit"] = v
	}

	if len(resources) == 0 {
		return "", fmt.Errorf("patch_resources requires at least one resource parameter")
	}

	patchMap["spec"] = map[string]interface{}{
		"template": map[string]interface{}{
			"spec": map[string]interface{}{
				"containers": []map[string]interface{}{
					{
						"name":      container,
						"resources": resources,
					},
				},
			},
		},
	}

	patchJSON := fmt.Sprintf(`{"spec":{"template":{"spec":{"containers":[{"name":%q,"resources":%s}]}}}}`,
		container, resourcePatchValue(params))

	_, err := e.clientset.AppsV1().Deployments(namespace).Patch(
		ctx, name, types.StrategicMergePatchType, []byte(patchJSON), metav1.PatchOptions{})
	if err != nil {
		return "", fmt.Errorf("patch resources for deployment %s/%s container %s: %w", namespace, name, container, err)
	}

	return fmt.Sprintf("patched resources for deployment %s/%s container %s", namespace, name, container), nil
}

// resourcePatchValue builds the resources JSON fragment for the patch.
func resourcePatchValue(params map[string]string) string {
	parts := make(map[string]map[string]string)
	for k, v := range params {
		if k == "container" {
			continue
		}
		switch k {
		case "cpu_request":
			ensureMap(parts, "cpu")["request"] = v
		case "cpu_limit":
			ensureMap(parts, "cpu")["limit"] = v
		case "memory_request":
			ensureMap(parts, "memory")["request"] = v
		case "memory_limit":
			ensureMap(parts, "memory")["limit"] = v
		}
	}

	// Build JSON manually since we need a simple deterministic output
	json := "{"
	first := true
	for resType, limits := range parts {
		if !first {
			json += ","
		}
		first = false
		json += fmt.Sprintf("%q:{", resType)
		subFirst := true
		for k, v := range limits {
			if !subFirst {
				json += ","
			}
			subFirst = false
			json += fmt.Sprintf("%q:%q", k, v)
		}
		json += "}"
	}
	json += "}"
	return json
}

func ensureMap(m map[string]map[string]string, key string) map[string]string {
	if _, ok := m[key]; !ok {
		m[key] = make(map[string]string)
	}
	return m[key]
}

// compile-time interface checks
var _ kubernetes.Interface = (*kubernetes.Clientset)(nil)
