package cli

import (
	"fmt"
	"os"
	"strings"

	"github.com/lohitkolluri/KubeWise/pkg/models"
	"github.com/spf13/cobra"
	yaml "gopkg.in/yaml.v3"
)

func init() {
	configCmd.AddCommand(configShowCmd, configSetCmd, configApplyCmd)
	rootCmd.AddCommand(configCmd)
}

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

// Legacy: `kwctl config` without subcommand shows config.
func init() {
	configCmd.RunE = runConfigShow
}

func runConfigShow(cmd *cobra.Command, args []string) error {
	if err := validateOutputFormat(); err != nil {
		return err
	}
	cfg, err := fetchAgentConfig()
	if err != nil {
		if strings.Contains(err.Error(), "no config") {
			fmt.Fprintln(cmd.OutOrStdout(), err.Error())
			return nil
		}
		return err
	}
	return writeOutput(cmd.OutOrStdout(), outputFormat, cfg, func() error {
		fmt.Fprintf(cmd.OutOrStdout(), "%-25s %s\n", "Scrape Interval:", cfg.ScrapeInterval)
		fmt.Fprintf(cmd.OutOrStdout(), "%-25s %s\n", "Prometheus:", cfg.PrometheusAddress)
		fmt.Fprintf(cmd.OutOrStdout(), "%-25s %s\n", "LLM Provider:", cfg.LLMProvider)
		fmt.Fprintf(cmd.OutOrStdout(), "%-25s %s\n", "LLM Model:", cfg.LLMModel)
		fmt.Fprintf(cmd.OutOrStdout(), "%-25s %s\n", "Remediation Mode:", cfg.Remediation.Mode)
		fmt.Fprintf(cmd.OutOrStdout(), "%-25s %v\n", "Dry Run:", cfg.Remediation.DryRun)
		fmt.Fprintf(cmd.OutOrStdout(), "%-25s %d\n", "Rate Limit:", cfg.Remediation.RateLimit)
		if cfg.Remediation.MinConfidence > 0 {
			fmt.Fprintf(cmd.OutOrStdout(), "%-25s %.2f\n", "Min Confidence:", cfg.Remediation.MinConfidence)
		}
		return nil
	})
}

var configSetCmd = &cobra.Command{
	Use:   "set KEY VALUE",
	Short: "Update a single agent config field (persisted; restart agent to apply runtime changes)",
	Long: `Keys:
  scrape_interval, prometheus_address, llm_provider, llm_model
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
		fmt.Fprintln(cmd.OutOrStdout(), "Config updated. Restart the agent pod to apply runtime changes.")
		return nil
	},
}

var configApplyCmd = &cobra.Command{
	Use:   "apply -f FILE",
	Short: "Apply agent configuration from a YAML file",
	RunE: func(cmd *cobra.Command, args []string) error {
		file, _ := cmd.Flags().GetString("file")
		if file == "" {
			return fmt.Errorf("--file is required")
		}
		data, err := os.ReadFile(file)
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
		fmt.Fprintln(cmd.OutOrStdout(), "Config applied. Restart the agent pod to apply runtime changes.")
		return nil
	},
}

func init() {
	configApplyCmd.Flags().StringP("file", "f", "", "YAML config file")
	_ = configApplyCmd.MarkFlagRequired("file")
}

func applyConfigField(cfg *models.AgentConfig, key, value string) error {
	switch key {
	case "scrape_interval":
		cfg.ScrapeInterval = value
	case "prometheus_address":
		cfg.PrometheusAddress = value
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
