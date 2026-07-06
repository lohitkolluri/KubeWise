package models

import "strings"

// FormatEntity returns a canonical entity ID as namespace/name when namespace is set.
func FormatEntity(namespace, name string) string {
	if namespace == "" {
		return name
	}
	return namespace + "/" + name
}

// ParseEntity splits a canonical entity ID into namespace and name.
// Kind-prefixed forms like Pod/nginx-abc return ("", "nginx-abc").
func ParseEntity(entity string) (namespace, name string) {
	if idx := strings.Index(entity, "/"); idx >= 0 {
		left, right := entity[:idx], entity[idx+1:]
		switch left {
		case "Pod", "Deployment", "Service", "Node", "ReplicaSet":
			return "", right
		default:
			return left, right
		}
	}
	return "", entity
}
