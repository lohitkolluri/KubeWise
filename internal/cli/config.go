package cli

import (
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"
	yaml "gopkg.in/yaml.v3"

	"github.com/lohitkolluri/KubeWise/internal/agent/llm"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

var configCmd = &cobra.Command{
	Use:   "config",
	Short: "View or update agent configuration",
}

var configShowCmd = &cobra.Command{
	Use:     "show",
	Aliases: []string{"get"},
	Short:   "Show agent configuration",
	RunE:    runConfigShow,
}

func init() {
	configCmd.AddCommand(configShowCmd, configSetCmd, configApplyCmd, configInitCmd)
	configCmd.RunE = runConfigShow // Legacy: `kwctl config` without subcommand shows config.
	rootCmd.AddCommand(configCmd)

	configApplyCmd.Flags().StringP("file", "f", "", "YAML config file")
	_ = configApplyCmd.MarkFlagRequired("file")

	configInitCmd.Flags().StringVarP(&configInitFile, "file", "f", "agent-config.yaml", "output YAML file path")
	configInitCmd.Flags().BoolVar(&configInitPrint, "print", false, "print YAML to stdout instead of writing a file")
	configInitCmd.Flags().StringVar(&configInitFrom, "from", "agent", "source: agent (current) or defaults")
}

func runConfigShow(cmd *cobra.Command, _ []string) error {
	if err := validateOutputFormat(); err != nil {
		return err
	}
	cfg, err := fetchAgentConfig()
	if err != nil {
		if strings.Contains(err.Error(), "no config") {
			_, _ = fmt.Fprintln(cmd.OutOrStdout(), err.Error())
			return nil
		}
		return err
	}
	return writeOutput(cmd.OutOrStdout(), outputFormat, cfg, func() error {
		out := cmd.OutOrStdout()
		printBanner(out)
		printSection(out, "Agent configuration")
		printKV(out, "Scrape Interval:", cfg.ScrapeInterval)
		printKV(out, "Prometheus:", cfg.PrometheusAddress)
		if strings.TrimSpace(cfg.LokiURL) != "" {
			printKV(out, "Loki:", cfg.LokiURL)
		}
		if strings.TrimSpace(cfg.TempoURL) != "" {
			printKV(out, "Tempo:", cfg.TempoURL)
		}
		printKV(out, "LLM Provider:", cfg.LLMProvider)
		printKV(out, "LLM Model:", cfg.LLMModel)
		printKVStyled(out, "Remediation Mode:", cfg.Remediation.Mode, statusStyle(cfg.Remediation.Mode))
		printKV(out, "Dry Run:", fmt.Sprintf("%v", cfg.Remediation.DryRun))
		printKV(out, "Rate Limit:", fmt.Sprintf("%d", cfg.Remediation.RateLimit))
		if cfg.Remediation.MinConfidence > 0 {
			printKV(out, "Min Confidence:", fmt.Sprintf("%.2f", cfg.Remediation.MinConfidence))
		}
		return nil
	})
}

var configSetCmd = &cobra.Command{
	Use:   "set KEY VALUE",
	Short: "Update a single agent config field (persisted; restart agent to apply runtime changes)",
	Long: `Keys:
  scrape_interval, prometheus_address, loki_url, tempo_url, llm_provider, llm_model
  remediation.mode, remediation.dry_run, remediation.rate_limit, remediation.min_confidence`,
	Args: cobra.ExactArgs(2),
	RunE: func(cmd *cobra.Command, args []string) error {
		cfg, err := fetchAgentConfig()
		if err != nil {
			return err
		}
		if err := applyConfigField(cfg, args[0], args[1]); err != nil {
			return err
		}
		if err := putAgentConfig(cfg); err != nil {
			return err
		}
		printOK(cmd.OutOrStdout(), "Config updated — restart the agent pod to apply runtime changes")
		return nil
	},
}

var configApplyCmd = &cobra.Command{
	Use:   "apply -f FILE",
	Short: "Apply agent configuration from a YAML file",
	RunE: func(cmd *cobra.Command, _ []string) error {
		file, _ := cmd.Flags().GetString("file")
		if file == "" {
			return errors.New("--file is required")
		}
		data, err := os.ReadFile(file) //nolint:gosec // user-provided config file path via --file flag
		if err != nil {
			return err
		}
		var cfg models.AgentConfig
		if err := yaml.Unmarshal(data, &cfg); err != nil {
			return fmt.Errorf("parse yaml: %w", err)
		}
		if err := putAgentConfig(&cfg); err != nil {
			return err
		}
		printOK(cmd.OutOrStdout(), "Config applied — restart the agent pod to apply runtime changes")
		return nil
	},
}

var (
	configInitFile  string
	configInitPrint bool
	configInitFrom  string
)

var configInitCmd = &cobra.Command{
	Use:   "init",
	Short: "Generate a starter agent config YAML file",
	Long: `Generate a starter agent configuration YAML.

By default this uses the current config from the agent (if reachable). You can
also generate a sane default template for committing to git.

Examples:
  kwctl config init -f agent-config.yaml
  kwctl config init --from defaults --print`,
	RunE: func(cmd *cobra.Command, _ []string) error {
		var cfg *models.AgentConfig
		switch strings.TrimSpace(configInitFrom) {
		case "", "agent":
			c, err := fetchAgentConfig()
			if err != nil {
				// If no config saved or agent unreachable, fall back to defaults.
				cfg = defaultAgentConfigTemplate()
			} else {
				cfg = c
			}
		case "defaults":
			cfg = defaultAgentConfigTemplate()
		default:
			return fmt.Errorf("invalid --from %q (use agent or defaults)", configInitFrom)
		}

		if configInitPrint || configInitFile == "" {
			return writeOutput(cmd.OutOrStdout(), "yaml", cfg, func() error { return nil })
		}

		data, err := yaml.Marshal(cfg)
		if err != nil {
			return err
		}
		if err := os.WriteFile(configInitFile, data, 0o600); err != nil {
			return err
		}
		printOK(cmd.OutOrStdout(), "Wrote %s", configInitFile)
		return nil
	},
}

func defaultAgentConfigTemplate() *models.AgentConfig {
	return &models.AgentConfig{
		ScrapeInterval:    "30s",
		PrometheusAddress: "http://prometheus-kube-prometheus-prometheus.monitoring:9090",
		LokiURL:           "",
		TempoURL:          "",
		LLMProvider:       "openrouter",
		LLMModel:          llm.DefaultModel,
		Remediation: models.RemediationConfig{
			Mode:          models.RemediationModeDryRun,
			DryRun:        true,
			RateLimit:     0,
			MinConfidence: 0,
		},
	}
}

func applyConfigField(cfg *models.AgentConfig, key, value string) error {
	switch key {
	case "scrape_interval":
		cfg.ScrapeInterval = value
	case "prometheus_address":
		cfg.PrometheusAddress = value
	case "loki_url":
		cfg.LokiURL = value
	case "tempo_url":
		cfg.TempoURL = value
	case "llm_provider":
		cfg.LLMProvider = value
	case "llm_model":
		cfg.LLMModel = value
	case "remediation.mode":
		cfg.Remediation.Mode = value
	case "remediation.dry_run":
		cfg.Remediation.DryRun = value == "true" || value == "1"
	case "remediation.rate_limit":
		var n int
		if _, err := fmt.Sscanf(value, "%d", &n); err != nil {
			return fmt.Errorf("invalid rate_limit: %w", err)
		}
		cfg.Remediation.RateLimit = n
	case "remediation.min_confidence":
		var f float64
		if _, err := fmt.Sscanf(value, "%f", &f); err != nil {
			return fmt.Errorf("invalid min_confidence: %w", err)
		}
		cfg.Remediation.MinConfidence = f
	default:
		return fmt.Errorf("unknown config key %q", key)
	}
	return nil
}
