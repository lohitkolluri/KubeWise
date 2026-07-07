package cli

import (
	"fmt"

	"github.com/spf13/cobra"
)

func init() {
	rootCmd.AddCommand(statusCmd)
	statusCmd.AddCommand(healthCmd)
}

var statusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show agent status",
	Long:  `Connect to the KubeWise agent and display its current status.`,
	RunE:  runStatus,
}

var healthCmd = &cobra.Command{
	Use:   "health",
	Short: "Check agent health endpoint",
	RunE: func(cmd *cobra.Command, args []string) error {
		h, err := fetchHealth()
		if err != nil {
			return err
		}
		if outputFormat == "json" {
			return writeOutput(cmd.OutOrStdout(), "json", h, nil)
		}
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "status: %s\n", h["status"])
		return nil
	},
}

func runStatus(cmd *cobra.Command, args []string) error {
	if err := validateOutputFormat(); err != nil {
		return err
	}
	st, err := fetchStatus()
	if err != nil {
		return err
	}
	return writeOutput(cmd.OutOrStdout(), outputFormat, st, func() error {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-20s %s\n", "Uptime:", st.Uptime)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-20s %s\n", "Started At:", st.StartedAt)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-20s %d\n", "Scrapes:", st.Scrapes)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-20s %d\n", "Gate Passed:", st.GatePassed)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-20s %d\n", "Gate Dropped:", st.GateDropped)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-20s %d\n", "Gate Observed:", st.GateObserved)
		return nil
	})
}
