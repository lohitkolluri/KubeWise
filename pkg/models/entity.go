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
func ParseEntity(entity string) (namespace, name string) {
	if idx := strings.Index(entity, "/"); idx >= 0 {
		return entity[:idx], entity[idx+1:]
	}
	return "", entity
}
