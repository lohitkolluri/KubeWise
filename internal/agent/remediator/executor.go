package remediator

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strconv"
	"sync/atomic"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"github.com/lohitkolluri/KubeWise/internal/tools"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// toolRoute maps a remediation action type to a tool plugin name and command.
type toolRoute struct {
	tool    string
	command string
}

// toolActionRoutes routes non-k8s action types to the tool plugin system.
// These are checked in execute() before the k8s API switch.
var toolActionRoutes = map[string]toolRoute{
	"helm_upgrade":     {tool: "helm", command: "upgrade"},
	"helm_rollback":    {tool: "helm", command: "rollback"},
	"argocd_sync":      {tool: "argocd", command: "app sync"},
	"argocd_rollback":  {tool: "argocd", command: "app rollback"},
	"github_create_pr": {tool: "github", command: "create pr"},
	"github_merge_pr":  {tool: "github", command: "merge pr"},
	"terraform_apply":  {tool: "terraform", command: "apply"},
}

// K8sExecutor executes remediation actions against the Kubernetes API
// and dispatches external tool actions (helm, argocd, github, terraform)
// through the tool plugin registry.
type K8sExecutor struct {
	clientset    kubernetes.Interface
	dryRun       atomic.Bool
	toolRegistry *tools.Registry
}

// NewK8sExecutor creates an executor using in-cluster config.
// It also initialises the tool plugin registry with in-cluster tools
// (kubectl, helm). Tools requiring external auth (argocd, github, terraform)
// must be registered separately via RegisterToolPlugin.
func NewK8sExecutor(dryRun bool) (*K8sExecutor, error) {
	config, err := rest.InClusterConfig()
	if err != nil {
		return nil, fmt.Errorf("in-cluster config: %w", err)
	}
	clientset, err := kubernetes.NewForConfig(config)
	if err != nil {
		return nil, fmt.Errorf("create clientset: %w", err)
	}

	reg := tools.NewRegistry()
	// kubectl and helm detect in-cluster config automatically via
	// KUBERNETES_SERVICE_HOST / KUBERNETES_SERVICE_PORT env vars.
	_ = reg.Register(tools.NewKubectlPlugin(""))
	_ = reg.Register(tools.NewHelmPlugin("", ""))
	// argocd and terraform use CLI config/env defaults when config is empty
	_ = reg.Register(tools.NewArgoCDPlugin("", "", false))
	_ = reg.Register(tools.NewTerraformPlugin(""))

	exec := &K8sExecutor{clientset: clientset, toolRegistry: reg}
	exec.dryRun.Store(dryRun)
	return exec, nil
}

// NewK8sExecutorWithClient creates an executor with an existing clientset (for testing).
// The tool registry is nil by default; call SetToolRegistry or RegisterToolPlugin when
// tests need to exercise tool plugin dispatch.
func NewK8sExecutorWithClient(clientset kubernetes.Interface, dryRun bool) *K8sExecutor {
	exec := &K8sExecutor{clientset: clientset}
	exec.dryRun.Store(dryRun)
	return exec
}

// SetToolRegistry sets the tool plugin registry. Used in tests and when wiring
// external tool plugins after construction.
func (e *K8sExecutor) SetToolRegistry(r *tools.Registry) {
	e.toolRegistry = r
}

// RegisterToolPlugin registers a single tool plugin. Creates the registry
// if it does not already exist. Returns an error if the plugin name is
// already registered.
func (e *K8sExecutor) RegisterToolPlugin(plugin tools.ToolPlugin) error {
	if e.toolRegistry == nil {
		e.toolRegistry = tools.NewRegistry()
	}
	return e.toolRegistry.Register(plugin)
}

// SetDryRun updates the dry-run mode.
func (e *K8sExecutor) SetDryRun(dryRun bool) {
	e.dryRun.Store(dryRun)
}

// Clientset exposes the underlying Kubernetes client.
func (e *K8sExecutor) Clientset() kubernetes.Interface {
	return e.clientset
}

