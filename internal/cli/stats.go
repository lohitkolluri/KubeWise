package cli

import (
	"fmt"

	"github.com/spf13/cobra"
)

func init() {
	rootCmd.AddCommand(statsCmd)
}

var statsCmd = &cobra.Command{
	Use:   "stats",
	Short: "Show prediction accuracy and remediation outcome metrics",
	RunE:  runStats,
}

func runStats(cmd *cobra.Command, args []string) error {
	if err := validateOutputFormat(); err != nil {
		return err
	}
	stats, err := fetchStats()
	if err != nil {
		return err
	}
	return writeOutput(cmd.OutOrStdout(), outputFormat, stats, func() error {
		fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Predictions total:", stats.PredictionsTotal)
		fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Predictions pending:", stats.PredictionsPending)
		fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Predictions hit:", stats.PredictionsHit)
		fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Predictions missed:", stats.PredictionsMissed)
		fmt.Fprintf(cmd.OutOrStdout(), "%-28s %.1f%%\n", "Prediction accuracy:", stats.PredictionAccuracy*100)
		fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Remediations total:", stats.RemediationsTotal)
		fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Remediations verified:", stats.RemediationsVerified)
		fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Remediations dry-run:", stats.RemediationsDryRun)
		fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Pending approvals:", stats.RemediationsPending)
		return nil
	})
}
