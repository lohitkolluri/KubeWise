// Package namespace provides namespace-scoping utilities for watch filters.
package namespace

// InScope reports whether ns is within watch. Empty watch means all namespaces.
func InScope(ns string, watch []string) bool {
	if len(watch) == 0 {
		return true
	}
	for _, w := range watch {
		if w == ns {
			return true
		}
	}
	return false
}