// Execute runs the given plan (single action or multi-step runbook).
func (e *K8sExecutor) Execute(ctx context.Context, plan models.RemediationPlan) (string, error) {
	return e.ExecuteRunbook(ctx, plan, e.dryRun.Load())
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

	// Check tool plugin route for external tool actions before the k8s API switch.
	if route, ok := toolActionRoutes[plan.Action.Type]; ok {
		return e.executeToolAction(ctx, plan, route)
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
	case "view_logs":
		return e.viewPodLogs(ctx, plan.Action.Namespace, plan.Action.Target, plan.Action.Parameters)
	case "escalate":
		return "escalated to human operator", nil
	default:
		return "", fmt.Errorf("unknown action type: %s", plan.Action.Type)
	}
}

func (e *K8sExecutor) restartPod(ctx context.Context, namespace, name string) (string, error) {
	err := e.clientset.CoreV1().Pods(namespace).Delete(ctx, name, metav1.DeleteOptions{})
	if err == nil {
		return fmt.Sprintf("deleted pod %s/%s (will be recreated by ReplicaSet)", namespace, name), nil
	}
	if !apierrors.IsNotFound(err) {
		return "", fmt.Errorf("delete pod %s/%s: %w", namespace, name, err)
	}

	owner := inferDeploymentFromPodName(name)
	if owner == "" {
		return "", fmt.Errorf("pod %s/%s not found and could not infer owner", namespace, name)
	}

	labelKeys := []string{"app.kubernetes.io/name", "app"}
	for _, k := range labelKeys {
		sel := labels.SelectorFromSet(map[string]string{k: owner}).String()
		pods, err := e.clientset.CoreV1().Pods(namespace).List(ctx, metav1.ListOptions{LabelSelector: sel})
		if err != nil || len(pods.Items) == 0 {
			continue
		}
		best := pickBestPodForLogs(pods.Items)
		err = e.clientset.CoreV1().Pods(namespace).Delete(ctx, best.Name, metav1.DeleteOptions{})
		if err != nil {
			return "", fmt.Errorf("delete replacement pod %s/%s: %w", namespace, best.Name, err)
		}
		return fmt.Sprintf("deleted replacement pod %s/%s (original %s was already gone)", namespace, best.Name, name), nil
	}

	return "", fmt.Errorf("pod %s/%s not found and no replacement pod found for owner %s", namespace, name, owner)
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
		return "", errors.New("scale_replicas requires 'replicas' parameter")
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

	// Use strategic merge patch to replace only spec.template.spec, avoiding
	// a TOCTOU race between the read and write of the full deployment object.
	patch := map[string]interface{}{
		"spec": map[string]interface{}{
			"template": map[string]interface{}{
				"spec": previous.Spec.Template.Spec,
			},
		},
	}
	patchJSON, err := json.Marshal(patch)
	if err != nil {
		return "", fmt.Errorf("marshal rollback patch: %w", err)
	}
	_, err = e.clientset.AppsV1().Deployments(namespace).Patch(
		ctx, name, types.StrategicMergePatchType, patchJSON, metav1.PatchOptions{})
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
		return "", errors.New("patch_resources requires at least one resource parameter")
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

func (e *K8sExecutor) viewPodLogs(ctx context.Context, namespace, name string, params map[string]string) (string, error) {
	tail := int64(50)
	if t := params["tail"]; t != "" {
		if v, err := strconv.ParseInt(t, 10, 64); err == nil && v > 0 && v < 500 {
			tail = v
		}
	}
	container := params["container"]

	// Prefer direct pod-name lookup when the target looks like a pod name.
	if len(validation.IsDNS1123Subdomain(name)) == 0 {
		if pod, err := e.clientset.CoreV1().Pods(namespace).Get(ctx, name, metav1.GetOptions{}); err == nil {
			return e.getPodLogs(ctx, namespace, pod.Name, container, tail)
		}
	}

	// Fallback: list by common label keys. Build selectors safely (no string concat).
	labelKeys := []string{
		"app.kubernetes.io/name",
		"app",
	}
	seen := sets.NewString()
	for _, k := range labelKeys {
		sel := labels.SelectorFromSet(map[string]string{k: name}).String()
		if seen.Has(sel) {
			continue
		}
		seen.Insert(sel)
		pods, err := e.clientset.CoreV1().Pods(namespace).List(ctx, metav1.ListOptions{LabelSelector: sel})
		if err != nil {
			continue
		}
		if len(pods.Items) == 0 {
			continue
		}
		best := pickBestPodForLogs(pods.Items)
		return e.getPodLogs(ctx, namespace, best.Name, container, tail)
	}

	// Last resort: no match found.
	return "", fmt.Errorf("no pods found for %s/%s", namespace, name)
}

func pickBestPodForLogs(pods []corev1.Pod) corev1.Pod {
	// Prefer Running, then most recent start time, then most recent creation timestamp.
	sort.SliceStable(pods, func(i, j int) bool {
		pi, pj := pods[i], pods[j]
		ri := pi.Status.Phase == corev1.PodRunning
		rj := pj.Status.Phase == corev1.PodRunning
		if ri != rj {
			return ri
		}
		si := pi.Status.StartTime
		sj := pj.Status.StartTime
		if si != nil && sj != nil && !si.Equal(sj) {
			return si.After(sj.Time)
		}
		if si != nil && sj == nil {
			return true
		}
		if si == nil && sj != nil {
			return false
		}
		return pi.CreationTimestamp.After(pj.CreationTimestamp.Time)
	})
	return pods[0]
}

func (e *K8sExecutor) getPodLogs(ctx context.Context, namespace, podName, container string, tail int64) (string, error) {
	opts := &corev1.PodLogOptions{TailLines: &tail}
	if container != "" {
		opts.Container = container
	}
	req := e.clientset.CoreV1().Pods(namespace).GetLogs(podName, opts)
	stream, err := req.Stream(ctx)
	if err != nil {
		return "", fmt.Errorf("stream logs %s/%s: %w", namespace, podName, err)
	}
	defer func() { _ = stream.Close() }()
	data, err := io.ReadAll(stream)
	if err != nil {
		return "", fmt.Errorf("read logs %s/%s: %w", namespace, podName, err)
	}
	return string(data), nil
}

// executeToolAction dispatches a remediation action to the tool plugin system.
// Returns a descriptive result string on success, or an error with a clear message.
func (e *K8sExecutor) executeToolAction(ctx context.Context, plan models.RemediationPlan, route toolRoute) (string, error) {
	if e.toolRegistry == nil {
		return "", fmt.Errorf("tool plugin registry not configured — cannot dispatch %s/%s",
			route.tool, route.command)
	}
	plugin := e.toolRegistry.Get(route.tool)
	if plugin == nil {
		return "", fmt.Errorf("tool plugin %q not registered — cannot execute %s/%s",
			route.tool, route.tool, route.command)
	}

	action := e.buildToolAction(plan, route)
	result, err := e.toolRegistry.Execute(ctx, action)
	if err != nil {
		return "", fmt.Errorf("tool %s/%s failed: %w", route.tool, route.command, err)
	}
	if !result.Success {
		return "", fmt.Errorf("tool %s/%s execution unsuccessful: %s",
			route.tool, route.command, result.Stderr)
	}
	return result.Stdout, nil
}

// buildToolAction converts a RemediationPlan action into a ToolAction for the
// target tool plugin. Plan parameters become tool args; the target and namespace
// are mapped to tool-specific keys when not already present in parameters.
func (e *K8sExecutor) buildToolAction(plan models.RemediationPlan, route toolRoute) models.ToolAction {
	args := make(map[string]string, len(plan.Action.Parameters)+2)
	for k, v := range plan.Action.Parameters {
		args[k] = v
	}

	// Map the plan target to a tool-specific field when the plan uses Target
	// as the primary resource name (e.g. release name for helm, app for argocd).
	if plan.Action.Target != "" {
		switch route.tool {
		case "helm":
			if _, ok := args["release"]; !ok {
				args["release"] = plan.Action.Target
			}
		case "argocd":
			if _, ok := args["app"]; !ok {
				args["app"] = plan.Action.Target
			}
		}
	}

	if plan.Action.Namespace != "" {
		if _, ok := args["namespace"]; !ok {
			args["namespace"] = plan.Action.Namespace
		}
	}

	return models.ToolAction{
		Tool:    route.tool,
		Command: route.command,
		Args:    args,
	}
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
