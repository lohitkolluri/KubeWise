package tools

import (
	"testing"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// --- HelmPlugin tests ---

func TestHelmPlugin_Name(t *testing.T) {
	p := NewHelmPlugin("", "")
	if got := p.Name(); got != "helm" {
		t.Errorf("Name() = %q, want %q", got, "helm")
	}
}

func TestHelmPlugin_Capabilities(t *testing.T) {
	p := NewHelmPlugin("", "")
	caps := p.Capabilities()
	if len(caps) == 0 {
		t.Fatal("Capabilities() returned empty")
	}
	hasRead := false
	hasWrite := false
	hasDestructive := false
	for _, c := range caps {
		switch c {
		case models.CapRead:
			hasRead = true
		case models.CapWrite:
			hasWrite = true
		case models.CapDestructive:
			hasDestructive = true
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
}

func TestHelmPlugin_Validate_AllowsReadCommands(t *testing.T) {
	p := NewHelmPlugin("", "")
	tests := []struct {
		cmd  string
		args map[string]string
	}{
		{"list", map[string]string{"namespace": "default"}},
		{"status", map[string]string{"release": "my-app", "namespace": "prod"}},
		{"history", map[string]string{"release": "my-app"}},
		{"get values", map[string]string{"release": "my-app"}},
		{"get manifest", map[string]string{"release": "my-app"}},
		{"get notes", map[string]string{"release": "my-app"}},
		{"get hooks", map[string]string{"release": "my-app"}},
		{"show chart", map[string]string{"chart": "stable/nginx"}},
		{"show values", map[string]string{"chart": "bitnami/redis"}},
		{"show all", map[string]string{"chart": "oci://registry.example.com/chart"}},
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

func TestHelmPlugin_Validate_AllowsWriteCommands(t *testing.T) {
	p := NewHelmPlugin("", "")
	tests := []struct {
		cmd  string
		args map[string]string
	}{
		{"upgrade", map[string]string{"release": "my-app", "chart": "stable/nginx", "namespace": "prod"}},
		{"rollback", map[string]string{"release": "my-app", "revision": "3", "namespace": "prod"}},
		{"uninstall", map[string]string{"release": "my-app", "namespace": "prod"}},
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

func TestHelmPlugin_Validate_BlocksBlockedCommands(t *testing.T) {
	p := NewHelmPlugin("", "")
	blocked := []string{"install", "create", "package", "dependency", "repo add", "repo remove", "repo update", "plugin", "test"}
	for _, cmd := range blocked {
		t.Run(cmd, func(t *testing.T) {
			action := models.ToolAction{Command: cmd}
			if err := p.Validate(action); err == nil {
				t.Error("Validate() expected error, got nil")
			}
		})
	}
}

func TestHelmPlugin_Validate_RejectsInvalidReleaseName(t *testing.T) {
	p := NewHelmPlugin("", "")
	tests := []string{
		"../etc/passwd",
		"; rm -rf /",
		"$(id)",
		"UPPERCASE", // helm release names must be lowercase
	}
	for _, release := range tests {
		t.Run(release, func(t *testing.T) {
			action := models.ToolAction{
				Command: "status",
				Args:    map[string]string{"release": release},
			}
			if err := p.Validate(action); err == nil {
				t.Errorf("Validate() expected error for release %q, got nil", release)
			}
		})
	}
}

func TestHelmPlugin_Validate_RejectsInvalidRevision(t *testing.T) {
	p := NewHelmPlugin("", "")
	tests := []string{"0", "-1", "abc", "1.5"}
	for _, rev := range tests {
		t.Run(rev, func(t *testing.T) {
			action := models.ToolAction{
				Command: "rollback",
				Args:    map[string]string{"release": "my-app", "revision": rev},
			}
			if err := p.Validate(action); err == nil {
				t.Errorf("Validate() expected error for revision %q, got nil", rev)
			}
		})
	}
}

func TestHelmPlugin_Validate_RejectsUnknownCommand(t *testing.T) {
	p := NewHelmPlugin("", "")
	action := models.ToolAction{Command: "unknown"}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for unknown command, got nil")
	}
}

func TestHelmPlugin_buildArgs(t *testing.T) {
	p := NewHelmPlugin("/tmp/kubeconfig", "my-cluster")
	tests := []struct {
		name   string
		action models.ToolAction
		want   []string
	}{
		{
			name:   "list releases in namespace",
			action: models.ToolAction{Command: "list", Args: map[string]string{"namespace": "default"}},
			want:   []string{"--kubeconfig", "/tmp/kubeconfig", "--kube-context", "my-cluster", "list", "-n", "default"},
		},
		{
			name:   "status of a release",
			action: models.ToolAction{Command: "status", Args: map[string]string{"release": "my-app", "namespace": "prod"}},
			want:   []string{"--kubeconfig", "/tmp/kubeconfig", "--kube-context", "my-cluster", "status", "-n", "prod", "my-app"},
		},
		{
			name:   "get values for release",
			action: models.ToolAction{Command: "get values", Args: map[string]string{"release": "my-app"}},
			want:   []string{"--kubeconfig", "/tmp/kubeconfig", "--kube-context", "my-cluster", "get", "values", "my-app"},
		},
		{
			name:   "show chart",
			action: models.ToolAction{Command: "show chart", Args: map[string]string{"chart": "stable/nginx"}},
			want:   []string{"--kubeconfig", "/tmp/kubeconfig", "--kube-context", "my-cluster", "show", "chart", "stable/nginx"},
		},
		{
			name:   "upgrade release",
			action: models.ToolAction{Command: "upgrade", Args: map[string]string{"release": "my-app", "chart": "stable/nginx", "namespace": "prod", "version": "6.0"}},
			want:   []string{"--kubeconfig", "/tmp/kubeconfig", "--kube-context", "my-cluster", "upgrade", "-n", "prod", "my-app", "stable/nginx", "--version", "6.0"},
		},
		{
			name:   "rollback release",
			action: models.ToolAction{Command: "rollback", Args: map[string]string{"release": "my-app", "revision": "3"}},
			want:   []string{"--kubeconfig", "/tmp/kubeconfig", "--kube-context", "my-cluster", "rollback", "my-app", "3"},
		},
		{
			name:   "uninstall release",
			action: models.ToolAction{Command: "uninstall", Args: map[string]string{"release": "my-app", "namespace": "default"}},
			want:   []string{"--kubeconfig", "/tmp/kubeconfig", "--kube-context", "my-cluster", "uninstall", "-n", "default", "my-app"},
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

func TestHelmPlugin_buildArgs_NoGlobals(t *testing.T) {
	p := NewHelmPlugin("", "")
	action := models.ToolAction{
		Command: "list",
		Args:    map[string]string{"namespace": "default"},
	}
	args := p.buildArgs(action)
	want := []string{"list", "-n", "default"}
	if len(args) != len(want) {
		t.Errorf("buildArgs() len = %d, want %d\n  got:  %v\n  want: %v", len(args), len(want), args, want)
	}
}

// --- ArgoCDPlugin tests ---

func TestArgoCDPlugin_Name(t *testing.T) {
	p := NewArgoCDPlugin("", "", false)
	if got := p.Name(); got != "argocd" {
		t.Errorf("Name() = %q, want %q", got, "argocd")
	}
}

func TestArgoCDPlugin_Capabilities(t *testing.T) {
	p := NewArgoCDPlugin("", "", false)
	caps := p.Capabilities()
	if len(caps) == 0 {
		t.Fatal("Capabilities() returned empty")
	}
	hasRead := false
	hasWrite := false
	hasDestructive := false
	for _, c := range caps {
		switch c {
		case models.CapRead:
			hasRead = true
		case models.CapWrite:
			hasWrite = true
		case models.CapDestructive:
			hasDestructive = true
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
}

func TestArgoCDPlugin_Validate_AllowsReadCommands(t *testing.T) {
	p := NewArgoCDPlugin("", "", false)
	tests := []struct {
		cmd  string
		args map[string]string
	}{
		{"app list", map[string]string{}},
		{"app get", map[string]string{"app": "my-app"}},
		{"app diff", map[string]string{"app": "my-app"}},
		{"app history", map[string]string{"app": "my-app"}},
		{"app resources", map[string]string{"app": "my-app"}},
		{"app wait", map[string]string{"app": "my-app"}},
		{"proj list", map[string]string{}},
		{"proj get", map[string]string{"project": "default"}},
		{"cluster list", map[string]string{}},
		{"cluster get", map[string]string{"server": "https://10.0.0.1:6443"}},
		{"repo list", map[string]string{}},
		{"repo get", map[string]string{"repo": "https://github.com/org/repo"}},
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

func TestArgoCDPlugin_Validate_AllowsWriteCommands(t *testing.T) {
	p := NewArgoCDPlugin("", "", false)
	tests := []struct {
		cmd  string
		args map[string]string
	}{
		{"app sync", map[string]string{"app": "my-app"}},
		{"app rollback", map[string]string{"app": "my-app", "revision": "5"}},
		{"app set", map[string]string{"app": "my-app", "auto-sync": "false"}},
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

func TestArgoCDPlugin_Validate_BlocksBlockedCommands(t *testing.T) {
	p := NewArgoCDPlugin("", "", false)
	blocked := []string{"login", "logout", "account", "cluster add", "cluster remove", "app create", "app delete", "proj create", "proj delete", "cert", "gpg", "admin"}
	for _, cmd := range blocked {
		t.Run(cmd, func(t *testing.T) {
			action := models.ToolAction{Command: cmd}
			if err := p.Validate(action); err == nil {
				t.Error("Validate() expected error, got nil")
			}
		})
	}
}

func TestArgoCDPlugin_Validate_RejectsInvalidAppName(t *testing.T) {
	p := NewArgoCDPlugin("", "", false)
	tests := []string{
		"../etc/passwd",
		"; rm -rf /",
		"$(id)",
		"app with spaces",
	}
	for _, app := range tests {
		t.Run(app, func(t *testing.T) {
			action := models.ToolAction{
				Command: "app get",
				Args:    map[string]string{"app": app},
			}
			if err := p.Validate(action); err == nil {
				t.Errorf("Validate() expected error for app %q, got nil", app)
			}
		})
	}
}

func TestArgoCDPlugin_Validate_RejectsInvalidProject(t *testing.T) {
	p := NewArgoCDPlugin("", "", false)
	action := models.ToolAction{
		Command: "proj get",
		Args:    map[string]string{"project": "../etc"},
	}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for invalid project, got nil")
	}
}

func TestArgoCDPlugin_Validate_RejectsInvalidRevision(t *testing.T) {
	p := NewArgoCDPlugin("", "", false)
	tests := []string{"0", "-1", "abc"}
	for _, rev := range tests {
		t.Run(rev, func(t *testing.T) {
			action := models.ToolAction{
				Command: "app rollback",
				Args:    map[string]string{"app": "my-app", "revision": rev},
			}
			if err := p.Validate(action); err == nil {
				t.Errorf("Validate() expected error for revision %q, got nil", rev)
			}
		})
	}
}

func TestArgoCDPlugin_Validate_RejectsUnknownCommand(t *testing.T) {
	p := NewArgoCDPlugin("", "", false)
	action := models.ToolAction{Command: "not-a-command"}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for unknown command, got nil")
	}
}

func TestArgoCDPlugin_buildArgs(t *testing.T) {
	p := NewArgoCDPlugin("argocd.example.com:443", "Bearer mytoken", true)
	tests := []struct {
		name   string
		action models.ToolAction
		want   []string
	}{
		{
			name:   "app list",
			action: models.ToolAction{Command: "app list", Args: map[string]string{}},
			want:   []string{"--server", "argocd.example.com:443", "--auth-token", "Bearer mytoken", "--grpc-web", "app", "list"},
		},
		{
			name:   "app get",
			action: models.ToolAction{Command: "app get", Args: map[string]string{"app": "my-app"}},
			want:   []string{"--server", "argocd.example.com:443", "--auth-token", "Bearer mytoken", "--grpc-web", "app", "get", "my-app"},
		},
		{
			name:   "app sync with prune",
			action: models.ToolAction{Command: "app sync", Args: map[string]string{"app": "my-app", "prune": "true"}},
			want:   []string{"--server", "argocd.example.com:443", "--auth-token", "Bearer mytoken", "--grpc-web", "app", "sync", "my-app", "--prune", "true"},
		},
		{
			name:   "app rollback",
			action: models.ToolAction{Command: "app rollback", Args: map[string]string{"app": "my-app", "revision": "5"}},
			want:   []string{"--server", "argocd.example.com:443", "--auth-token", "Bearer mytoken", "--grpc-web", "app", "rollback", "my-app", "--revision", "5"},
		},
		{
			name:   "proj list",
			action: models.ToolAction{Command: "proj list", Args: map[string]string{}},
			want:   []string{"--server", "argocd.example.com:443", "--auth-token", "Bearer mytoken", "--grpc-web", "proj", "list"},
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

func TestArgoCDPlugin_buildArgs_NoGlobals(t *testing.T) {
	p := NewArgoCDPlugin("", "", false)
	action := models.ToolAction{
		Command: "app list",
		Args:    map[string]string{},
	}
	args := p.buildArgs(action)
	want := []string{"app", "list"}
	if len(args) != len(want) {
		t.Errorf("buildArgs() len = %d, want %d\n  got:  %v\n  want: %v", len(args), len(want), args, want)
	}
}

// TestSplitOnSpace verifies the splitOnSpace helper.
func TestSplitOnSpace(t *testing.T) {
	tests := []struct {
		input string
		want  []string
	}{
		{"app list", []string{"app", "list"}},
		{"get values", []string{"get", "values"}},
		{"list", []string{"list"}},
		{"", []string{""}},
	}
	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got := splitOnSpace(tt.input)
			if len(got) != len(tt.want) {
				t.Errorf("splitOnSpace() len = %d, want %d\n  got: %v\n  want: %v", len(got), len(tt.want), got, tt.want)
				return
			}
			for i := range got {
				if got[i] != tt.want[i] {
					t.Errorf("splitOnSpace() [%d] = %q, want %q", i, got[i], tt.want[i])
				}
			}
		})
	}
}
