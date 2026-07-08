package wizard

import (
	"context"
	"fmt"
	"os"
	"os/exec"

	"gopkg.in/yaml.v3"
)

// Execute performs the install using the collected WizardState.
// Returns a verbose log of all actions taken and any fatal error.
func (s WizardState) Execute(ctx context.Context) (string, error) {
	var log stringsBuilder

	if s.DryRun {
		log.WriteString("[dry-run] Validating configuration...\n")
		configYAML, err := s.GenerateConfig()
		if err != nil {
			return log.String(), fmt.Errorf("generate config: %w", err)
		}
		log.WriteString("Generated config:\n")
		log.WriteString(configYAML)
		log.WriteString("\n[dry-run] No changes made.\n")
		return log.String(), nil
	}

	// 1. Generate and save config.
	log.WriteString("Generating configuration...\n")
	configYAML, err := s.GenerateConfig()
	if err != nil {
		return log.String(), fmt.Errorf("generate config: %w", err)
	}

	configPath := configFilePath()
	if err := os.MkdirAll(configDir(), 0755); err != nil {
		return log.String(), fmt.Errorf("create config dir: %w", err)
	}
	if err := os.WriteFile(configPath, []byte(configYAML), 0600); err != nil {
		return log.String(), fmt.Errorf("write config: %w", err)
	}
	log.WriteString(fmt.Sprintf("✓ Config saved to %s\n", configPath))

	// 2. Run Helm install.
	log.WriteString("Installing Helm chart...\n")
	if err := s.helmInstall(ctx, &log); err != nil {
		return log.String(), fmt.Errorf("helm install: %w", err)
	}

	return log.String(), nil
}

// GenerateConfig produces a KubeWise agent YAML config from the wizard state.
func (s WizardState) GenerateConfig() (string, error) {
	cfg := map[string]interface{}{
		"agent": map[string]interface{}{
			"llm_provider": llmProvider(s),
			"llm_model":    llmModel(s),
			"llm_base_url": s.OllamaURL,
			"remediation": map[string]interface{}{
				"mode": s.RemediationMode,
			},
			"notifications": map[string]interface{}{
				"enabled":               s.SlackWebhookURL != "" || s.PagerDutyKey != "",
				"slack_webhook_url":     s.SlackWebhookURL,
				"pagerduty_routing_key": s.PagerDutyKey,
				"on_prediction":         true,
				"on_remediation":        true,
				"on_approval":           true,
			},
			"watch_namespaces": s.WatchNamespaces,
		},
		"observability": map[string]interface{}{
			"loki":    s.EnableLoki,
			"tempo":   s.EnableTempo,
			"grafana": s.EnableGrafana,
		},
	}

	out, err := yaml.Marshal(cfg)
	if err != nil {
		return "", err
	}
	return string(out), nil
}

func llmProvider(s WizardState) string {
	if s.OpenRouterKey != "" {
		return "openrouter"
	}
	if s.OllamaURL != "" {
		return "ollama"
	}
	return ""
}

func llmModel(s WizardState) string {
	if s.OpenRouterKey != "" {
		return "openai/gpt-4o-mini"
	}
	if s.OllamaURL != "" {
		return "llama3.1:8b"
	}
	return ""
}

func (s WizardState) helmInstall(ctx context.Context, log *stringsBuilder) error {
	if _, err := exec.LookPath("helm"); err != nil {
		return fmt.Errorf("helm not found in PATH")
	}

	args := []string{
		"upgrade", "--install", "kubewise",
		"oci://ghcr.io/lohitkolluri/charts/kubewise",
		"-n", "kubewise", "--create-namespace",
		"--wait", "--timeout", "5m",
	}

	if s.OpenRouterKey != "" {
		args = append(args, "--set", "secrets.openrouterApiKey="+s.OpenRouterKey)
	}
	if len(s.WatchNamespaces) > 0 {
		for _, ns := range s.WatchNamespaces {
			args = append(args, "--set", "agent.watchNamespaces={"+ns+"}")
		}
	}
	args = append(args, "--set", "agent.remediation.mode="+s.RemediationMode)

	cmd := exec.CommandContext(ctx, "helm", args...)
	cmd.Stdout = log
	cmd.Stderr = log

	if err := cmd.Run(); err != nil {
		return fmt.Errorf("helm failed: %w", err)
	}
	return nil
}

// stringsBuilder accumulates strings and implements io.Writer.
type stringsBuilder struct {
	data string
}

func (b *stringsBuilder) WriteString(s string) (int, error) {
	b.data += s
	return len(s), nil
}

func (b *stringsBuilder) Write(p []byte) (int, error) {
	n := len(p)
	b.data += string(p)
	return n, nil
}

func (b *stringsBuilder) String() string {
	return b.data
}

func configDir() string {
	if d := os.Getenv("KUBEWISE_CONFIG_DIR"); d != "" {
		return d
	}
	home, _ := os.UserHomeDir()
	return home + "/.config/kwctl"
}

func configFilePath() string {
	return configDir() + "/config.yaml"
}

// Validate performs pre-flight checks and returns a list of warnings.
func (s WizardState) Validate() []string {
	var warnings []string

	if s.OpenRouterKey == "" && s.OllamaURL == "" {
		warnings = append(warnings, "No LLM provider configured — running in observe-only mode")
	}
	if s.OpenRouterKey != "" && len(s.OpenRouterKey) < 20 {
		warnings = append(warnings, "OpenRouter API key looks too short — verify it is correct")
	}
	if s.RemediationMode == "" {
		warnings = append(warnings, "Remediation mode not set, defaulting to dry-run")
	}
	if s.EnableLoki || s.EnableTempo || s.EnableGrafana {
		if _, err := exec.LookPath("helm"); err != nil {
			warnings = append(warnings, "You'll need Helm installed to deploy the observability stack")
		}
	}
	if s.SlackWebhookURL != "" && !stringsContains(s.SlackWebhookURL, "hooks.slack.com") {
		warnings = append(warnings, "Slack webhook URL doesn't look like a Slack webhook")
	}

	return warnings
}

func stringsContains(s, substr string) bool {
	return len(s) >= len(substr) && containsStr(s, substr)
}

func containsStr(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}

// Ensure context is used (linter guard).
var _ = context.Background
