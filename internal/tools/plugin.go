// Package tools defines the tool plugin interface for KubeWise tool integrations.
//
// Each tool (kubectl, helm, argocd, etc.) implements the ToolPlugin interface.
// The Registry manages registered plugins and dispatches actions to them.
package tools

import (
	"context"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// ToolPlugin is the interface each tool integration implements.
type ToolPlugin interface {
	// Name returns the canonical tool name (e.g. "kubectl", "helm").
	Name() string

	// Capabilities returns the set of capabilities this tool supports.
	Capabilities() []models.ToolCapability

	// Validate checks whether the action is valid for this tool before execution.
	Validate(action models.ToolAction) error

	// Execute runs the tool action and returns the result.
	Execute(ctx context.Context, action models.ToolAction) (*models.ToolResult, error)
}

// OutputMaxBytes is the maximum size for captured tool output (10KB).
const OutputMaxBytes = 10 * 1024

// Truncate truncates a string to max bytes, appending "..." if truncated.
func Truncate(s string, maxBytes int) string {
	if len(s) <= maxBytes {
		return s
	}
	return s[:maxBytes] + "..."
}
