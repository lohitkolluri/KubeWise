package tools

import (
	"context"
	"fmt"
	"regexp"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// allowedCommand describes a kubectl subcommand allowed for use.
type allowedCommand struct {
	capabilities []models.ToolCapability
	readOnly     bool
}

// allowed kubectl subcommands and their capabilities.
var allowedSubcommands = map[string]allowedCommand{
	"get":           {[]models.ToolCapability{models.CapRead}, true},
	"describe":      {[]models.ToolCapability{models.CapRead}, true},
	"logs":          {[]models.ToolCapability{models.CapRead}, true},
	"top":           {[]models.ToolCapability{models.CapRead}, true},
	"api-resources": {[]models.ToolCapability{models.CapRead}, true},
	"api-versions":  {[]models.ToolCapability{models.CapRead}, true},
	"version":       {[]models.ToolCapability{models.CapRead}, true},
	"explain":       {[]models.ToolCapability{models.CapRead}, true},
	"events":        {[]models.ToolCapability{models.CapRead}, true},
	"auth":          {[]models.ToolCapability{models.CapRead}, true},
	"apply":         {[]models.ToolCapability{models.CapWrite}, false},
	"delete":        {[]models.ToolCapability{models.CapWrite, models.CapDestructive}, false},
	"rollout":       {[]models.ToolCapability{models.CapWrite}, false},
	"scale":         {[]models.ToolCapability{models.CapWrite}, false},
	"label":         {[]models.ToolCapability{models.CapWrite}, false},
	"annotate":      {[]models.ToolCapability{models.CapWrite}, false},
	"drain":         {[]models.ToolCapability{models.CapWrite, models.CapDestructive}, false},
	"cordon":        {[]models.ToolCapability{models.CapWrite}, false},
	"uncordon":      {[]models.ToolCapability{models.CapWrite}, false},
	"taint":         {[]models.ToolCapability{models.CapWrite}, false},
	"exec":          {[]models.ToolCapability{models.CapRead}, true},
	"run":           {[]models.ToolCapability{models.CapWrite}, false},
	"cp":            {[]models.ToolCapability{models.CapRead}, true},
	"proxy":         {[]models.ToolCapability{models.CapRead}, true},
	"port-forward":  {[]models.ToolCapability{models.CapRead}, true},
	"attach":        {[]models.ToolCapability{models.CapRead}, true},
}

// blockedSubcommands are explicitly rejected for security reasons — kept empty;
// authorization is delegated to Kubernetes RBAC via the agent's ServiceAccount.
var blockedSubcommands = map[string]bool{}

// validators for resource names to prevent injection.
var (
	validResourceName  = regexp.MustCompile(`^[a-z0-9]([a-z0-9\-\.]*[a-z0-9])?$`)
	validResourceType  = regexp.MustCompile(`^[a-z]+(/[a-z]+)?$`)
	validNamespace     = regexp.MustCompile(`^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$`)
	validLabelSelector = regexp.MustCompile(`^[a-zA-Z0-9_\-.!*'()%/,;:=@]+$`)
)

// KubectlPlugin wraps kubectl binary calls with command validation and output
// truncation. It implements the ToolPlugin interface.
type KubectlPlugin struct {
	// kubeconfig is an optional path to a kubeconfig file.
	kubeconfig string
	// binaryPath is the kubectl binary (default: "kubectl").
	binaryPath string
}

// NewKubectlPlugin creates a new KubectlPlugin with the given kubeconfig path.
// Pass an empty string to use the default kubeconfig.
func NewKubectlPlugin(kubeconfig string) *KubectlPlugin {
	return &KubectlPlugin{
		kubeconfig: kubeconfig,
		binaryPath: "kubectl",
	}
}

// Name returns the canonical tool name.
func (p *KubectlPlugin) Name() string { return "kubectl" }

// Capabilities returns the set of capabilities this tool supports.
func (p *KubectlPlugin) Capabilities() []models.ToolCapability {
	return []models.ToolCapability{models.CapRead, models.CapWrite, models.CapDestructive}
}

// Validate checks whether the action contains a valid, allowed kubectl command
// with properly formatted resource names. It blocks high-risk commands like
// exec, proxy, port-forward, and attach.
func (p *KubectlPlugin) Validate(action models.ToolAction) error {
	cmd := action.Command

	if blockedSubcommands[cmd] {
		return fmt.Errorf("kubectl %q is blocked for security reasons", cmd)
	}

	if _, ok := allowedSubcommands[cmd]; !ok {
		return fmt.Errorf("kubectl %q is not in the allowed command list", cmd)
	}

	if resourceType := action.Args["resource"]; resourceType != "" {
		if !validResourceType.MatchString(resourceType) {
			return fmt.Errorf("invalid resource type: %q", resourceType)
		}
	}

	if name := action.Args["name"]; name != "" {
		if !validResourceName.MatchString(name) {
			return fmt.Errorf("invalid resource name: %q", name)
		}
	}

	if ns := action.Args["namespace"]; ns != "" {
		if action.Command == "apply" {
			// apply can target multiple namespaces; skip validation if it's a
			// comma-separated list
			for _, part := range splitCSV(ns) {
				if !validNamespace.MatchString(part) {
					return fmt.Errorf("invalid namespace: %q", part)
				}
			}
		} else if !validNamespace.MatchString(ns) {
			return fmt.Errorf("invalid namespace: %q", ns)
		}
	}

	if sel := action.Args["selector"]; sel != "" {
		if !validLabelSelector.MatchString(sel) {
			return fmt.Errorf("invalid label selector: %q", sel)
		}
	}

	return nil
}

// Execute runs the kubectl command with the given action. It validates the
// action first, constructs the argument list, and delegates to RunCommand.
func (p *KubectlPlugin) Execute(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	if err := p.Validate(action); err != nil {
		return nil, err
	}

	args := p.buildArgs(action)
	timeout := p.resolveTimeout(action.Timeout)

	return RunCommand(ctx, p.binaryPath, args, timeout), nil
}

// buildArgs constructs the kubectl argument list from the action.
func (p *KubectlPlugin) buildArgs(action models.ToolAction) []string {
	var args []string

	if p.kubeconfig != "" {
		args = append(args, "--kubeconfig", p.kubeconfig)
	}

	args = append(args, action.Command)

	if ns := action.Args["namespace"]; ns != "" {
		if action.Command == "apply" {
			for _, part := range splitCSV(ns) {
				args = append(args, "-n", part)
			}
		} else {
			args = append(args, "-n", ns)
		}
	}

	if resourceType := action.Args["resource"]; resourceType != "" {
		args = append(args, resourceType)
	}

	if name := action.Args["name"]; name != "" {
		args = append(args, name)
	}

	for key, value := range action.Args {
		switch key {
		case "resource", "name", "namespace":
			continue
		default:
			if value != "" {
				args = append(args, "--"+key, value)
			}
		}
	}

	return args
}

// resolveTimeout returns the action timeout as a time.Duration, defaulting to
// 30 seconds if not set.
func (p *KubectlPlugin) resolveTimeout(dv models.DurationValue) time.Duration {
	if dur := time.Duration(dv.Duration); dur > 0 {
		return dur
	}
	return 30 * time.Second
}

// splitCSV splits a comma-separated string, trimming whitespace.
func splitCSV(s string) []string {
	if s == "" {
		return nil
	}
	var parts []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == ',' {
			part := s[start:i]
			start = i + 1
			// Trim leading/trailing whitespace
			part = trimSpace(part)
			if part != "" {
				parts = append(parts, part)
			}
		}
	}
	// Last part
	part := trimSpace(s[start:])
	if part != "" {
		parts = append(parts, part)
	}
	return parts
}

// trimSpace removes leading and trailing whitespace.
func trimSpace(s string) string {
	left, right := 0, len(s)
	for left < right && (s[left] == ' ' || s[left] == '\t') {
		left++
	}
	for right > left && (s[right-1] == ' ' || s[right-1] == '\t') {
		right--
	}
	return s[left:right]
}
