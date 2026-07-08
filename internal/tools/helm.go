package tools

import (
	"context"
	"fmt"
	"regexp"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// helmCommand describes a helm subcommand allowed for use.
type helmCommand struct {
	capabilities []models.ToolCapability
	readOnly     bool
}

// allowedHelmSubcommands and their capabilities.
var allowedHelmSubcommands = map[string]helmCommand{
	"list":         {[]models.ToolCapability{models.CapRead}, true},
	"status":       {[]models.ToolCapability{models.CapRead}, true},
	"history":      {[]models.ToolCapability{models.CapRead}, true},
	"get":          {[]models.ToolCapability{models.CapRead}, true},
	"get values":   {[]models.ToolCapability{models.CapRead}, true},
	"get manifest": {[]models.ToolCapability{models.CapRead}, true},
	"get notes":    {[]models.ToolCapability{models.CapRead}, true},
	"get hooks":    {[]models.ToolCapability{models.CapRead}, true},
	"show":         {[]models.ToolCapability{models.CapRead}, true},
	"show chart":   {[]models.ToolCapability{models.CapRead}, true},
	"show values":  {[]models.ToolCapability{models.CapRead}, true},
	"show all":     {[]models.ToolCapability{models.CapRead}, true},
	"upgrade":      {[]models.ToolCapability{models.CapWrite, models.CapRequiresApproval}, false},
	"rollback":     {[]models.ToolCapability{models.CapWrite, models.CapDestructive, models.CapRequiresApproval}, false},
	"uninstall":    {[]models.ToolCapability{models.CapWrite, models.CapDestructive, models.CapRequiresApproval}, false},
}

// blockedHelmSubcommands are explicitly rejected.
var blockedHelmSubcommands = map[string]bool{
	"install":     true,
	"create":      true,
	"package":     true,
	"dependency":  true,
	"repo add":    true,
	"repo remove": true,
	"repo update": true,
	"plugin":      true,
	"test":        true,
	"template":    true, // local rendering only, no cluster impact — low risk but no use case
}

var (
	validHelmRelease  = regexp.MustCompile(`^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$`)
	validHelmChart    = regexp.MustCompile(`^([a-zA-Z][a-zA-Z0-9+.-]*://)?[a-zA-Z0-9]([a-zA-Z0-9\-_.\/]*[a-zA-Z0-9])?$`)
	validHelmRevision = regexp.MustCompile(`^[1-9][0-9]*$`)
)

// HelmPlugin wraps helm binary calls with command validation and output
// truncation. It implements the ToolPlugin interface.
type HelmPlugin struct {
	// kubeconfig is an optional path to a kubeconfig file.
	kubeconfig string
	// kubeContext is an optional kube-context override.
	kubeContext string
	// binaryPath is the helm binary (default: "helm").
	binaryPath string
}

// NewHelmPlugin creates a new HelmPlugin with an optional kubeconfig path and
// kube-context. Pass empty strings for default behaviour.
func NewHelmPlugin(kubeconfig, kubeContext string) *HelmPlugin {
	return &HelmPlugin{
		kubeconfig:  kubeconfig,
		kubeContext: kubeContext,
		binaryPath:  "helm",
	}
}

// Name returns the canonical tool name.
func (p *HelmPlugin) Name() string { return "helm" }

// Capabilities returns the set of capabilities this tool supports.
func (p *HelmPlugin) Capabilities() []models.ToolCapability {
	return []models.ToolCapability{models.CapRead, models.CapWrite, models.CapDestructive, models.CapRequiresApproval}
}

// Validate checks whether the action contains a valid, allowed helm command
// with properly formatted release names. It blocks high-risk commands like
// install, dependency, and repo management.
func (p *HelmPlugin) Validate(action models.ToolAction) error {
	cmd := action.Command

	if blockedHelmSubcommands[cmd] {
		return fmt.Errorf("helm %q is blocked for security reasons", cmd)
	}

	var found bool
	for allowed := range allowedHelmSubcommands {
		if cmd == allowed {
			found = true
			break
		}
		// Allow "get" as prefix for "get values", "get manifest", etc.
		if allowed == "get" && cmd == "get" {
			found = true
		}
	}

	// Check "get <subcommand>" variants
	if !found {
		for allowed := range allowedHelmSubcommands {
			if len(allowed) > 4 && allowed[:4] == "get " && cmd == allowed {
				found = true
				break
			}
			if len(allowed) > 5 && allowed[:5] == "show " && cmd == allowed {
				found = true
				break
			}
		}
	}

	if !found {
		return fmt.Errorf("helm %q is not in the allowed command list", cmd)
	}

	if release := action.Args["release"]; release != "" {
		if !validHelmRelease.MatchString(release) {
			return fmt.Errorf("invalid helm release name: %q", release)
		}
	}

	if ns := action.Args["namespace"]; ns != "" {
		if !validNamespace.MatchString(ns) {
			return fmt.Errorf("invalid namespace: %q", ns)
		}
	}

	if rev := action.Args["revision"]; rev != "" {
		if !validHelmRevision.MatchString(rev) {
			return fmt.Errorf("invalid revision: %q", rev)
		}
	}

	if chart := action.Args["chart"]; chart != "" {
		if !validHelmChart.MatchString(chart) {
			return fmt.Errorf("invalid chart reference: %q", chart)
		}
	}

	return nil
}

// Execute runs the helm command with the given action. It validates the action
// first, constructs the argument list, and delegates to RunCommand.
func (p *HelmPlugin) Execute(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	if err := p.Validate(action); err != nil {
		return nil, err
	}

	args := p.buildArgs(action)
	timeout := p.resolveTimeout(action.Timeout)

	return RunCommand(ctx, p.binaryPath, args, timeout), nil
}

// buildArgs constructs the helm argument list from the action.
func (p *HelmPlugin) buildArgs(action models.ToolAction) []string {
	var args []string

	if p.kubeconfig != "" {
		args = append(args, "--kubeconfig", p.kubeconfig)
	}
	if p.kubeContext != "" {
		args = append(args, "--kube-context", p.kubeContext)
	}

	// For compound commands like "get values", "show chart", split on space
	args = append(args, splitOnSpace(action.Command)...)

	// Add namespace early (for commands that support -n)
	if ns := action.Args["namespace"]; ns != "" {
		args = append(args, "-n", ns)
	}

	// Add positional args based on command
	switch action.Command {
	case "list", "history", "status":
		if release := action.Args["release"]; release != "" {
			args = append(args, release)
		}
	case "upgrade":
		if release := action.Args["release"]; release != "" {
			args = append(args, release)
		}
		if chart := action.Args["chart"]; chart != "" {
			args = append(args, chart)
		}
	case "rollback":
		if release := action.Args["release"]; release != "" {
			args = append(args, release)
		}
		if rev := action.Args["revision"]; rev != "" {
			args = append(args, rev)
		}
	case "uninstall":
		if release := action.Args["release"]; release != "" {
			args = append(args, release)
		}
	default:
		// For "get values", "get manifest", "show chart", etc.
		if release := action.Args["release"]; release != "" {
			args = append(args, release)
		}
		if chart := action.Args["chart"]; chart != "" {
			args = append(args, chart)
		}
	}

	// Add remaining args as --key=value or --key value
	for key, value := range action.Args {
		switch key {
		case "namespace", "release", "chart", "revision":
			continue
		default:
			if value != "" {
				args = append(args, "--"+key, value)
			}
		}
	}

	return args
}

// resolveTimeout returns the action timeout, defaulting to 60s for helm
// (operations like upgrade may take longer than kubectl).
func (p *HelmPlugin) resolveTimeout(dv models.DurationValue) time.Duration {
	if dur := time.Duration(dv.Duration); dur > 0 {
		return dur
	}
	return 60 * time.Second
}

// splitOnSpace splits a string on the first space, returning both parts.
// If there's no space, returns [s].
func splitOnSpace(s string) []string {
	for i := 0; i < len(s); i++ {
		if s[i] == ' ' {
			return []string{s[:i], s[i+1:]}
		}
	}
	return []string{s}
}
