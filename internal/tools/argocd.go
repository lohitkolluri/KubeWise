package tools

import (
	"context"
	"fmt"
	"os"
	"regexp"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// argocdCommand describes an argocd subcommand allowed for use.
type argocdCommand struct {
	capabilities []models.ToolCapability
	readOnly     bool
}

// allowedArgoCDSubcommands and their capabilities.
var allowedArgoCDSubcommands = map[string]argocdCommand{
	"app list":           {[]models.ToolCapability{models.CapRead}, true},
	"app get":            {[]models.ToolCapability{models.CapRead}, true},
	"app diff":           {[]models.ToolCapability{models.CapRead}, true},
	"app history":        {[]models.ToolCapability{models.CapRead}, true},
	"app resources":      {[]models.ToolCapability{models.CapRead}, true},
	"app resources tree": {[]models.ToolCapability{models.CapRead}, true},
	"app sync":           {[]models.ToolCapability{models.CapWrite, models.CapRequiresApproval}, false},
	"app rollback":       {[]models.ToolCapability{models.CapWrite, models.CapDestructive, models.CapRequiresApproval}, false},
	"app set":            {[]models.ToolCapability{models.CapWrite, models.CapRequiresApproval}, false},
	"app wait":           {[]models.ToolCapability{models.CapRead}, true},
	"proj list":          {[]models.ToolCapability{models.CapRead}, true},
	"proj get":           {[]models.ToolCapability{models.CapRead}, true},
	"cluster list":       {[]models.ToolCapability{models.CapRead}, true},
	"cluster get":        {[]models.ToolCapability{models.CapRead}, true},
	"repo list":          {[]models.ToolCapability{models.CapRead}, true},
	"repo get":           {[]models.ToolCapability{models.CapRead}, true},
}

// blockedArgoCDSubcommands are explicitly rejected for security reasons.
var blockedArgoCDSubcommands = map[string]bool{
	"login":                   true,
	"logout":                  true,
	"account":                 true,
	"account update-password": true,
	"cluster add":             true,
	"cluster remove":          true,
	"repocreds":               true,
	"cert":                    true,
	"gpg":                     true,
	"proj create":             true,
	"proj delete":             true,
	"proj update":             true,
	"app create":              true,
	"app delete":              true,
	"app unset":               true,
	"admin":                   true,
	"db":                      true,
	"dex":                     true,
	// app set can change desired state; require explicit opt-in via env.
	"app set": true,
}

var (
	validArgoAppName  = regexp.MustCompile(`^[a-zA-Z0-9]([a-zA-Z0-9\-_.]*[a-zA-Z0-9])?$`)
	validArgoProject  = regexp.MustCompile(`^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?$`)
	validArgoRevision = regexp.MustCompile(`^[1-9][0-9]*$`)
)

// ArgoCDPlugin wraps argocd binary calls with command validation and output
// truncation. It implements the ToolPlugin interface.
type ArgoCDPlugin struct {
	// server is the ArgoCD API server address.
	server string
	// authToken is the ArgoCD authentication token.
	authToken string
	// grpcWeb enables gRPC-web for environments without HTTP/2.
	grpcWeb bool
	// binaryPath is the argocd binary (default: "argocd").
	binaryPath string
}

// NewArgoCDPlugin creates a new ArgoCDPlugin with the given server and
// optional auth token. Pass an empty string for server to use the default
// configured in the argocd CLI context.
func NewArgoCDPlugin(server, authToken string, grpcWeb bool) *ArgoCDPlugin {
	return &ArgoCDPlugin{
		server:     server,
		authToken:  authToken,
		grpcWeb:    grpcWeb,
		binaryPath: "argocd",
	}
}

// Name returns the canonical tool name.
func (p *ArgoCDPlugin) Name() string { return "argocd" }

// Capabilities returns the set of capabilities this tool supports.
func (p *ArgoCDPlugin) Capabilities() []models.ToolCapability {
	return []models.ToolCapability{models.CapRead, models.CapWrite, models.CapDestructive, models.CapRequiresApproval}
}

// Validate checks whether the action contains a valid, allowed argocd command.
func (p *ArgoCDPlugin) Validate(action models.ToolAction) error {
	cmd := action.Command

	if blockedArgoCDSubcommands[cmd] {
		if cmd == "app set" && os.Getenv("KUBEWISE_ALLOW_ARGOCD_APP_SET") == "true" {
			// allowed explicitly
		} else {
			return fmt.Errorf("argocd %q is blocked for security reasons", cmd)
		}
	}

	if _, found := allowedArgoCDSubcommands[cmd]; !found {
		return fmt.Errorf("argocd %q is not in the allowed command list", cmd)
	}

	if app := action.Args["app"]; app != "" {
		if !validArgoAppName.MatchString(app) {
			return fmt.Errorf("invalid argocd app name: %q", app)
		}
	}

	if proj := action.Args["project"]; proj != "" {
		if !validArgoProject.MatchString(proj) {
			return fmt.Errorf("invalid argocd project: %q", proj)
		}
	}

	if rev := action.Args["revision"]; rev != "" {
		if !validArgoRevision.MatchString(rev) {
			return fmt.Errorf("invalid revision: %q", rev)
		}
	}

	return nil
}

// Execute runs the argocd command with the given action. It validates the
// action first, constructs the argument list, and delegates to RunCommand.
func (p *ArgoCDPlugin) Execute(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	if err := p.Validate(action); err != nil {
		return nil, err
	}

	args := p.buildArgs(action)
	timeout := p.resolveTimeout(action.Timeout)

	return RunCommand(ctx, p.binaryPath, args, timeout), nil
}

// buildArgs constructs the argocd argument list from the action.
func (p *ArgoCDPlugin) buildArgs(action models.ToolAction) []string {
	var args []string

	if p.server != "" {
		args = append(args, "--server", p.server)
	}
	if p.authToken != "" {
		args = append(args, "--auth-token", p.authToken)
	}
	if p.grpcWeb {
		args = append(args, "--grpc-web")
	}

	// Compound commands like "app list", "app get"
	args = append(args, splitOnSpace(action.Command)...)

	// Add app name for app-specific commands
	if app := action.Args["app"]; app != "" {
		args = append(args, app)
	}

	// Add project name for project-specific commands
	if proj := action.Args["project"]; proj != "" {
		args = append(args, proj)
	}

	// Add remaining args as --key=value or --key value
	for key, value := range action.Args {
		switch key {
		case "app", "project":
			continue
		default:
			if value != "" {
				args = append(args, "--"+key, value)
			}
		}
	}

	return args
}

// resolveTimeout returns the action timeout, defaulting to 60s for argocd
// (sync/rollback may take time).
func (p *ArgoCDPlugin) resolveTimeout(dv models.DurationValue) time.Duration {
	if dur := time.Duration(dv.Duration); dur > 0 {
		return dur
	}
	return 60 * time.Second
}
