package remediator

import (
	"strings"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func matchTarget(a models.AnomalyRecord, namespace, target, actionType string) bool {
	ns, name := models.ParseEntity(a.Entity)
	if a.Namespace != "" && ns == "" {
		ns = a.Namespace
	}
	if ns != namespace {
		return false
	}
	if name == target {
		return true
	}
	if deploymentActions[actionType] {
		return podBelongsToDeployment(name, target)
	}
	if owner := inferDeploymentFromPodName(name); owner != "" && owner == target {
		return true
	}
	if owner := inferDeploymentFromPodName(target); owner != "" && owner == name {
		return true
	}
	return false
}

// anomaliesMatchingPlan filters anomalies that correspond to the plan's target namespace/name.
func (c *Correlator) anomaliesMatchingPlan(anomalies []models.AnomalyRecord, plan models.RemediationPlan) []models.AnomalyRecord {
	// For escalate/noop, still filter by matching anomalies so we properly
	// track which anomalies triggered the escalation (rather than returning all).
	target := plan.Action.Target
	if deploymentActions[plan.Action.Type] {
		target = deploymentFromPlan(plan)
	}
	var matched []models.AnomalyRecord
	for _, a := range anomalies {
		if matchTarget(a, plan.Action.Namespace, target, plan.Action.Type) {
			matched = append(matched, a)
		}
	}
	return matched
}

// deploymentActions require a deployment target rather than a pod name.
var deploymentActions = map[string]bool{
	"scale_replicas":      true,
	"rollback_deployment": true,
	"patch_resources":     true,
}

func podBelongsToDeployment(podName, deployment string) bool {
	if deployment == "" || podName == "" {
		return false
	}
	// Use prefix segment matching: split the pod name and check if the deployment
	// matches any prefix segment. Starting from the longest prefix minimizes false
	// positives when deployment names are substrings of each other.
	//
	// LIMITATION: This is still a heuristic. A deployment "foo" could match a pod
	// from deployment "foo-bar" if "foo-bar" has a pod named "foo-bar-abc-def"
	// and we check against "foo" — the prefix segment "foo" would match. In practice,
	// deployments rarely have names that are exact prefixes of other deployment names
	// within the same namespace. When this ambiguity matters, the caller should
	// verify via the Kubernetes API.
	parts := strings.Split(podName, "-")
	for i := len(parts) - 1; i >= 1; i-- {
		if strings.Join(parts[:i], "-") == deployment {
			return true
		}
	}
	return false
}

func deploymentFromPlan(plan models.RemediationPlan) string {
	if d := plan.Action.Parameters["deployment"]; d != "" {
		return d
	}
	return plan.Action.Target
}
