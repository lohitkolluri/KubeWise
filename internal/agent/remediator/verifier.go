package remediator

import (
	"context"
	"fmt"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// Verifier confirms remediation outcomes against the cluster.
type Verifier struct {
	clientset kubernetes.Interface
}

// NewVerifier creates a verifier with the given clientset.
func NewVerifier(clientset kubernetes.Interface) *Verifier {
	return &Verifier{clientset: clientset}
}

// Verify runs all checks in the plan. Returns nil when every check passes.
func (v *Verifier) Verify(ctx context.Context, plan models.RemediationPlan) error {
	if v == nil || v.clientset == nil {
		return fmt.Errorf("verifier unavailable")
	}
	checks := plan.Verification.Checks
	if len(checks) == 0 {
		checks = defaultVerificationChecks(plan)
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

func defaultVerificationChecks(plan models.RemediationPlan) []models.VerificationCheck {
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

// inferDeploymentFromPodName attempts to derive the deployment name from a pod name.
// Typical pod name shape: <deployment>-<replicasetHash>-<suffix>.
func inferDeploymentFromPodName(pod string) string {
	parts := strings.Split(pod, "-")
	if len(parts) < 3 {
		return ""
	}
	// Drop last two segments (hash + suffix).
	return strings.Join(parts[:len(parts)-2], "-")
}

func (v *Verifier) runCheck(ctx context.Context, check models.VerificationCheck) error {
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
