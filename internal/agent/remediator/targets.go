package remediator

import (
	"github.com/lohitkolluri/KubeWise/pkg/k8s"
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
	return k8s.PodBelongsToWorkload(podName, deployment)
}

func deploymentFromPlan(plan models.RemediationPlan) string {
	if d := plan.Action.Parameters["deployment"]; d != "" {
		return d
	}
	return plan.Action.Target
}
