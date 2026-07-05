package cli

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

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
		base := resolveAgentURL()
		resp, err := http.Get(base + "/api/v1/predictions")
		if err != nil {
			return fmt.Errorf("connecting to agent: %w", err)
		}
		defer resp.Body.Close()

		body, err := io.ReadAll(resp.Body)
		if err != nil {
			return fmt.Errorf("reading response: %w", err)
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
			return enc.Encode(predictions)
		default:
			if len(predictions) == 0 {
				fmt.Fprintln(cmd.OutOrStdout(), "No active predictions.")
				return nil
			}
			fmt.Fprintf(cmd.OutOrStdout(), "%-20s %-10s %-12s %-10s %s\n",
				"TYPE", "ENTITY", "CONFIDENCE", "ETA", "ACTION")
			fmt.Fprintln(cmd.OutOrStdout(), strings.Repeat("-", 60))
			for _, p := range predictions {
				fmt.Fprintf(cmd.OutOrStdout(), "%-20s %-10s %-12.2f %-10s %s\n",
					trunc(p.Type, 18), trunc(p.Entity, 8), p.Confidence,
					p.Action, p.Type)
			}
			return nil
		}
	},
}

func trunc(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n-1] + "…"
}

func formatETA(p models.PredictionResult) string {
	if p.Timestamp.IsZero() {
		return "-"
	}
	return p.Timestamp.Format("15:04:05")
}
