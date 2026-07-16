// Package k8s provides entity resolution utilities for KubeWise.
package k8s

import "strings"

// InferWorkloadFromPodName attempts to derive the workload (deployment/statefulset/daemonset)
// name from a pod name using standard K8s naming conventions. Used as a fallback when
// OwnerReferences aren't available (e.g., for anomaly records created without owner info).
//
// Patterns handled:
//   - Deployment:  <name>-<replicasetHash>-<podHash> (3+ segments, strip last 2)
//   - DaemonSet:   <name>-<hash>                     (2 segments, strip last 1)
//   - StatefulSet: <name>-<ordinal>                  (2 segments, strip last 1)
//   - Job:         <name>-<hash>                     (2 segments, strip last 1)
func InferWorkloadFromPodName(podName string) string {
	if podName == "" {
		return ""
	}
	parts := strings.Split(podName, "-")
	if len(parts) < 2 {
		return ""
	}
	if len(parts) >= 3 {
		// Deployment-style: drop last two segments (replicaset hash + pod hash)
		return strings.Join(parts[:len(parts)-2], "-")
	}
	// Exactly 2 segments: DaemonSet, StatefulSet, or Job
	// Return the first segment as the workload name
	return parts[0]
}

// PodBelongsToWorkload checks if a pod name belongs to a given workload (deployment/daemonset/statefulset).
// Uses prefix segment matching to handle names with dashes correctly.
func PodBelongsToWorkload(podName, workload string) bool {
	if workload == "" || podName == "" {
		return false
	}
	parts := strings.Split(podName, "-")
	for i := len(parts); i >= 1; i-- {
		if strings.Join(parts[:i], "-") == workload {
			return true
		}
	}
	return false
}
