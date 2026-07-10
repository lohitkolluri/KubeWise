package tools

import (
	"context"
	"fmt"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// terraformCommand describes a terraform subcommand allowed for use.
type terraformCommand struct {
	capabilities []models.ToolCapability
	readOnly     bool
}

// allowedTerraformSubcommands and their capabilities.
var allowedTerraformSubcommands = map[string]terraformCommand{
	"init":           {[]models.ToolCapability{models.CapWrite, models.CapRequiresApproval}, false},
	"validate":       {[]models.ToolCapability{models.CapRead}, true},
	"plan":           {[]models.ToolCapability{models.CapRead}, true},
	"show":           {[]models.ToolCapability{models.CapRead}, true},
	"output":         {[]models.ToolCapability{models.CapRead}, true},
	"fmt":            {[]models.ToolCapability{models.CapWrite}, false},
	"version":        {[]models.ToolCapability{models.CapRead}, true},
	"apply":          {[]models.ToolCapability{models.CapWrite, models.CapDestructive, models.CapRequiresApproval}, false},
	"destroy":        {[]models.ToolCapability{models.CapWrite, models.CapDestructive, models.CapRequiresApproval}, false},
	"refresh":        {[]models.ToolCapability{models.CapWrite}, false},
	"state list":     {[]models.ToolCapability{models.CapRead}, true},
	"state show":     {[]models.ToolCapability{models.CapRead}, true},
	"workspace list": {[]models.ToolCapability{models.CapRead}, true},
	"workspace show": {[]models.ToolCapability{models.CapRead}, true},
}

// blockedTerraformSubcommands are explicitly rejected for security reasons.
var blockedTerraformSubcommands = map[string]bool{
	"import":           true,
	"taint":            true,
	"untaint":          true,
	"force-unlock":     true,
	"state rm":         true,
	"state mv":         true,
	"workspace new":    true,
	"workspace delete": true,
	"console":          true,
	// init can download providers/modules; require explicit opt-in via env.
	"init": true,
}

var (
	validTerraformDir  = regexp.MustCompile(`^[a-zA-Z0-9_\-./]+$`)
	validPlanFile      = regexp.MustCompile(`^[a-zA-Z0-9_\-./]+\.tfplan$`)
	validWorkspaceName = regexp.MustCompile(`^[a-zA-Z0-9_\-.]+$`)
	validTargetAddress = regexp.MustCompile(`^[a-zA-Z0-9_]+(\.[a-zA-Z0-9_]+)*$`)
)

// TerraformPlugin wraps terraform binary calls with command validation.
// It implements the ToolPlugin interface.
//
// Apply and destroy are gated behind CapRequiresApproval and are dry-run
// only by default. The plan produces output that a human applies externally.
type TerraformPlugin struct {
	// workingDir is the terraform configuration directory.
	workingDir string
	// binaryPath is the terraform binary (default: "terraform").
	binaryPath string
}

// NewTerraformPlugin creates a new TerraformPlugin with the given working
// directory. Pass an empty string to use the current directory.
func NewTerraformPlugin(workingDir string) *TerraformPlugin {
	return &TerraformPlugin{
		workingDir: workingDir,
		binaryPath: "terraform",
	}
}

// Name returns the canonical tool name.
func (p *TerraformPlugin) Name() string { return "terraform" }

// Capabilities returns the set of capabilities this tool supports.
func (p *TerraformPlugin) Capabilities() []models.ToolCapability {
	return []models.ToolCapability{models.CapRead, models.CapWrite, models.CapDestructive, models.CapRequiresApproval}
}

// Validate checks whether the action contains a valid, allowed terraform
// command with properly formatted parameters.
func (p *TerraformPlugin) Validate(action models.ToolAction) error {
	cmd := action.Command

	if blockedTerraformSubcommands[cmd] && (cmd != "init" || !strings.EqualFold(strings.TrimSpace(os.Getenv("KUBEWISE_ALLOW_TERRAFORM_INIT")), "true")) {
		return fmt.Errorf("terraform %q is blocked for security reasons", cmd)
	}

	if _, found := allowedTerraformSubcommands[cmd]; !found {
		return fmt.Errorf("terraform %q is not in the allowed command list", cmd)
	}

	if dir := action.Args["dir"]; dir != "" {
		if strings.Contains(dir, "..") {
			return fmt.Errorf("invalid directory: %q", dir)
		}
		if !validTerraformDir.MatchString(dir) {
			return fmt.Errorf("invalid directory: %q", dir)
		}
	}

	if plan := action.Args["plan"]; plan != "" {
		if strings.Contains(plan, "..") {
			return fmt.Errorf("invalid plan file: %q", plan)
		}
		if !validPlanFile.MatchString(plan) {
			return fmt.Errorf("invalid plan file: %q", plan)
		}
	}

	if ws := action.Args["workspace"]; ws != "" {
		if !validWorkspaceName.MatchString(ws) {
			return fmt.Errorf("invalid workspace name: %q", ws)
		}
	}

	if target := action.Args["target"]; target != "" {
		if !validTargetAddress.MatchString(target) {
			return fmt.Errorf("invalid target address: %q", target)
		}
	}

	return nil
}

// Execute runs the terraform command with the given action. It validates the
// action first, constructs the argument list, and delegates to RunCommand.
func (p *TerraformPlugin) Execute(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	if err := p.Validate(action); err != nil {
		return nil, err
	}

	args := p.buildArgs(action)
	timeout := p.resolveTimeout(action.Timeout)

	return RunCommand(ctx, p.binaryPath, args, timeout), nil
}

// buildArgs constructs the terraform argument list from the action.
func (p *TerraformPlugin) buildArgs(action models.ToolAction) []string {
	var args []string

	// Add compound command parts (e.g. "state list" -> "state", "list")
	args = append(args, splitOnSpace(action.Command)...)

	// Add -chdir for non-current working directory
	if p.workingDir != "" {
		args = append(args, "-chdir="+p.workingDir)
	}

	// Add positional args based on command
	switch action.Command {
	case "init":
		if dir := action.Args["dir"]; dir != "" {
			args = append(args, dir)
		}
	case "plan":
		if out := action.Args["out"]; out != "" {
			args = append(args, "-out="+out)
		}
		if target := action.Args["target"]; target != "" {
			args = append(args, "-target="+target)
		}
	case "apply":
		if plan := action.Args["plan"]; plan != "" {
			args = append(args, plan)
		}
		if autoApprove := action.Args["auto-approve"]; autoApprove == "true" {
			args = append(args, "-auto-approve")
		}
	case "destroy":
		if autoApprove := action.Args["auto-approve"]; autoApprove == "true" {
			args = append(args, "-auto-approve")
		}
		if target := action.Args["target"]; target != "" {
			args = append(args, "-target="+target)
		}
	case "show":
		if plan := action.Args["plan"]; plan != "" {
			args = append(args, plan)
		}
	case "fmt":
		if dir := action.Args["dir"]; dir != "" {
			args = append(args, dir)
		}
		if action.Args["recursive"] == "true" {
			args = append(args, "-recursive")
		}
	case "output":
		if name := action.Args["name"]; name != "" {
			args = append(args, name)
		}
	case "refresh":
		if target := action.Args["target"]; target != "" {
			args = append(args, "-target="+target)
		}
	}

	// Add remaining args as -var key=value
	for key, value := range action.Args {
		switch key {
		case "dir", "out", "target", "plan", "name", "workspace", "recursive", "auto-approve":
			continue
		case "vars":
			args = append(args, "-var", value)
		default:
			if value != "" {
				args = append(args, "-"+key, value)
			}
		}
	}

	return args
}

// resolveTimeout returns the action timeout, defaulting to 120s for terraform
// (operations like init and apply may take significant time).
func (p *TerraformPlugin) resolveTimeout(dv models.DurationValue) time.Duration {
	if dur := time.Duration(dv.Duration); dur > 0 {
		return dur
	}
	return 120 * time.Second
}
