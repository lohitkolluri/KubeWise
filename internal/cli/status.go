package cli

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/spf13/cobra"
	yaml "gopkg.in/yaml.v3"
)

type agentStatus struct {
	Uptime    string `json:"uptime" yaml:"uptime"`
	StartedAt string `json:"started_at" yaml:"started_at"`
	Scrapes   int64  `json:"scrapes" yaml:"scrapes"`
}

func init() {
	rootCmd.AddCommand(statusCmd)
}

var statusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show agent status",
	Long:  `Connect to the KubeWise agent and display its current status.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		base := resolveAgentURL()
		resp, err := http.Get(base + "/status")
		if err != nil {
			return fmt.Errorf("connecting to agent: %w", err)
		}
		defer resp.Body.Close()

		body, err := io.ReadAll(resp.Body)
		if err != nil {
			return fmt.Errorf("reading response: %w", err)
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
			return enc.Encode(st)
		default:
			fmt.Fprintf(cmd.OutOrStdout(), "%-20s %s\n", "Uptime:", st.Uptime)
			fmt.Fprintf(cmd.OutOrStdout(), "%-20s %s\n", "Started At:", st.StartedAt)
			fmt.Fprintf(cmd.OutOrStdout(), "%-20s %d\n", "Scrapes:", st.Scrapes)
			return nil
		}
	},
}

var resolveAgentURL = func() string {
	port := 8080
	return fmt.Sprintf("http://localhost:%d", port)
}

func getJSON(url string, target interface{}) error {
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	return json.NewDecoder(resp.Body).Decode(target)
}
