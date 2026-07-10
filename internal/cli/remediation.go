package cli

import (
	"fmt"

	"github.com/spf13/cobra"
)

var auditLimit int
var auditStatus string
var auditSince string
var auditID string

func init() {
	remediationCmd.Flags().IntVarP(&auditLimit, "limit", "l", 20, "max records")
	remediationCmd.Flags().StringVar(&auditStatus, "status", "", "filter by status (pending, executed, rejected, failed, dry-run, verified, verify_failed, escalated)")
	remediationCmd.Flags().StringVar(&auditSince, "since", "", "filter by created_at since RFC3339 timestamp")
	remediationCmd.Flags().StringVar(&auditID, "id", "", "fetch a single audit record by exact ID")
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
		records, err := fetchAuditFiltered(auditLimit, auditStatus, auditSince, auditID)
		if err != nil {
			return err
		}
		return writeOutput(cmd.OutOrStdout(), outputFormat, records, func() error {
			if len(records) == 0 {
				printEmpty(cmd.OutOrStdout(), "No remediation records.")
				return nil
			}
			rows := make([][]string, 0, len(records))
			for _, r := range records {
				action := fmt.Sprintf("%s/%s", r.Plan.Action.Type, r.Plan.Action.Target)
				rows = append(rows, []string{
					trunc(r.ID, 28),
					string(r.Status),
					trunc(action, 20),
					string(r.RiskTier),
					trunc(r.Reason, 30),
				})
			}
			printDataTable(cmd.OutOrStdout(), []string{"ID", "STATUS", "ACTION", "TIER", "REASON"}, rows)
			return nil
		})
	},
}
