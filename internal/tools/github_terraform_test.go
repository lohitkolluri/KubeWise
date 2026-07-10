package tools

import (
	"context"
	"testing"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// --- GitHubPlugin tests ---

func TestGitHubPlugin_Name(t *testing.T) {
	p := NewGitHubPlugin("", "")
	if got := p.Name(); got != "github" {
		t.Errorf("Name() = %q, want %q", got, "github")
	}
}

func TestGitHubPlugin_Capabilities(t *testing.T) {
	p := NewGitHubPlugin("", "")
	caps := p.Capabilities()
	if len(caps) == 0 {
		t.Fatal("Capabilities() returned empty")
	}
	hasRead := false
	hasWrite := false
	hasDestructive := false
	hasApproval := false
	for _, c := range caps {
		switch c {
		case models.CapRead:
			hasRead = true
		case models.CapWrite:
			hasWrite = true
		case models.CapDestructive:
			hasDestructive = true
		case models.CapRequiresApproval:
			hasApproval = true
		}
	}
	if !hasRead {
		t.Error("Capabilities() missing CapRead")
	}
	if !hasWrite {
		t.Error("Capabilities() missing CapWrite")
	}
	if !hasDestructive {
		t.Error("Capabilities() missing CapDestructive")
	}
	if !hasApproval {
		t.Error("Capabilities() missing CapRequiresApproval")
	}
}

func TestGitHubPlugin_Validate_AllowsReadCommands(t *testing.T) {
	p := NewGitHubPlugin("", "")
	tests := []struct {
		cmd  string
		args map[string]string
	}{
		{"get repo", map[string]string{"owner": "my-org", "repo": "my-repo"}},
		{"get file", map[string]string{"owner": "my-org", "repo": "my-repo", "path": "README.md"}},
		{"get file", map[string]string{"owner": "my-org", "repo": "my-repo", "path": "src/main.go", "ref": "main"}},
		{"get pr", map[string]string{"owner": "my-org", "repo": "my-repo", "pr": "42"}},
		{"list prs", map[string]string{"owner": "my-org", "repo": "my-repo"}},
		{"list prs", map[string]string{"owner": "my-org", "repo": "my-repo", "state": "closed"}},
		{"search prs", map[string]string{"query": "bug+repo:my-org/my-repo"}},
		{"get issue", map[string]string{"owner": "my-org", "repo": "my-repo", "issue": "7"}},
		{"list issues", map[string]string{"owner": "my-org", "repo": "my-repo"}},
		{"list repos", map[string]string{"owner": "my-org"}},
	}
	for _, tt := range tests {
		t.Run(tt.cmd, func(t *testing.T) {
			action := models.ToolAction{Command: tt.cmd, Args: tt.args}
			if err := p.Validate(action); err != nil {
				t.Errorf("Validate() unexpected error: %v", err)
			}
		})
	}
}

func TestGitHubPlugin_Validate_AllowsWriteCommands(t *testing.T) {
	p := NewGitHubPlugin("", "")
	tests := []struct {
		cmd  string
		args map[string]string
	}{
		{"create pr", map[string]string{"owner": "my-org", "repo": "my-repo", "title": "Fix bug", "head": "fix-branch", "base": "main"}},
		{"merge pr", map[string]string{"owner": "my-org", "repo": "my-repo", "pr": "42"}},
		{"close pr", map[string]string{"owner": "my-org", "repo": "my-repo", "pr": "42"}},
		{"comment pr", map[string]string{"owner": "my-org", "repo": "my-repo", "pr": "42", "body": "LGTM"}},
		{"close issue", map[string]string{"owner": "my-org", "repo": "my-repo", "issue": "7"}},
	}
	for _, tt := range tests {
		t.Run(tt.cmd, func(t *testing.T) {
			action := models.ToolAction{Command: tt.cmd, Args: tt.args}
			if err := p.Validate(action); err != nil {
				t.Errorf("Validate() unexpected error: %v", err)
			}
		})
	}
}

func TestGitHubPlugin_Validate_BlocksBlockedCommands(t *testing.T) {
	p := NewGitHubPlugin("", "")
	blocked := []string{"delete repo", "transfer repo", "push", "force push", "delete branch", "create token", "add collaborator"}
	for _, cmd := range blocked {
		t.Run(cmd, func(t *testing.T) {
			action := models.ToolAction{Command: cmd}
			if err := p.Validate(action); err == nil {
				t.Error("Validate() expected error, got nil")
			}
		})
	}
}

func TestGitHubPlugin_Validate_RejectsInvalidOwner(t *testing.T) {
	p := NewGitHubPlugin("", "")
	action := models.ToolAction{
		Command: "list repos",
		Args:    map[string]string{"owner": "../etc"},
	}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for invalid owner, got nil")
	}
}

