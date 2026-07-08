package remediator

import (
	"strings"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

var actionTypeAliases = map[string]string{
	"restart":             "restart_pod",
	"restart_pod":         "restart_pod",
	"delete":              "delete_pod",
	"delete_pod":          "delete_pod",
	"scale":               "scale_replicas",
	"scale_replicas":      "scale_replicas",
	"rollback":            "rollback_deployment",
	"rollback_deployment": "rollback_deployment",
	"patch":               "patch_resources",
	"patch_resources":     "patch_resources",
	"escalate":            "escalate",
	"noop":                "noop",
	"none":                "noop",
}

var blastRadiusAliases = map[string]string{
	"single_pod":    "single_pod",
	"single pod":    "single_pod",
	"single-pod":    "single_pod",
	"pod":           "single_pod",
	"multiple_pods": "multiple_pods",
	"multiple pods": "multiple_pods",
	"service":       "service",
	"cluster":       "cluster",
}

// normalizePlan repairs common LLM formatting mistakes before validation.
func normalizePlan(plan *models.RemediationPlan, anomalies []models.AnomalyRecord) {
	if plan == nil {
		return
	}

	plan.Action.Type = normalizeActionType(plan.Action.Type, plan.Action.Target)
	plan.Action.Target = cleanTargetName(plan.Action.Target)
	plan.Action.Namespace = strings.TrimSpace(plan.Action.Namespace)
	plan.Risk.BlastRadius = normalizeBlastRadius(plan.Risk.BlastRadius)

	// LLM sometimes puts "namespace/name" in target or swaps type/target.
	splitNamespaceTarget(&plan.Action)

	// If target is a known action type, the fields were likely swapped.
	if knownActionTypes[plan.Action.Target] && !knownActionTypes[plan.Action.Type] {
		plan.Action.Type, plan.Action.Target = plan.Action.Target, plan.Action.Type
		plan.Action.Type = normalizeActionType(plan.Action.Type, plan.Action.Target)
	}

	// Generic placeholders: infer from anomalies.
	if isGenericTarget(plan.Action.Target) {
		if ns, name := inferTargetFromAnomalies(anomalies, plan.Action.Namespace); name != "" {
			if plan.Action.Namespace == "" {
				plan.Action.Namespace = ns
			}
			plan.Action.Target = name
		}
	}

	if plan.Action.Namespace == "" {
		if ns, _ := inferTargetFromAnomalies(anomalies, ""); ns != "" {
			plan.Action.Namespace = ns
		}
	}

	// Target may still embed namespace prefix.
	if ns, name := models.ParseEntity(plan.Action.Target); name != "" && ns != "" {
		if plan.Action.Namespace == "" || plan.Action.Namespace == ns {
			plan.Action.Namespace = ns
			plan.Action.Target = name
		}
	}

	plan.Action.Target = cleanTargetName(plan.Action.Target)

	if plan.Risk.BlastRadius == "" && plan.Action.Type != "noop" && plan.Action.Type != "escalate" {
		plan.Risk.BlastRadius = "single_pod"
	}
	if plan.Risk.EstimatedTimeToResolve == "" {
		plan.Risk.EstimatedTimeToResolve = "2m"
	}

	normalizeRunbookSteps(plan, anomalies)
	syncActionFromSteps(plan)
}

func normalizeRunbookSteps(plan *models.RemediationPlan, anomalies []models.AnomalyRecord) {
	if len(plan.Steps) == 0 {
		return
	}
	out := make([]models.RunbookStep, 0, len(plan.Steps))
	for i := range plan.Steps {
		step := plan.Steps[i]
		if step.Order == 0 {
			step.Order = i + 1
		}
		step.Type = normalizeActionType(step.Type, step.Target)
		step.Target = cleanTargetName(step.Target)
		step.Namespace = strings.TrimSpace(step.Namespace)
		if step.Namespace == "" {
			step.Namespace = plan.Action.Namespace
		}
		// Drop any structurally invalid steps instead of failing the whole plan.
		// This prevents cases where the model emits an extra empty step.
		if step.Type == "" {
			continue
		}
		if step.Type == waitActionType {
			out = append(out, step)
			continue
		}
		if isGenericTarget(step.Target) {
			if ns, name := inferTargetFromAnomalies(anomalies, step.Namespace); name != "" {
				if step.Namespace == "" {
					step.Namespace = ns
				}
				step.Target = name
			}
		}
		// patch_resources without parameters is a common LLM error.
		// Demote to noop instead of failing the entire plan.
		if step.Type == "patch_resources" && len(step.Parameters) == 0 {
			step.Type = "noop"
			step.Rationale = "demoted from patch_resources: missing resource parameters"
		}
		out = append(out, step)
	}
	// Re-number to keep sequential ordering after dropping steps.
	for i := range out {
		out[i].Order = i + 1
	}
	plan.Steps = out
}

func normalizeActionType(actionType, target string) string {
	t := strings.ToLower(strings.TrimSpace(actionType))
	t = strings.ReplaceAll(t, " ", "_")
	t = strings.ReplaceAll(t, "-", "_")
	if alias, ok := actionTypeAliases[t]; ok {
		return alias
	}
	// Model returned target name as type (e.g. "crashloop-demo").
	if knownActionTypes[target] {
		return target
	}
	return t
}

func cleanTargetName(target string) string {
	t := strings.TrimSpace(target)
	for _, prefix := range []string{"pod/", "pods/", "deployment/", "deploy/", "replicaset/", "rs/"} {
		if strings.HasPrefix(strings.ToLower(t), prefix) {
			t = t[len(prefix):]
		}
	}
	return strings.TrimSpace(t)
}

func normalizeBlastRadius(r string) string {
	r = strings.ToLower(strings.TrimSpace(r))
	if alias, ok := blastRadiusAliases[r]; ok {
		return alias
	}
	return strings.ReplaceAll(r, " ", "_")
}

func splitNamespaceTarget(action *models.Action) {
	if action == nil || !strings.Contains(action.Target, "/") {
		return
	}
	parts := strings.SplitN(action.Target, "/", 2)
	if len(parts) != 2 || parts[1] == "" {
		return
	}
	if isGenericTarget(parts[1]) {
		if action.Namespace == "" {
			action.Namespace = parts[0]
		}
		action.Target = parts[1]
		return
	}
	if knownActionTypes[parts[1]] {
		return
	}
	if action.Namespace == "" || action.Namespace == parts[0] {
		action.Namespace = parts[0]
		action.Target = parts[1]
	}
}

func isGenericTarget(target string) bool {
	switch strings.ToLower(strings.TrimSpace(target)) {
	case "", "pod", "pods", "deployment", "deploy", "service", "container", "namespace":
		return true
	default:
		return false
	}
}

func inferTargetFromAnomalies(anomalies []models.AnomalyRecord, preferNS string) (namespace, name string) {
	for _, a := range anomalies {
		ns, n := models.ParseEntity(a.Entity)
		if a.Namespace != "" && ns == "" {
			ns = a.Namespace
		}
		if n == "" {
			continue
		}
		if preferNS != "" && ns != preferNS {
			continue
		}
		return ns, n
	}
	if len(anomalies) == 0 {
		return "", ""
	}
	a := anomalies[0]
	ns, n := models.ParseEntity(a.Entity)
	if a.Namespace != "" && ns == "" {
		ns = a.Namespace
	}
	return ns, n
}
