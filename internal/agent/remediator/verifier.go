package remediator

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/prometheus/client_golang/api"
	v1 "github.com/prometheus/client_golang/api/prometheus/v1"
	"github.com/prometheus/common/model"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/client-go/kubernetes"

	"github.com/lohitkolluri/KubeWise/pkg/k8s"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// Verifier confirms remediation outcomes against the cluster.
type Verifier struct {
	clientset kubernetes.Interface
	promAPI   v1.API // Prometheus API for PromQL verification checks
}

// NewVerifier creates a verifier with the given clientset.
// Optionally accepts a Prometheus API instance for PromQL-based verification
// checks. When promAPI is nil, "promql" check types are skipped.
func NewVerifier(clientset kubernetes.Interface, promAPI ...v1.API) *Verifier {
	v := &Verifier{clientset: clientset}
	if len(promAPI) > 0 {
		v.promAPI = promAPI[0]
	}
	return v
}

// NewVerifierWithPrometheus creates a verifier from a Prometheus address string.
// This is a convenience constructor that creates the Prometheus API client
// internally.
func NewVerifierWithPrometheus(clientset kubernetes.Interface, promAddr string) *Verifier {
	v := &Verifier{clientset: clientset}
	if promAddr != "" {
		client, err := api.NewClient(api.Config{Address: promAddr})
		if err == nil {
			v.promAPI = v1.NewAPI(client)
		}
	}
	return v
}

// Verify runs all checks in the plan. Returns nil when every check passes.
func (v *Verifier) Verify(ctx context.Context, plan models.RemediationPlan) error {
	if v == nil || v.clientset == nil {
		return errors.New("verifier unavailable")
	}
	checks := plan.Verification.Checks
	if len(checks) == 0 {
		checks = defaultVerificationChecks(plan, v.clientset)
	}
	if len(checks) == 0 {
		return nil
	}

	wait := time.Duration(plan.Verification.WaitSeconds) * time.Second
	if wait <= 0 {
		// Default waits tuned for common Kubernetes rollouts:
		// - deployment readiness often needs longer than a pod restart
		// - restart/delete pod changes the pod name; default check becomes deployment_ready
		wait = defaultVerifyWait(checks)
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(wait):
	}

	var failures []string
	for _, check := range checks {
		if err := v.runCheck(ctx, check); err != nil {
			failures = append(failures, err.Error())
		}
	}
	if len(failures) > 0 {
		return fmt.Errorf("verification failed: %s", strings.Join(failures, "; "))
	}
	return nil
}

func defaultVerifyWait(checks []models.VerificationCheck) time.Duration {
	for _, c := range checks {
		if c.Type == "deployment_ready" {
			return 45 * time.Second
		}
	}
	return 20 * time.Second
}

func defaultVerificationChecks(plan models.RemediationPlan, clientset ...kubernetes.Interface) []models.VerificationCheck {
	steps := plan.EffectiveSteps()
	if len(steps) == 0 {
		return nil
	}
	last := steps[len(steps)-1]
	ns, target := last.Namespace, last.Target

	// Pod restarts/deletions typically change the pod name, so checking a specific pod
	// by name yields false negatives. Prefer deployment readiness when we can infer it.
	if last.Type == "restart_pod" || last.Type == "delete_pod" {
		if dep := inferDeploymentFromPodName(target); dep != "" {
			return []models.VerificationCheck{{
				Type: "deployment_ready", Namespace: ns, Target: dep,
			}}
		}
		// If name-based inference fails, try the Kubernetes API (DaemonSet, StatefulSet).
		if len(clientset) > 0 && clientset[0] != nil {
			if owner := inferOwnerFromPod(clientset[0], ns, target); owner != "" {
				return []models.VerificationCheck{{
					Type: "deployment_ready", Namespace: ns, Target: owner,
				}}
			}
		}
		// If we can't infer the owner, skip strict verification rather than failing noisily.
		return nil
	}

	if deploymentActions[last.Type] {
		if d := last.Parameters["deployment"]; d != "" {
			target = d
		}
		return []models.VerificationCheck{{
			Type: "deployment_ready", Namespace: ns, Target: target,
		}}
	}
	switch last.Type {
	case "noop", "escalate":
		return nil
	default:
		return []models.VerificationCheck{{
			Type: "pod_ready", Namespace: ns, Target: target,
		}}
	}
}

