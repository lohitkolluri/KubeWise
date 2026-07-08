package tools

import (
	"testing"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func TestKubectlPlugin_Name(t *testing.T) {
	p := NewKubectlPlugin("")
	if got := p.Name(); got != "kubectl" {
		t.Errorf("Name() = %q, want %q", got, "kubectl")
	}
}

func TestKubectlPlugin_Capabilities(t *testing.T) {
	p := NewKubectlPlugin("")
	caps := p.Capabilities()
	if len(caps) == 0 {
		t.Fatal("Capabilities() returned empty")
	}

	// Must include read and write
	hasRead := false
	hasWrite := false
	for _, c := range caps {
		if c == models.CapRead {
			hasRead = true
		}
		if c == models.CapWrite {
			hasWrite = true
		}
	}
	if !hasRead {
		t.Error("Capabilities() missing CapRead")
	}
	if !hasWrite {
		t.Error("Capabilities() missing CapWrite")
	}
}

func TestKubectlPlugin_Validate_AllowsValidReadCommands(t *testing.T) {
	p := NewKubectlPlugin("")
	tests := []struct {
		cmd  string
		args map[string]string
	}{
		{"get", map[string]string{"resource": "pods"}},
		{"get", map[string]string{"resource": "pods", "name": "my-pod", "namespace": "default"}},
		{"describe", map[string]string{"resource": "deployment", "name": "web", "namespace": "prod"}},
		{"logs", map[string]string{"resource": "pods", "name": "web-7f8b9", "namespace": "staging", "tail": "50"}},
		{"top", map[string]string{"resource": "nodes"}},
		{"api-resources", map[string]string{}},
		{"version", map[string]string{}},
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

func TestKubectlPlugin_Validate_AllowsValidWriteCommands(t *testing.T) {
	p := NewKubectlPlugin("")
	tests := []struct {
		cmd  string
		args map[string]string
	}{
		{"apply", map[string]string{"filename": "deploy.yaml"}},
		{"delete", map[string]string{"resource": "pod", "name": "bad-pod", "namespace": "default"}},
		{"rollout", map[string]string{"resource": "deployment", "name": "web", "namespace": "prod", "command": "restart"}},
		{"scale", map[string]string{"resource": "deployment", "name": "web", "replicas": "5"}},
		{"label", map[string]string{"resource": "pod", "name": "my-pod", "labels": "env=prod"}},
		{"drain", map[string]string{"name": "node-1"}},
		{"cordon", map[string]string{"name": "node-1"}},
		{"uncordon", map[string]string{"name": "node-1"}},
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

func TestKubectlPlugin_Validate_BlocksExec(t *testing.T) {
	p := NewKubectlPlugin("")
	// Previously blocked commands are now allowed — RBAC delegates authorization.
	// These commands should validate successfully (RBAC will enforce actual access).
	allowed := []string{"exec", "proxy", "port-forward", "attach", "run", "cp", "auth"}
	for _, cmd := range allowed {
		t.Run(cmd, func(t *testing.T) {
			action := models.ToolAction{Command: cmd}
			if err := p.Validate(action); err != nil {
				t.Errorf("Validate() unexpected error for allowed command %q: %v", cmd, err)
			}
		})
	}
}

func TestKubectlPlugin_Validate_RejectsUnknownCommands(t *testing.T) {
	p := NewKubectlPlugin("")
	action := models.ToolAction{Command: "unsupported"}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for unknown command, got nil")
	}
}

func TestKubectlPlugin_Validate_RejectsInvalidResourceName(t *testing.T) {
	p := NewKubectlPlugin("")
	tests := []string{
		"../etc/passwd",
		"; rm -rf /",
		"$(id)",
		"`whoami`",
		"pod | cat /etc/passwd",
	}
	for _, name := range tests {
		t.Run(name, func(t *testing.T) {
			action := models.ToolAction{
				Command: "get",
				Args:    map[string]string{"resource": "pod", "name": name},
			}
			if err := p.Validate(action); err == nil {
				t.Errorf("Validate() expected error for name %q, got nil", name)
			}
		})
	}
}

func TestKubectlPlugin_Validate_RejectsInvalidNamespace(t *testing.T) {
	p := NewKubectlPlugin("")
	action := models.ToolAction{
		Command: "get",
		Args:    map[string]string{"resource": "pods", "namespace": "../../etc"},
	}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for invalid namespace, got nil")
	}
}

func TestKubectlPlugin_Validate_RejectsInvalidResourceType(t *testing.T) {
	p := NewKubectlPlugin("")
	action := models.ToolAction{
		Command: "get",
		Args:    map[string]string{"resource": "; rm -rf /"},
	}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for invalid resource type, got nil")
	}
}

func TestKubectlPlugin_Validate_RejectsInvalidLabelSelector(t *testing.T) {
	p := NewKubectlPlugin("")
	action := models.ToolAction{
		Command: "get",
		Args:    map[string]string{"resource": "pods", "selector": "$(id)"},
	}
	if err := p.Validate(action); err == nil {
		t.Error("Validate() expected error for invalid selector, got nil")
	}
}

func TestKubectlPlugin_Validate_AllowsNamespaceForApply(t *testing.T) {
	p := NewKubectlPlugin("")
	action := models.ToolAction{
		Command: "apply",
		Args:    map[string]string{"filename": "deploy.yaml", "namespace": "default,staging"},
	}
	if err := p.Validate(action); err != nil {
		t.Errorf("Validate() unexpected error for apply with multi-namespace: %v", err)
	}
}

func TestKubectlPlugin_buildArgs(t *testing.T) {
	p := NewKubectlPlugin("/tmp/kubeconfig")
	tests := []struct {
		name   string
		action models.ToolAction
		want   []string
	}{
		{
			name:   "get pods in namespace",
			action: models.ToolAction{Command: "get", Args: map[string]string{"resource": "pods", "namespace": "default", "output": "json"}},
			want:   []string{"--kubeconfig", "/tmp/kubeconfig", "get", "-n", "default", "pods", "--output", "json"},
		},
		{
			name:   "delete specific pod",
			action: models.ToolAction{Command: "delete", Args: map[string]string{"resource": "pod", "name": "bad-pod-abc", "namespace": "prod"}},
			want:   []string{"--kubeconfig", "/tmp/kubeconfig", "delete", "-n", "prod", "pod", "bad-pod-abc"},
		},
		{
			name:   "apply with filename",
			action: models.ToolAction{Command: "apply", Args: map[string]string{"filename": "deploy.yaml"}},
			want:   []string{"--kubeconfig", "/tmp/kubeconfig", "apply", "--filename", "deploy.yaml"},
		},
		{
			name:   "logs with tail",
			action: models.ToolAction{Command: "logs", Args: map[string]string{"resource": "pods", "name": "web-7f8b9", "tail": "50"}},
			want:   []string{"--kubeconfig", "/tmp/kubeconfig", "logs", "pods", "web-7f8b9", "--tail", "50"},
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

func TestKubectlPlugin_buildArgs_NoKubeconfig(t *testing.T) {
	p := NewKubectlPlugin("")
	action := models.ToolAction{
		Command: "get",
		Args:    map[string]string{"resource": "pods", "namespace": "default"},
	}
	args := p.buildArgs(action)
	want := []string{"get", "-n", "default", "pods"}
	if len(args) != len(want) {
		t.Errorf("buildArgs() len = %d, want %d\n  got:  %v\n  want: %v", len(args), len(want), args, want)
	}
}

func TestSplitCSV(t *testing.T) {
	tests := []struct {
		input string
		want  []string
	}{
		{"default,staging", []string{"default", "staging"}},
		{"default", []string{"default"}},
		{"", nil},
		{"a, b, c", []string{"a", "b", "c"}},
	}
	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got := splitCSV(tt.input)
			if len(got) != len(tt.want) {
				t.Errorf("splitCSV() len = %d, want %d\n  got: %v\n  want: %v", len(got), len(tt.want), got, tt.want)
				return
			}
			for i := range got {
				if got[i] != tt.want[i] {
					t.Errorf("splitCSV() [%d] = %q, want %q", i, got[i], tt.want[i])
				}
			}
		})
	}
}

func TestKubectlPlugin_Validate_RejectsInvalidNameInBlockedList(t *testing.T) {
	p := NewKubectlPlugin("")
	// exec with valid args should be allowed (RBAC gate, not hardcoded block)
	action := models.ToolAction{
		Command: "exec",
		Args:    map[string]string{"resource": "pod", "name": "valid-name"},
	}
	if err := p.Validate(action); err != nil {
		t.Errorf("Validate() unexpected error for exec command: %v", err)
	}
}
