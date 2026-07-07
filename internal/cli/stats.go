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
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Predictions total:", stats.PredictionsTotal)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Predictions pending:", stats.PredictionsPending)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Predictions hit:", stats.PredictionsHit)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Predictions missed:", stats.PredictionsMissed)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-28s %.1f%%\n", "Prediction accuracy:", stats.PredictionAccuracy*100)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Remediations total:", stats.RemediationsTotal)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Remediations verified:", stats.RemediationsVerified)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Remediations dry-run:", stats.RemediationsDryRun)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-28s %d\n", "Pending approvals:", stats.RemediationsPending)
		return nil
	})
}
