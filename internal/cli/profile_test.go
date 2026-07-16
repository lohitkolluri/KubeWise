package cli

import "testing"

func TestActiveProfile_DefaultsWhenNoFile(t *testing.T) {
	// Ensure we don't pick up the real user profile.
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	p := activeProfile()
	if p.AgentURL != "http://localhost:8080" {
		t.Fatalf("expected default agent URL, got %q", p.AgentURL)
	}
	if p.AgentNamespace != "kubewise" {
		t.Fatalf("expected default namespace, got %q", p.AgentNamespace)
	}
	if p.AgentService != "kubewise" {
		t.Fatalf("expected default service, got %q", p.AgentService)
	}
	if p.Output != "table" {
		t.Fatalf("expected default output, got %q", p.Output)
	}
	if p.TimeoutSeconds != 15 {
		t.Fatalf("expected default timeout 15, got %d", p.TimeoutSeconds)
	}
	if p.APIToken != "" {
		t.Fatalf("expected empty api token, got %q", p.APIToken)
	}
}

func TestDefaultProfileFile_Fields(t *testing.T) {
	pf := defaultProfileFile()
	if pf.Current != "default" {
		t.Fatalf("expected current=default, got %q", pf.Current)
	}
	if len(pf.Profiles) != 1 {
		t.Fatalf("expected 1 profile, got %d", len(pf.Profiles))
	}
	d := pf.Profiles["default"]
	if d.AgentURL != "http://localhost:8080" {
		t.Fatalf("expected default URL, got %q", d.AgentURL)
	}
}

func TestApplyProfileDefaults_TokenFromEnv(t *testing.T) {
	savedToken := apiToken
	defer func() { apiToken = savedToken }()
	savedURL := agentURL
	defer func() { agentURL = savedURL }()

	t.Setenv("KUBEWISE_API_TOKEN", "env-token-from-test")

	apiToken = ""
	agentURL = ""
	applyProfileDefaults()
	if apiToken != "env-token-from-test" { //nolint:gosec // test token, not a real credential
		t.Fatalf("expected token from env, got %q", apiToken)
	}
}

func TestApplyProfileDefaults_TokenAlreadySet(t *testing.T) {
	savedToken := apiToken
	defer func() { apiToken = savedToken }()
	savedURL := agentURL
	defer func() { agentURL = savedURL }()

	t.Setenv("KUBEWISE_API_TOKEN", "env-token")

	apiToken = "already-set"
	applyProfileDefaults()
	if apiToken != "already-set" {
		t.Fatalf("expected already-set token to stay, got %q", apiToken)
	}
}

func TestApplyProfileDefaults_AgentURLFromEnv(t *testing.T) {
	savedURL := agentURL
	defer func() { agentURL = savedURL }()

	t.Setenv("KUBEWISE_AGENT_URL", "http://from-env:8080")
	agentURL = ""
	applyProfileDefaults()
	if agentURL != "http://from-env:8080" {
		t.Fatalf("expected URL from env, got %q", agentURL)
	}
}

func TestParseSetArg_ValidKeyValue(t *testing.T) {
	key, val, err := parseSetArg("timeout=30")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if key != "timeout" {
		t.Fatalf("expected key 'timeout', got %q", key)
	}
	if val != "30" {
		t.Fatalf("expected value '30', got %q", val)
	}
}

func TestParseSetArg_Invalid(t *testing.T) {
	_, _, err := parseSetArg("noequal")
	if err == nil {
		t.Fatal("expected error for missing '='")
	}
}

func TestParseSetArg_EmptyValue(t *testing.T) {
	key, val, err := parseSetArg("key=")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if key != "key" {
		t.Fatalf("expected key 'key', got %q", key)
	}
	if val != "" {
		t.Fatalf("expected empty value, got %q", val)
	}
}

// profilePath uses XDG_CONFIG_HOME or ~/.config. Capture it to verify path construction.
func TestProfilePath_UsesXDG(t *testing.T) {
	xdgHome := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", xdgHome)
	path, err := profilePath()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := xdgHome + "/kwctl/config.yaml"
	if path != want {
		t.Fatalf("expected path %q, got %q", want, path)
	}
}

func TestSetProfileField_UnknownKey(t *testing.T) {
	err := setProfileField("default", "nonexistent", "value")
	if err == nil {
		t.Fatal("expected error for unknown key")
	}
}

func TestActiveProfile_EmptyNamespaceDefaults(t *testing.T) {
	// Create a temporary profile with empty fields to verify defaults fill in.
	tmpDir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", tmpDir)

	pf := defaultProfileFile()
	p := pf.Profiles["default"]
	p.AgentNamespace = ""
	p.AgentService = ""
	p.AgentURL = ""
	pf.Profiles["default"] = p
	err := saveProfileFile(pf)
	if err != nil {
		t.Fatalf("save: %v", err)
	}

	ap := activeProfile()
	if ap.AgentURL != "http://localhost:8080" {
		t.Fatalf("expected default URL, got %q", ap.AgentURL)
	}
	if ap.AgentNamespace != "kubewise" {
		t.Fatalf("expected default namespace, got %q", ap.AgentNamespace)
	}
	if ap.AgentService != "kubewise" {
		t.Fatalf("expected default service, got %q", ap.AgentService)
	}
	if ap.TimeoutSeconds != 15 {
		t.Fatalf("expected default timeout 15, got %d", ap.TimeoutSeconds)
	}
}
