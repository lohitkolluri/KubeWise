// Package tools provides convenience helpers for tool plugin execution.
//
// RunCommand is a reusable shell-out helper that any tool plugin can use
// to run external commands with timeout, output capture, and truncation.
package tools

import (
	"context"
	"os/exec"
	"strings"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// RunCommand is a convenience helper that runs a shell command, captures
// stdout/stderr, and returns a ToolResult. It applies a configurable timeout
// and truncates output to OutputMaxBytes.
//
// This is intended for simple tools that execute a single external command.
// Complex tools (kubectl streaming, multi-step helm install) should implement
// ToolPlugin.Execute directly.
func RunCommand(ctx context.Context, command string, args []string, timeout time.Duration) *models.ToolResult {
	start := time.Now()

	if timeout <= 0 {
		timeout = 30 * time.Second
	}

	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, command, args...) //nolint:gosec // generic command runner for tool plugins

	stdout := new(strings.Builder)
	stderr := new(strings.Builder)
	cmd.Stdout = stdout
	cmd.Stderr = stderr

	err := cmd.Run()

	duration := time.Since(start)
	result := &models.ToolResult{
		Stdout:   Truncate(stdout.String(), OutputMaxBytes),
		Stderr:   Truncate(stderr.String(), OutputMaxBytes),
		Duration: duration,
		Success:  err == nil,
	}
	if cmd.ProcessState != nil {
		result.ExitCode = cmd.ProcessState.ExitCode()
	}

	if ctx.Err() == context.DeadlineExceeded {
		result.Stderr = Truncate(result.Stderr+"\n[timeout after "+timeout.String()+"]", OutputMaxBytes)
	}

	return result
}
