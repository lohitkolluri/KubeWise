package cli

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/lohitkolluri/KubeWise/pkg/models"
	"github.com/spf13/cobra"
	yaml "gopkg.in/yaml.v3"
)

func init() {
	rootCmd.AddCommand(anomaliesCmd)
}

var anomaliesCmd = &cobra.Command{
	Use:   "anomalies",
	Short: "Show detected anomalies",
	Long:  `Fetch and display anomaly records from the agent.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if err := validateOutputFormat(); err != nil {
			return err
		}
		body, _, err := agentGet("/api/v1/anomalies")
		if err != nil {
			return err
		}

		var records []models.AnomalyRecord
		if err := json.Unmarshal(body, &records); err != nil {
			return fmt.Errorf("parsing anomalies: %w", err)
		}

		switch outputFormat {
		case "json":
			enc := json.NewEncoder(cmd.OutOrStdout())
			enc.SetIndent("", "  ")
			return enc.Encode(records)
		case "yaml":
			enc := yaml.NewEncoder(cmd.OutOrStdout())
			enc.SetIndent(2)
			defer enc.Close()
			return enc.Encode(records)
		default:
			if len(records) == 0 {
				fmt.Fprintln(cmd.OutOrStdout(), "No anomalies.")
				return nil
			}
			fmt.Fprintf(cmd.OutOrStdout(), "%-12s %-24s %-10s %-8s %s\n",
				"TYPE", "ENTITY", "SCORE", "STATUS", "METRIC")
			fmt.Fprintln(cmd.OutOrStdout(), strings.Repeat("-", 80))
			for _, r := range records {
				typ := r.Pattern
				if typ == "" {
					typ = "statistical"
				}
				fmt.Fprintf(cmd.OutOrStdout(), "%-12s %-24s %-10.2f %-8s %s\n",
					trunc(typ, 10), trunc(r.Entity, 22), r.Score, string(r.Status), trunc(r.MetricName, 30))
			}
			return nil
		}
	},
}
