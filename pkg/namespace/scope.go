// Package namespace provides namespace-scoping utilities for watch filters.
package namespace

import "path"

// InScope reports whether ns is within watch. Empty watch means all namespaces.
// Cluster-scoped resources (ns=="") are always allowed. Watch entries support
// trailing wildcard globs (e.g., "kube-*" matches "kube-system").
func InScope(ns string, watch []string) bool {
	if len(watch) == 0 {
		return true
	}
	// Always allow cluster-scoped resources.
	if ns == "" {
		return true
	}
	for _, w := range watch {
		if w == ns {
			return true
		}
		// Support simple wildcard/glob patterns.
		if matched, _ := path.Match(w, ns); matched {
			return true
		}
	}
	return false
}
