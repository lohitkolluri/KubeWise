package cli

import (
	"fmt"
	"strings"

	"github.com/spf13/cobra"
)

var auditLimit int

func init() {
	remediationCmd.Flags().IntVarP(&auditLimit, "limit", "l", 20, "max records")
	rootCmd.AddCommand(remediationCmd)
}

var remediationCmd = &cobra.Command{
	Use:     "remediation",
	Aliases: []string{"audit", "remediations"},
	Short:   "Show remediation audit log",
	Long:    `Fetch and display remediation audit records from the agent.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if err := validateOutputFormat(); err != nil {
			return err
		}
		records, err := fetchAudit(auditLimit)
		if err != nil {
			return err
		}
		return writeOutput(cmd.OutOrStdout(), outputFormat, records, func() error {
			if len(records) == 0 {
				fmt.Fprintln(cmd.OutOrStdout(), "No remediation records.")
				return nil
			}
			fmt.Fprintf(cmd.OutOrStdout(), "%-30s %-10s %-20s %-8s %s\n",
				"ID", "STATUS", "ACTION", "TIER", "REASON")
			fmt.Fprintln(cmd.OutOrStdout(), strings.Repeat("-", 80))
			for _, r := range records {
				action := fmt.Sprintf("%s/%s", r.Plan.Action.Type, r.Plan.Action.Target)
				fmt.Fprintf(cmd.OutOrStdout(), "%-30s %-10s %-20s %-8s %s\n",
					trunc(r.ID, 28), string(r.Status), trunc(action, 18), string(r.RiskTier), trunc(r.Reason, 30))
			}
			return nil
		})
	},
}
