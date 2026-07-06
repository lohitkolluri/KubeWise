package cli

import (
	"encoding/json"
	"fmt"

	"github.com/lohitkolluri/KubeWise/pkg/models"
	"github.com/spf13/cobra"
	yaml "gopkg.in/yaml.v3"
)

func init() {
	rootCmd.AddCommand(configCmd)
}

var configCmd = &cobra.Command{
	Use:   "config",
	Short: "Show agent configuration",
	Long:  `Fetch and display the agent's current configuration.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if err := validateOutputFormat(); err != nil {
			return err
		}
		body, _, err := agentGet("/api/v1/config")
		if err != nil {
			return err
		}

		var msg map[string]string
		if err := json.Unmarshal(body, &msg); err == nil {
			if m, ok := msg["message"]; ok {
				fmt.Fprintln(cmd.OutOrStdout(), m)
				return nil
			}
		}

		var cfg models.AgentConfig
		if err := json.Unmarshal(body, &cfg); err != nil {
			return fmt.Errorf("parsing config: %w", err)
		}

		switch outputFormat {
		case "json":
			enc := json.NewEncoder(cmd.OutOrStdout())
			enc.SetIndent("", "  ")
			return enc.Encode(cfg)
		case "yaml":
			enc := yaml.NewEncoder(cmd.OutOrStdout())
			enc.SetIndent(2)
			defer enc.Close()
			return enc.Encode(cfg)
		default:
			fmt.Fprintf(cmd.OutOrStdout(), "%-25s %s\n", "Scrape Interval:", cfg.ScrapeInterval)
			fmt.Fprintf(cmd.OutOrStdout(), "%-25s %s\n", "Prometheus:", cfg.PrometheusAddress)
			fmt.Fprintf(cmd.OutOrStdout(), "%-25s %s\n", "LLM Provider:", cfg.LLMProvider)
			fmt.Fprintf(cmd.OutOrStdout(), "%-25s %s\n", "LLM Model:", cfg.LLMModel)
			fmt.Fprintf(cmd.OutOrStdout(), "%-25s %s\n", "Remediation:", cfg.Remediation.Mode)
			return nil
		}
	},
}
