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

func runStats(cmd *cobra.Command, _ []string) error {
	if err := validateOutputFormat(); err != nil {
		return err
	}
	stats, err := fetchStats()
	if err != nil {
		return err
	}
	return writeOutput(cmd.OutOrStdout(), outputFormat, stats, func() error {
		out := cmd.OutOrStdout()
		printBanner(out)
		printSection(out, "Outcome metrics")
		printKV(out, "Predictions total:", fmt.Sprintf("%d", stats.PredictionsTotal))
		printKV(out, "Predictions pending:", fmt.Sprintf("%d", stats.PredictionsPending))
		printKV(out, "Predictions hit:", fmt.Sprintf("%d", stats.PredictionsHit))
		printKV(out, "Predictions missed:", fmt.Sprintf("%d", stats.PredictionsMissed))
		printKVStyled(out, "Prediction accuracy:", fmt.Sprintf("%.1f%%", stats.PredictionAccuracy*100), scoreStyle(stats.PredictionAccuracy))
		printKV(out, "Remediations total:", fmt.Sprintf("%d", stats.RemediationsTotal))
		printKV(out, "Remediations verified:", fmt.Sprintf("%d", stats.RemediationsVerified))
		printKV(out, "Remediations dry-run:", fmt.Sprintf("%d", stats.RemediationsDryRun))
		printKV(out, "Pending approvals:", fmt.Sprintf("%d", stats.RemediationsPending))
		return nil
	})
}
