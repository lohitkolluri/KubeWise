package remediator

import (
	"strings"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

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
	return strings.HasPrefix(podName, deployment+"-")
}

func deploymentFromPlan(plan models.RemediationPlan) string {
	if d := plan.Action.Parameters["deployment"]; d != "" {
		return d
	}
	return plan.Action.Target
}
