// Package tools defines the tool plugin interface and registry for KubeWise.
//
// Registry manages tool plugin lifecycle — registration, lookup, and dispatch.
// Each tool integration (kubectl, helm, argocd, etc.) implements the ToolPlugin
// interface and is registered with the Registry for use by the remediation executor.
package tools

import (
	"context"
	"fmt"
	"sync"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// Registry manages tool plugin registration and dispatch.
// It is safe for concurrent use.
type Registry struct {
	mu    sync.RWMutex
	tools map[string]ToolPlugin
}

// NewRegistry creates an empty tool registry.
func NewRegistry() *Registry {
	return &Registry{
		tools: make(map[string]ToolPlugin),
	}
}

// Register adds a tool plugin. Returns an error if a tool with the same name
// is already registered.
func (r *Registry) Register(plugin ToolPlugin) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	name := plugin.Name()
	if _, exists := r.tools[name]; exists {
		return fmt.Errorf("tool %q already registered", name)
	}
	r.tools[name] = plugin
	return nil
}

// Get retrieves a registered tool by name. Returns nil if not found.
func (r *Registry) Get(name string) ToolPlugin {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.tools[name]
}

// List returns all registered tool names.
func (r *Registry) List() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()

	names := make([]string, 0, len(r.tools))
	for n := range r.tools {
		names = append(names, n)
	}
	return names
}

// Execute dispatches an action to the named tool plugin.
// It validates the action before execution and truncates output to OutputMaxBytes.
func (r *Registry) Execute(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	plugin := r.Get(action.Tool)
	if plugin == nil {
		return nil, fmt.Errorf("tool %q not registered", action.Tool)
	}

	if err := plugin.Validate(action); err != nil {
		return nil, fmt.Errorf("validate %q action: %w", action.Tool, err)
	}

	result, err := plugin.Execute(ctx, action)
	if err != nil {
		return nil, err
	}

	if result != nil {
		result.Stdout = Truncate(result.Stdout, OutputMaxBytes)
		result.Stderr = Truncate(result.Stderr, OutputMaxBytes)
	}

	return result, nil
}