// inferDeploymentFromPodName attempts to derive the deployment/owner name from a pod name.
// Delegates to the shared k8s.InferWorkloadFromPodName for standard K8s naming conventions.
// Typical pod name shapes:
//   - Deployment:  <name>-<replicasetHash>-<podHash>      (2 trailing segments)
//   - DaemonSet:   <name>-<hash>                           (1 trailing hash segment)
//   - StatefulSet: <name>-<ordinal>                        (1 trailing numeric segment)
func inferDeploymentFromPodName(pod string) string {
	return k8s.InferWorkloadFromPodName(pod)
}

// inferOwnerFromPod queries the Kubernetes API for a pod and reads its OwnerReferences
// to determine the owning workload name. Returns the owner name, or "" on failure.
// This handles DaemonSet, StatefulSet, and other controllers where the pod name
// alone does not follow the <deployment>-<hash>-<suffix> pattern.
func inferOwnerFromPod(clientset kubernetes.Interface, namespace, podName string) string {
	if clientset == nil {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	pod, err := clientset.CoreV1().Pods(namespace).Get(ctx, podName, metav1.GetOptions{})
	if err != nil {
		return ""
	}
	for _, ref := range pod.OwnerReferences {
		if ref.Controller != nil && *ref.Controller {
			return ref.Name
		}
	}
	if len(pod.OwnerReferences) > 0 {
		return pod.OwnerReferences[0].Name
	}
	return ""
}

// resolvePodName handles the case where a pod was restarted and has a new name.
// If the exact pod name doesn't exist, tries to find a replacement via owner labels.
func (v *Verifier) resolvePodName(ctx context.Context, namespace, name string) (string, error) {
	_, err := v.clientset.CoreV1().Pods(namespace).Get(ctx, name, metav1.GetOptions{})
	if err == nil {
		return name, nil // pod exists
	}
	if !apierrors.IsNotFound(err) {
		return "", err
	}
	// Pod doesn't exist — try owner inference
	owner := inferDeploymentFromPodName(name)
	if owner == "" {
		return "", fmt.Errorf("pod %s/%s not found and cannot infer owner", namespace, name)
	}
	labelKeys := []string{"app.kubernetes.io/name", "app"}
	for _, k := range labelKeys {
		sel := labels.SelectorFromSet(map[string]string{k: owner}).String()
		pods, err := v.clientset.CoreV1().Pods(namespace).List(ctx, metav1.ListOptions{LabelSelector: sel})
		if err != nil || len(pods.Items) == 0 {
			continue
		}
		return pickBestPodForLogs(pods.Items).Name, nil
	}
	return "", fmt.Errorf("pod %s/%s not found and no replacement for owner %s", namespace, name, owner)
}

func (v *Verifier) runCheck(ctx context.Context, check models.VerificationCheck) error {
	// Resolve the target name: if the pod was restarted, find its replacement.
	resolved, err := v.resolvePodName(ctx, check.Namespace, check.Target)
	if err != nil {
		return err
	}
	check.Target = resolved

	switch check.Type {
	case "pod_ready":
		return v.checkPodReady(ctx, check.Namespace, check.Target)
	case "pod_phase":
		want := check.Parameters["phase"]
		if want == "" {
			want = string(corev1.PodRunning)
		}
		return v.checkPodPhase(ctx, check.Namespace, check.Target, want)
	case "no_crashloop":
		return v.checkNoCrashLoop(ctx, check.Namespace, check.Target)
	case "deployment_ready":
		return v.checkDeploymentReady(ctx, check.Namespace, check.Target)
	case "promql":
		return v.checkPromQL(ctx, check)
	default:
		return fmt.Errorf("unknown verification check %q", check.Type)
	}
}

func (v *Verifier) checkPodReady(ctx context.Context, namespace, name string) error {
	pod, err := v.clientset.CoreV1().Pods(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return fmt.Errorf("pod_ready %s/%s: %w", namespace, name, err)
	}
	if pod.Status.Phase != corev1.PodRunning {
		return fmt.Errorf("pod_ready %s/%s: phase=%s", namespace, name, pod.Status.Phase)
	}
	for _, cs := range pod.Status.ContainerStatuses {
		if !cs.Ready {
			return fmt.Errorf("pod_ready %s/%s: container %s not ready", namespace, name, cs.Name)
		}
		if cs.State.Waiting != nil && cs.State.Waiting.Reason == "CrashLoopBackOff" {
			return fmt.Errorf("pod_ready %s/%s: container %s in CrashLoopBackOff", namespace, name, cs.Name)
		}
	}
	return nil
}

func (v *Verifier) checkPodPhase(ctx context.Context, namespace, name, want string) error {
	pod, err := v.clientset.CoreV1().Pods(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return fmt.Errorf("pod_phase %s/%s: %w", namespace, name, err)
	}
	if string(pod.Status.Phase) != want {
		return fmt.Errorf("pod_phase %s/%s: got %s want %s", namespace, name, pod.Status.Phase, want)
	}
	return nil
}

func (v *Verifier) checkNoCrashLoop(ctx context.Context, namespace, name string) error {
	pod, err := v.clientset.CoreV1().Pods(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return fmt.Errorf("no_crashloop %s/%s: %w", namespace, name, err)
	}
	for _, cs := range pod.Status.ContainerStatuses {
		if cs.State.Waiting != nil && cs.State.Waiting.Reason == "CrashLoopBackOff" {
			return fmt.Errorf("no_crashloop %s/%s: container %s still in CrashLoopBackOff", namespace, name, cs.Name)
		}
	}
	return nil
}

func (v *Verifier) checkDeploymentReady(ctx context.Context, namespace, name string) error {
	dep, err := v.clientset.AppsV1().Deployments(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return fmt.Errorf("deployment_ready %s/%s: %w", namespace, name, err)
	}
	if dep.Status.AvailableReplicas < 1 {
		return fmt.Errorf("deployment_ready %s/%s: available=%d", namespace, name, dep.Status.AvailableReplicas)
	}
	if dep.Spec.Replicas != nil && dep.Status.ReadyReplicas < *dep.Spec.Replicas {
		return fmt.Errorf("deployment_ready %s/%s: ready=%d want=%d", namespace, name, dep.Status.ReadyReplicas, *dep.Spec.Replicas)
	}
	return nil
}

// checkPromQL executes a PromQL query and compares the result against a
// threshold. The check.Parameters must contain "query" (the PromQL expression).
// An optional "threshold" parameter (default: "0.5") sets the maximum allowed
// value. Returns an error if any result series exceeds the threshold.
func (v *Verifier) checkPromQL(ctx context.Context, check models.VerificationCheck) error {
	if v.promAPI == nil {
		return errors.New("promql check skipped: no Prometheus API configured")
	}
	query, ok := check.Parameters["query"]
	if !ok || query == "" {
		return errors.New("promql check requires 'query' parameter")
	}
	thresholdStr, ok := check.Parameters["threshold"]
	if !ok {
		thresholdStr = "0.5"
	}
	threshold, err := strconv.ParseFloat(thresholdStr, 64)
	if err != nil {
		return fmt.Errorf("invalid threshold %q: %w", thresholdStr, err)
	}

	// Execute the PromQL query.
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()

	result, _, err := v.promAPI.Query(ctx, query, time.Now())
	if err != nil {
		return fmt.Errorf("promql query failed: %w", err)
	}

	// Check the result against the threshold.
	switch vec := result.(type) {
	case model.Vector:
		for _, s := range vec {
			if float64(s.Value) > threshold {
				return fmt.Errorf("promql check failed: %s = %v > threshold %f", query, s.Value, threshold)
			}
		}
		return nil
	default:
		return fmt.Errorf("unexpected promql result type %T", result)
	}
}
