package cli

import (
	"encoding/json"
	"fmt"

	"github.com/lohitkolluri/KubeWise/pkg/models"
	"github.com/spf13/cobra"
	yaml "gopkg.in/yaml.v3"
)

func init() {
	rootCmd.AddCommand(predictCmd)
}

var predictCmd = &cobra.Command{
	Use:     "predict",
	Aliases: []string{"predictions"},
	Short:   "Show active predictions",
	Long:    `Fetch and display predictions from the agent.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if err := validateOutputFormat(); err != nil {
			return err
		}
		body, _, err := agentGet("/api/v1/predictions")
		if err != nil {
			return err
		}

		var predictions []models.PredictionResult
		if err := json.Unmarshal(body, &predictions); err != nil {
			return fmt.Errorf("parsing predictions: %w", err)
		}

		switch outputFormat {
		case "json":
			enc := json.NewEncoder(cmd.OutOrStdout())
			enc.SetIndent("", "  ")
			return enc.Encode(predictions)
		case "yaml":
			enc := yaml.NewEncoder(cmd.OutOrStdout())
			enc.SetIndent(2)
			defer enc.Close()
			return enc.Encode(predictions)
		default:
			if len(predictions) == 0 {
				fmt.Fprintln(cmd.OutOrStdout(), "No active predictions.")
				return nil
			}
			fmt.Fprintf(cmd.OutOrStdout(), "%-12s %-24s %-10s %-10s %s\n",
				"TYPE", "ENTITY", "SCORE", "ETA(s)", "ACTION")
			fmt.Fprintln(cmd.OutOrStdout(), repeatLine(80))
			for _, p := range predictions {
				fmt.Fprintf(cmd.OutOrStdout(), "%-12s %-24s %-10.2f %-10.0f %s\n",
					trunc(p.Type, 10), trunc(p.Entity, 22), p.Score, p.ETASeconds, trunc(p.Action, 30))
			}
			return nil
		}
	},
}

func repeatLine(n int) string {
	b := make([]byte, n)
	for i := range b {
		b[i] = '-'
	}
	return string(b)
}

func trunc(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n-1] + "…"
}
