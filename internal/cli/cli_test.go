package cli

import (
	"bytes"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func executeCommand(args ...string) (string, error) {
	buf := new(bytes.Buffer)
	rootCmd.SetOut(buf)
	rootCmd.SetErr(buf)
	rootCmd.SetArgs(args)
	err := rootCmd.Execute()
	return buf.String(), err
}

// withUnreachableAgent forces commands to fail fast in unit tests even when a local agent is running.
func withUnreachableAgent(t *testing.T, fn func()) {
	t.Helper()
	oldURL := agentURL
	agentURL = "http://127.0.0.1:1"
	defer func() { agentURL = oldURL }()
	fn()
}

func TestRootHelp(t *testing.T) {
	output, err := executeCommand("--help")
	if err != nil {
		t.Fatalf("--help: %v", err)
	}
	if !strings.Contains(output, "kwctl") {
		t.Fatal("expected kwctl in help output")
	}
	if !strings.Contains(output, "status") {
		t.Fatal("expected status subcommand in help")
	}
	if !strings.Contains(output, "config") {
		t.Fatal("expected config subcommand in help")
	}
	if !strings.Contains(output, "predict") {
		t.Fatal("expected predict subcommand in help")
	}
}

func TestStatusNeedsAgent(t *testing.T) {
	withUnreachableAgent(t, func() {
		_, err := executeCommand("status")
		if err == nil {
			t.Fatal("expected error when no agent is running")
		}
		if !strings.Contains(err.Error(), "connecting to agent") {
			t.Fatalf("expected connection error, got: %v", err)
		}
	})
}

func TestConfigNeedsAgent(t *testing.T) {
	withUnreachableAgent(t, func() {
		_, err := executeCommand("config")
		if err == nil {
			t.Fatal("expected error when no agent is running")
		}
	})
}

func TestPredictNeedsAgent(t *testing.T) {
	withUnreachableAgent(t, func() {
		_, err := executeCommand("predict")
		if err == nil {
			t.Fatal("expected error when no agent is running")
		}
	})
}

func TestTableOutput(t *testing.T) {
	saved := outputFormat
	outputFormat = "table"
	defer func() { outputFormat = saved }()

	withUnreachableAgent(t, func() {
		_, err := executeCommand("status")
		if err == nil {
			t.Fatal("expected connection error")
		}
	})
}

func TestJSONOutput(t *testing.T) {
	saved := outputFormat
	outputFormat = "json"
	defer func() { outputFormat = saved }()

	withUnreachableAgent(t, func() {
		_, err := executeCommand("status")
		if err == nil {
			t.Fatal("expected connection error")
		}
	})
}

func TestYAMLOutput(t *testing.T) {
	saved := outputFormat
	outputFormat = "yaml"
	defer func() { outputFormat = saved }()

	withUnreachableAgent(t, func() {
		_, err := executeCommand("status")
		if err == nil {
			t.Fatal("expected connection error")
		}
	})
}

func TestUnknownCommand(t *testing.T) {
	_, err := executeCommand("nonexistent")
	if err == nil {
		t.Fatal("expected error for unknown subcommand")
	}
}

func TestStatusWithFormatFlag(t *testing.T) {
	withUnreachableAgent(t, func() {
		_, err := executeCommand("status", "-o", "json")
		if err == nil {
			t.Fatal("expected connection error with format flag")
		}
	})
}

func TestMultipleSubcommandsInHelp(t *testing.T) {
	output, err := executeCommand("--help")
	if err != nil {
		t.Fatalf("--help: %v", err)
	}
	for _, cmd := range []string{"status", "config", "predict", "ui", "watch", "connect", "profile", "logs", "agent"} {
		if !strings.Contains(output, cmd) {
			t.Fatalf("expected %q subcommand in help", cmd)
		}
	}
}

// Test with a running HTTP server to verify client logic
func TestStatusWithFakeAgent(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintln(w, `{"uptime":"5m","started_at":"2026-07-06T00:00:00Z","scrapes":42}`)
	}))
	defer ts.Close()

	// Override the agent URL for the test
	oldURL := agentURL
	agentURL = ts.URL
	defer func() { agentURL = oldURL }()

	output, err := executeCommand("status")
	if err != nil {
		t.Fatalf("status with fake agent: %v", err)
	}
	if !strings.Contains(output, "5m") {
		t.Fatalf("expected uptime in output, got: %s", output)
	}
	if !strings.Contains(output, "42") {
		t.Fatalf("expected scrapes in output, got: %s", output)
	}
}

