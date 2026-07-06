package remediator

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"

	appsv1 "k8s.io/api/apps/v1"
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

// Clientset exposes the underlying Kubernetes client.
func (e *K8sExecutor) Clientset() kubernetes.Interface {
	return e.clientset
}

// Execute runs the given plan (single action or multi-step runbook).
func (e *K8sExecutor) Execute(ctx context.Context, plan models.RemediationPlan) (string, error) {
	return e.ExecuteRunbook(ctx, plan, e.dryRun)
}

// ExecuteForce runs the plan even when dry-run is enabled (operator-approved).
func (e *K8sExecutor) ExecuteForce(ctx context.Context, plan models.RemediationPlan) (string, error) {
	return e.ExecuteRunbook(ctx, plan, false)
}

func (e *K8sExecutor) execute(ctx context.Context, plan models.RemediationPlan, dryRun bool) (string, error) {
	if dryRun {
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
	replicas, err := strconv.ParseInt(replicasStr, 10, 32)
	if err != nil {
		return "", fmt.Errorf("invalid replicas value %q: %w", replicasStr, err)
	}
	if replicas < 0 || replicas > 100 {
		return "", fmt.Errorf("replicas %d out of allowed range [0, 100]", replicas)
	}
	replicas32 := int32(replicas)

	patch := fmt.Sprintf(`{"spec":{"replicas":%d}}`, replicas32)
	_, err = e.clientset.AppsV1().Deployments(namespace).Patch(
		ctx, name, types.StrategicMergePatchType, []byte(patch), metav1.PatchOptions{})
	if err != nil {
		return "", fmt.Errorf("scale deployment %s/%s to %d: %w", namespace, name, replicas32, err)
	}
	return fmt.Sprintf("scaled deployment %s/%s to %d replicas", namespace, name, replicas32), nil
}

func (e *K8sExecutor) rollbackDeployment(ctx context.Context, namespace, name string) (string, error) {
	deployment, err := e.clientset.AppsV1().Deployments(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return "", fmt.Errorf("get deployment %s/%s: %w", namespace, name, err)
	}
	if deployment.Spec.Paused {
		return "", fmt.Errorf("deployment %s/%s is paused, cannot rollback", namespace, name)
	}

	labelSelector := metav1.FormatLabelSelector(deployment.Spec.Selector)
	rsList, err := e.clientset.AppsV1().ReplicaSets(namespace).List(ctx, metav1.ListOptions{
		LabelSelector: labelSelector,
	})
	if err != nil {
		return "", fmt.Errorf("list replicasets for %s/%s: %w", namespace, name, err)
	}

	type revisionedRS struct {
		revision int64
		rs       appsv1.ReplicaSet
	}
	var revisions []revisionedRS
	for _, rs := range rsList.Items {
		revStr := rs.Annotations["deployment.kubernetes.io/revision"]
		if revStr == "" {
			continue
		}
		rev, err := strconv.ParseInt(revStr, 10, 64)
		if err != nil {
			continue
		}
		revisions = append(revisions, revisionedRS{revision: rev, rs: rs})
	}
	if len(revisions) < 2 {
		return "", fmt.Errorf("deployment %s/%s has no previous revision to roll back to", namespace, name)
	}

	sort.Slice(revisions, func(i, j int) bool { return revisions[i].revision > revisions[j].revision })
	previous := revisions[1].rs
	deployment.Spec.Template = previous.Spec.Template
	_, err = e.clientset.AppsV1().Deployments(namespace).Update(ctx, deployment, metav1.UpdateOptions{})
	if err != nil {
		return "", fmt.Errorf("rollback deployment %s/%s to revision %d: %w", namespace, name, revisions[1].revision, err)
	}

	return fmt.Sprintf("rolled back deployment %s/%s to revision %d", namespace, name, revisions[1].revision), nil
}

func (e *K8sExecutor) patchResources(ctx context.Context, namespace, name string, params map[string]string) (string, error) {
	container := params["container"]
	if container == "" {
		container = name
	}

	if len(resourceParams(params)) == 0 {
		return "", fmt.Errorf("patch_resources requires at least one resource parameter")
	}

	deployment, err := e.clientset.AppsV1().Deployments(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return "", fmt.Errorf("get deployment %s/%s: %w", namespace, name, err)
	}
	found := false
	for _, c := range deployment.Spec.Template.Spec.Containers {
		if c.Name == container {
			found = true
			break
		}
	}
	if !found {
		return "", fmt.Errorf("container %q not found in deployment %s/%s", container, namespace, name)
	}

	resources := resourceParams(params)
	patch := map[string]interface{}{
		"spec": map[string]interface{}{
			"template": map[string]interface{}{
				"spec": map[string]interface{}{
					"containers": []map[string]interface{}{
						{"name": container, "resources": resources},
					},
				},
			},
		},
	}
	patchJSON, err := json.Marshal(patch)
	if err != nil {
		return "", fmt.Errorf("marshal patch: %w", err)
	}

	_, err = e.clientset.AppsV1().Deployments(namespace).Patch(
		ctx, name, types.StrategicMergePatchType, patchJSON, metav1.PatchOptions{})
	if err != nil {
		return "", fmt.Errorf("patch resources for deployment %s/%s container %s: %w", namespace, name, container, err)
	}

	return fmt.Sprintf("patched resources for deployment %s/%s container %s", namespace, name, container), nil
}

func resourceParams(params map[string]string) map[string]map[string]string {
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
	return parts
}

func ensureMap(m map[string]map[string]string, key string) map[string]string {
	if _, ok := m[key]; !ok {
		m[key] = make(map[string]string)
	}
	return m[key]
}

// compile-time interface checks
var _ kubernetes.Interface = (*kubernetes.Clientset)(nil)
