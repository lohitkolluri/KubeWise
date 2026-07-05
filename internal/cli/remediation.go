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
	rootCmd.AddCommand(remediationCmd)
}

var remediationCmd = &cobra.Command{
	Use:   "remediation",
	Short: "Show recent remediation actions",
	Long:  `Fetch and display remediation audit records from the agent.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		base := resolveAgentURL()
		resp, err := http.Get(base + "/api/v1/audit")
		if err != nil {
			return fmt.Errorf("connecting to agent: %w", err)
		}
		defer resp.Body.Close()

		body, err := io.ReadAll(resp.Body)
		if err != nil {
			return fmt.Errorf("reading response: %w", err)
		}

		var records []models.AuditRecord
		if err := json.Unmarshal(body, &records); err != nil {
			return fmt.Errorf("parsing audit records: %w", err)
		}

		switch outputFormat {
		case "json":
			enc := json.NewEncoder(cmd.OutOrStdout())
			enc.SetIndent("", "  ")
			return enc.Encode(records)
		case "yaml":
			enc := yaml.NewEncoder(cmd.OutOrStdout())
			enc.SetIndent(2)
			return enc.Encode(records)
		default:
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
		}
	},
}
