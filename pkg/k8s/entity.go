// Package k8s provides entity resolution utilities for KubeWise.
package k8s

import (
	"strings"

	corev1 "k8s.io/api/core/v1"
)

// ResolveOwner extracts the controlling owner kind and name from a pod's OwnerReferences.
// Returns ("", "") when no controller owner is found (e.g., static pods).
func ResolveOwner(pod *corev1.Pod) (kind, name string) {
	if pod == nil {
		return "", ""
	}
	for _, ref := range pod.OwnerReferences {
		if ref.Controller != nil && *ref.Controller {
			return ref.Kind, ref.Name
		}
	}
	if len(pod.OwnerReferences) > 0 {
		ref := pod.OwnerReferences[0]
		return ref.Kind, ref.Name
	}
	return "", ""
}

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

// FormatEntityID creates a stable entity identifier for a workload.
// Format: "namespace/kind/name" or "namespace/name" for backward compatibility.
// This is used for correlation across pod restarts.
func FormatEntityID(namespace, ownerKind, ownerName string) string {
	if ownerKind == "" || ownerName == "" {
		return ""
	}
	if namespace == "" {
		return ownerKind + "/" + ownerName
	}
	return namespace + "/" + ownerKind + "/" + ownerName
}
