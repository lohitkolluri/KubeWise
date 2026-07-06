package cli

import (
	"encoding/json"
	"fmt"

	"github.com/spf13/cobra"
	yaml "gopkg.in/yaml.v3"
)

type agentStatus struct {
	Uptime       string  `json:"uptime" yaml:"uptime"`
	StartedAt    string  `json:"started_at" yaml:"started_at"`
	Scrapes      int64   `json:"scrapes" yaml:"scrapes"`
	GatePassed   uint64  `json:"gate_passed" yaml:"gate_passed"`
	GateDropped  uint64  `json:"gate_dropped" yaml:"gate_dropped"`
	GateObserved uint64  `json:"gate_observed" yaml:"gate_observed"`
}

func init() {
	rootCmd.AddCommand(statusCmd)
}

var statusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show agent status",
	Long:  `Connect to the KubeWise agent and display its current status.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if err := validateOutputFormat(); err != nil {
			return err
		}
		body, _, err := agentGet("/status")
		if err != nil {
			return err
		}

		var st agentStatus
		if err := json.Unmarshal(body, &st); err != nil {
			return fmt.Errorf("parsing status: %w", err)
		}

		switch outputFormat {
		case "json":
			enc := json.NewEncoder(cmd.OutOrStdout())
			enc.SetIndent("", "  ")
			return enc.Encode(st)
		case "yaml":
			enc := yaml.NewEncoder(cmd.OutOrStdout())
			enc.SetIndent(2)
			defer enc.Close()
			return enc.Encode(st)
		default:
			fmt.Fprintf(cmd.OutOrStdout(), "%-20s %s\n", "Uptime:", st.Uptime)
			fmt.Fprintf(cmd.OutOrStdout(), "%-20s %s\n", "Started At:", st.StartedAt)
			fmt.Fprintf(cmd.OutOrStdout(), "%-20s %d\n", "Scrapes:", st.Scrapes)
			fmt.Fprintf(cmd.OutOrStdout(), "%-20s %d\n", "Gate Passed:", st.GatePassed)
			fmt.Fprintf(cmd.OutOrStdout(), "%-20s %d\n", "Gate Dropped:", st.GateDropped)
			fmt.Fprintf(cmd.OutOrStdout(), "%-20s %d\n", "Gate Observed:", st.GateObserved)
			return nil
		}
	},
}
