package cli

import (
	"testing"
)

func TestGenerateAPIToken_FromEnv(t *testing.T) {
	t.Setenv("KUBEWISE_API_TOKEN", "my-pre-set-token")
	tok := generateAPIToken()
	if tok != "my-pre-set-token" {
		t.Fatalf("expected token from env, got %q", tok)
	}
}

func TestGenerateAPIToken_Random(t *testing.T) {
	// Unset the env var so generateAPIToken falls through to crypto/rand.
	t.Setenv("KUBEWISE_API_TOKEN", "")
	tok := generateAPIToken()
	if tok == "" {
		t.Fatal("expected non-empty random token")
	}
	if len(tok) != 48 { // 24 bytes hex-encoded = 48 chars
		t.Fatalf("expected 48-char hex token, got %d chars: %q", len(tok), tok)
	}
}

func TestGenerateAPIToken_Deterministic(t *testing.T) {
	// Calling twice should produce different values (no env override).
	t.Setenv("KUBEWISE_API_TOKEN", "")
	a := generateAPIToken()
	b := generateAPIToken()
	if a == "" || b == "" {
		t.Fatal("expected non-empty tokens")
	}
	if a == b {
		t.Fatal("expected different tokens across calls")
	}
}

func TestPromptAPIToken_NonTTY(t *testing.T) {
	tok := promptAPIToken(nil)
	if tok != "" {
		t.Fatalf("expected empty token for non-TTY stdin, got %q", tok)
	}
}

func TestGenerateAPIToken_Race(t *testing.T) {
	// Sanity: generateAPIToken doesn't use shared state, so calling from
	// concurrent goroutines should always produce valid hex tokens.
	t.Setenv("KUBEWISE_API_TOKEN", "")
	c := make(chan string, 20)
	for range 20 {
		go func() {
			c <- generateAPIToken()
		}()
	}
	for range 20 {
		tok := <-c
		if tok == "" || len(tok) != 48 {
			t.Fatalf("expected 48-char hex token, got %d: %q", len(tok), tok)
		}
	}
}

// Test that generateAPIToken env var takes priority over random generation
// even when the env is set to empty string.
func TestGenerateAPIToken_EmptyEnv(t *testing.T) {
	t.Setenv("KUBEWISE_API_TOKEN", "")
	tok := generateAPIToken()
	// Setenv to "" should still cause the os.Getenv check to return ""
	// (not the random path being skipped). The function checks:
	// if tok := os.Getenv("KUBEWISE_API_TOKEN"); tok != "" { return tok }
	// So empty env should fall through to rand.
	if tok == "" {
		t.Fatal("expected non-empty token when env is empty")
	}
}

func TestSaveInstallProfile_EnvTokenTakesPriority(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("KUBEWISE_API_TOKEN", "env-token-save")

	// Preserve globals
	savedNS := agentNS
	savedSvc := agentSvc
	defer func() {
		agentNS = savedNS
		agentSvc = savedSvc
	}()

	agentNS = "test-ns"
	agentSvc = "test-svc"

	err := saveInstallProfile()
	if err != nil {
		t.Fatalf("saveInstallProfile: %v", err)
	}

	p := activeProfile()
	if p.APIToken != "env-token-save" {
		t.Fatalf("expected token 'env-token-save' in profile, got %q", p.APIToken)
	}
	if p.AgentNamespace != "test-ns" {
		t.Fatalf("expected ns 'test-ns', got %q", p.AgentNamespace)
	}
	if p.AgentService != "test-svc" {
		t.Fatalf("expected svc 'test-svc', got %q", p.AgentService)
	}
}

func TestSaveInstallProfile_NoToken(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("KUBEWISE_API_TOKEN", "")

	err := saveInstallProfile()
	if err != nil {
		t.Fatalf("saveInstallProfile: %v", err)
	}

	p := activeProfile()
	if p.APIToken != "" {
		t.Fatalf("expected empty token when env is unset, got %q", p.APIToken)
	}
}