func TestGitHubPlugin_Validate_RejectsInvalidRepo(t *testing.T) {
	p := NewGitHubPlugin("", "")
	action := models.ToolAction{
		Command: "get repo",
		Args:    map[string]string{"owner": "valid", "repo": "../etc"},
	}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for invalid repo, got nil")
	}
}

func TestGitHubPlugin_Validate_RejectsInvalidPRNumber(t *testing.T) {
	p := NewGitHubPlugin("", "")
	tests := []string{"0", "-1", "abc"}
	for _, pr := range tests {
		t.Run(pr, func(t *testing.T) {
			action := models.ToolAction{
				Command: "get pr",
				Args:    map[string]string{"owner": "o", "repo": "r", "pr": pr},
			}
			if err := p.Validate(action); err == nil {
				t.Errorf("Validate() expected error for pr %q, got nil", pr)
			}
		})
	}
}

func TestGitHubPlugin_Validate_RejectsInvalidIssueNumber(t *testing.T) {
	p := NewGitHubPlugin("", "")
	tests := []string{"0", "-1", "abc"}
	for _, issue := range tests {
		t.Run(issue, func(t *testing.T) {
			action := models.ToolAction{
				Command: "get issue",
				Args:    map[string]string{"owner": "o", "repo": "r", "issue": issue},
			}
			if err := p.Validate(action); err == nil {
				t.Errorf("Validate() expected error for issue %q, got nil", issue)
			}
		})
	}
}

func TestGitHubPlugin_Validate_RejectsInvalidFilePath(t *testing.T) {
	p := NewGitHubPlugin("", "")
	action := models.ToolAction{
		Command: "get file",
		Args:    map[string]string{"owner": "o", "repo": "r", "path": "../etc/passwd"},
	}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for path with .., got nil")
	}
}

func TestGitHubPlugin_Validate_RejectsUnknownCommand(t *testing.T) {
	p := NewGitHubPlugin("", "")
	action := models.ToolAction{Command: "unknown"}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for unknown command, got nil")
	}
}

func TestGitHubPlugin_Execute_RequiresToken(_ *testing.T) {
	p := NewGitHubPlugin("", "")
	// Without a token, the API call should fail (not crash)
	action := models.ToolAction{
		Command: "get repo",
		Args:    map[string]string{"owner": "some-org", "repo": "some-repo"},
	}
	_, _ = p.Execute(context.Background(), action)
	// Execute without token is expected to fail or return success=false.
	// This test only verifies no crash; actual auth behavior is tested
	// by integration tests.
}

// --- TerraformPlugin tests ---

func TestTerraformPlugin_Name(t *testing.T) {
	p := NewTerraformPlugin("")
	if got := p.Name(); got != "terraform" {
		t.Errorf("Name() = %q, want %q", got, "terraform")
	}
}

func TestTerraformPlugin_Capabilities(t *testing.T) {
	p := NewTerraformPlugin("")
	caps := p.Capabilities()
	if len(caps) == 0 {
		t.Fatal("Capabilities() returned empty")
	}
	hasRead := false
	hasWrite := false
	hasDestructive := false
	hasApproval := false
	for _, c := range caps {
		switch c {
		case models.CapRead:
			hasRead = true
		case models.CapWrite:
			hasWrite = true
		case models.CapDestructive:
			hasDestructive = true
		case models.CapRequiresApproval:
			hasApproval = true
		}
	}
	if !hasRead {
		t.Error("Capabilities() missing CapRead")
	}
	if !hasWrite {
		t.Error("Capabilities() missing CapWrite")
	}
	if !hasDestructive {
		t.Error("Capabilities() missing CapDestructive")
	}
	if !hasApproval {
		t.Error("Capabilities() missing CapRequiresApproval")
	}
}