func TestConfigWithFakeAgent(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintln(w, `{"scrape_interval":"30s","prometheus_address":"http://localhost:9090","loki_url":"http://loki:3100","tempo_url":"http://tempo:3200","llm_provider":"","llm_model":"","remediation":{"mode":"","dry_run":true,"rate_limit":0,"namespace_denylist":null,"allowlist":null}}`)
	}))
	defer ts.Close()

	oldURL := agentURL
	agentURL = ts.URL
	defer func() { agentURL = oldURL }()

	output, err := executeCommand("config")
	if err != nil {
		t.Fatalf("config with fake agent: %v", err)
	}
	if !strings.Contains(output, "30s") {
		t.Fatalf("expected scrape interval in output, got: %s", output)
	}
	if !strings.Contains(output, "loki") && !strings.Contains(output, "Loki") {
		t.Fatalf("expected loki in output, got: %s", output)
	}
}

func TestConfigNoConfigMessage(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		fmt.Fprintln(w, `{"message":"no config saved"}`)
	}))
	defer ts.Close()

	oldURL := agentURL
	agentURL = ts.URL
	defer func() { agentURL = oldURL }()

	output, err := executeCommand("config")
	if err != nil {
		t.Fatalf("config with no saved config: %v", err)
	}
	if !strings.Contains(output, "no config saved") {
		t.Fatalf("expected 'no config saved', got: %s", output)
	}
}

func TestPredictEmptyWithFakeAgent(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintln(w, `[]`)
	}))
	defer ts.Close()

	oldURL := agentURL
	agentURL = ts.URL
	defer func() { agentURL = oldURL }()

	output, err := executeCommand("predict", "-o", "table")
	if err != nil {
		t.Fatalf("predict with empty response: %v", err)
	}
	if !strings.Contains(output, "No active predictions") {
		t.Fatalf("expected 'No active predictions', got: %q", output)
	}
}

func TestPredictWithResults(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintln(w, `[{"type":"OOM","entity":"pod-a","namespace":"default","action":"restart","confidence":0.85,"eta_seconds":60,"timestamp":"2026-07-06T01:00:00Z","score":0.85}]`)
	}))
	defer ts.Close()

	oldURL := agentURL
	agentURL = ts.URL
	defer func() { agentURL = oldURL }()

	output, err := executeCommand("predict")
	if err != nil {
		t.Fatalf("predict with results: %v", err)
	}
	if !strings.Contains(output, "OOM") {
		t.Fatalf("expected OOM type in output, got: %s", output)
	}
	if !strings.Contains(output, "pod-a") {
		t.Fatalf("expected pod-a in output, got: %s", output)
	}
}

func TestPersistentFlagsInHelp(t *testing.T) {
	output, err := executeCommand("--help")
	if err != nil {
		t.Fatalf("--help: %v", err)
	}
	for _, flag := range []string{"--kubeconfig", "--context", "--agent-namespace", "--output"} {
		if !strings.Contains(output, flag) {
			t.Fatalf("expected flag %q in help output", flag)
		}
	}
}

func TestOutputFlagFormat(t *testing.T) {
	output, err := executeCommand("--help")
	if err != nil {
		t.Fatalf("--help: %v", err)
	}
	if !strings.Contains(output, "json") || !strings.Contains(output, "yaml") || !strings.Contains(output, "table") {
		t.Fatal("expected output format options in help")
	}
}

func TestRootNoArgs(t *testing.T) {
	// Non-TTY stdout in tests should show help without error.
	output, err := executeCommand()
	if err != nil {
		t.Fatalf("no args: %v", err)
	}
	if !strings.Contains(output, "kwctl") {
		t.Fatal("expected help output")
	}
}
