package cli

import (
	"fmt"
	"strings"

	"github.com/spf13/cobra"
)

var (
	approvalsLimit int
	approvalReason string
)

func init() {
	approvalsCmd.Flags().IntVarP(&approvalsLimit, "limit", "l", 20, "max records")
	rootCmd.AddCommand(approvalsCmd)

	approveCmd.Flags().StringVar(&approvalReason, "reason", "", "optional reason for approval (audit)")
	rejectCmd.Flags().StringVar(&approvalReason, "reason", "rejected via kwctl", "reason for rejection (required)")

	approvalsCmd.AddCommand(approveCmd, rejectCmd)
}

var approvalsCmd = &cobra.Command{
	Use:   "approvals",
	Short: "List and manage pending remediation approvals",
	Long:  `Fetch pending remediation approvals (e.g. Tier-3) and approve or reject them.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if err := validateOutputFormat(); err != nil {
			return err
		}
		records, err := fetchApprovals(approvalsLimit)
		if err != nil {
			return err
		}
		return writeOutput(cmd.OutOrStdout(), outputFormat, records, func() error {
			if len(records) == 0 {
				printEmpty(cmd.OutOrStdout(), "No pending approvals.")
				return nil
			}
			rows := make([][]string, 0, len(records))
			for _, r := range records {
				action := fmt.Sprintf("%s/%s", r.Plan.Action.Type, r.Plan.Action.Target)
				rows = append(rows, []string{
					trunc(r.ID, 28),
					string(r.RiskTier),
					trunc(action, 22),
					trunc(r.Reason, 40),
				})
			}
			printDataTable(cmd.OutOrStdout(), []string{"ID", "TIER", "ACTION", "REASON"}, rows)
			return nil
		})
	},
}

var approveCmd = &cobra.Command{
	Use:   "approve <id>",
	Short: "Approve and execute a pending remediation",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		id := strings.TrimSpace(args[0])
		if id == "" {
			return fmt.Errorf("missing id")
		}
		_ = approvalReason
		if err := approveRemediation(id); err != nil {
			return err
		}
		printOK(cmd.OutOrStdout(), "Approved and executed: %s", id)
		return nil
	},
}

var rejectCmd = &cobra.Command{
	Use:   "reject <id>",
	Short: "Reject a pending remediation",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		id := strings.TrimSpace(args[0])
		if id == "" {
			return fmt.Errorf("missing id")
		}
		if strings.TrimSpace(approvalReason) == "" {
			return fmt.Errorf("reason must not be empty")
		}
		if err := rejectRemediation(id, approvalReason); err != nil {
			return err
		}
		printWarn(cmd.OutOrStdout(), "Rejected: %s", id)
		return nil
	},
}