func TestTerraformPlugin_Validate_AllowsReadCommands(t *testing.T) {
	p := NewTerraformPlugin("")
	tests := []struct {
		cmd  string
		args map[string]string
	}{
		{"validate", map[string]string{}},
		{"plan", map[string]string{}},
		{"plan", map[string]string{"out": "plan.tfplan", "target": "module.foo"}},
		{"show", map[string]string{}},
		{"show", map[string]string{"plan": "plan.tfplan"}},
		{"output", map[string]string{}},
		{"output", map[string]string{"name": "instance_ip"}},
		{"version", map[string]string{}},
		{"state list", map[string]string{}},
		{"state show", map[string]string{"address": "module.foo"}},
		{"workspace list", map[string]string{}},
		{"workspace show", map[string]string{}},
	}
	for _, tt := range tests {
		t.Run(tt.cmd, func(t *testing.T) {
			action := models.ToolAction{Command: tt.cmd, Args: tt.args}
			if err := p.Validate(action); err != nil {
				t.Errorf("Validate() unexpected error: %v", err)
			}
		})
	}
}

func TestTerraformPlugin_Validate_AllowsWriteCommands(t *testing.T) {
	t.Setenv("KUBEWISE_ALLOW_TERRAFORM_INIT", "true")
	p := NewTerraformPlugin("")
	tests := []struct {
		cmd  string
		args map[string]string
	}{
		{"init", map[string]string{}},
		{"fmt", map[string]string{}},
		{"fmt", map[string]string{"dir": ".", "recursive": "true"}},
		{"refresh", map[string]string{}},
	}
	for _, tt := range tests {
		t.Run(tt.cmd, func(t *testing.T) {
			action := models.ToolAction{Command: tt.cmd, Args: tt.args}
			if err := p.Validate(action); err != nil {
				t.Errorf("Validate() unexpected error: %v", err)
			}
		})
	}
}

func TestTerraformPlugin_Validate_AllowsApplyOnlyWithApproval(t *testing.T) {
	p := NewTerraformPlugin("")
	// apply and destroy are allowed but require approval (tested via capability)
	tests := []string{"apply", "destroy"}
	for _, cmd := range tests {
		t.Run(cmd, func(t *testing.T) {
			action := models.ToolAction{Command: cmd, Args: map[string]string{}}
			if err := p.Validate(action); err != nil {
				t.Errorf("Validate() expected no error for %q (gated via approval), got: %v", cmd, err)
			}
		})
	}
}

func TestTerraformPlugin_Validate_BlocksBlockedCommands(t *testing.T) {
	p := NewTerraformPlugin("")
	blocked := []string{"import", "taint", "untaint", "force-unlock", "console", "workspace new", "workspace delete"}
	for _, cmd := range blocked {
		t.Run(cmd, func(t *testing.T) {
			action := models.ToolAction{Command: cmd}
			if err := p.Validate(action); err == nil {
				t.Error("Validate() expected error, got nil")
			}
		})
	}
}

func TestTerraformPlugin_Validate_RejectsInvalidDir(t *testing.T) {
	p := NewTerraformPlugin("")
	action := models.ToolAction{
		Command: "init",
		Args:    map[string]string{"dir": "../etc"},
	}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for invalid dir, got nil")
	}
}

func TestTerraformPlugin_Validate_RejectsInvalidPlanFile(t *testing.T) {
	p := NewTerraformPlugin("")
	tests := []string{"plan.out", "a.b.c", "/etc/passwd"}
	for _, plan := range tests {
		t.Run(plan, func(t *testing.T) {
			action := models.ToolAction{
				Command: "apply",
				Args:    map[string]string{"plan": plan},
			}
			if err := p.Validate(action); err == nil {
				t.Errorf("Validate() expected error for plan %q, got nil", plan)
			}
		})
	}
}

func TestTerraformPlugin_Validate_RejectsInvalidWorkspace(t *testing.T) {
	p := NewTerraformPlugin("")
	action := models.ToolAction{
		Command: "workspace show",
		Args:    map[string]string{"workspace": "../etc"},
	}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for invalid workspace, got nil")
	}
}

