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
		out := cmd.OutOrStdout()
		printKVStyled(out, "status:", h["status"], statusStyle(h["status"]))
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
		out := cmd.OutOrStdout()
		printBanner(out)
		printSection(out, "Agent status")
		printKV(out, "Uptime:", st.Uptime)
		printKV(out, "Started:", st.StartedAt)
		printKV(out, "Scrapes:", fmt.Sprintf("%d", st.Scrapes))
		printKV(out, "Gate passed:", fmt.Sprintf("%d", st.GatePassed))
		printKV(out, "Gate dropped:", fmt.Sprintf("%d", st.GateDropped))
		printKV(out, "Gate observed:", fmt.Sprintf("%d", st.GateObserved))
		return nil
	})
}