func TestTerraformPlugin_Validate_RejectsUnknownCommand(t *testing.T) {
	p := NewTerraformPlugin("")
	action := models.ToolAction{Command: "unknown"}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for unknown command, got nil")
	}
}

func TestTerraformPlugin_buildArgs(t *testing.T) {
	p := NewTerraformPlugin("/infra")
	tests := []struct {
		name   string
		action models.ToolAction
		want   []string
	}{
		{
			name:   "init in working dir",
			action: models.ToolAction{Command: "init"},
			want:   []string{"init", "-chdir=/infra"},
		},
		{
			name:   "plan with out",
			action: models.ToolAction{Command: "plan", Args: map[string]string{"out": "plan.tfplan"}},
			want:   []string{"plan", "-chdir=/infra", "-out=plan.tfplan"},
		},
		{
			name:   "plan with target",
			action: models.ToolAction{Command: "plan", Args: map[string]string{"target": "module.foo"}},
			want:   []string{"plan", "-chdir=/infra", "-target=module.foo"},
		},
		{
			name:   "apply with plan file",
			action: models.ToolAction{Command: "apply", Args: map[string]string{"plan": "plan.tfplan"}},
			want:   []string{"apply", "-chdir=/infra", "plan.tfplan"},
		},
		{
			name:   "destroy with auto-approve and target",
			action: models.ToolAction{Command: "destroy", Args: map[string]string{"auto-approve": "true", "target": "module.bar"}},
			want:   []string{"destroy", "-chdir=/infra", "-auto-approve", "-target=module.bar"},
		},
		{
			name:   "validate (no extra args)",
			action: models.ToolAction{Command: "validate"},
			want:   []string{"validate", "-chdir=/infra"},
		},
		{
			name:   "show with plan file",
			action: models.ToolAction{Command: "show", Args: map[string]string{"plan": "plan.tfplan"}},
			want:   []string{"show", "-chdir=/infra", "plan.tfplan"},
		},
		{
			name:   "fmt recursive",
			action: models.ToolAction{Command: "fmt", Args: map[string]string{"dir": "modules", "recursive": "true"}},
			want:   []string{"fmt", "-chdir=/infra", "modules", "-recursive"},
		},
		{
			name:   "state list",
			action: models.ToolAction{Command: "state list"},
			want:   []string{"state", "list", "-chdir=/infra"},
		},
		{
			name:   "output with name",
			action: models.ToolAction{Command: "output", Args: map[string]string{"name": "instance_ip"}},
			want:   []string{"output", "-chdir=/infra", "instance_ip"},
		},
		{
			name:   "version",
			action: models.ToolAction{Command: "version"},
			want:   []string{"version", "-chdir=/infra"},
		},
		{
			name:   "refresh with target",
			action: models.ToolAction{Command: "refresh", Args: map[string]string{"target": "module.foo"}},
			want:   []string{"refresh", "-chdir=/infra", "-target=module.foo"},
		},
		{
			name:   "plan with vars",
			action: models.ToolAction{Command: "plan", Args: map[string]string{"vars": "instance_count=3"}},
			want:   []string{"plan", "-chdir=/infra", "-var", "instance_count=3"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := p.buildArgs(tt.action)
			if len(got) != len(tt.want) {
				t.Errorf("buildArgs() len = %d, want %d\n  got:  %v\n  want: %v", len(got), len(tt.want), got, tt.want)
				return
			}
			for i := range got {
				if got[i] != tt.want[i] {
					t.Errorf("buildArgs() arg[%d] = %q, want %q\n  got:  %v\n  want: %v", i, got[i], tt.want[i], got, tt.want)
				}
			}
		})
	}
}

func TestTerraformPlugin_buildArgs_NoWorkDir(t *testing.T) {
	p := NewTerraformPlugin("")
	action := models.ToolAction{Command: "validate"}
	args := p.buildArgs(action)
	want := []string{"validate"}
	if len(args) != len(want) {
		t.Errorf("buildArgs() len = %d, want %d\n  got:  %v\n  want: %v", len(args), len(want), args, want)
	}
}
