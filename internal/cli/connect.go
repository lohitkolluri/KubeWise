package cli

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/lohitkolluri/KubeWise/internal/version"
)

func init() {
	rootCmd.AddCommand(connectCmd, versionCmd)
	rootCmd.AddCommand(completionCmd)
}

var connectCmd = &cobra.Command{
	Use:   "connect",
	Short: "Verify agent connectivity",
	Long: `Ping the agent health and status endpoints. Use this after port-forwarding:

  kubectl -n kubewise port-forward svc/kubewise-agent 8080:8080
  kwctl connect`,
	RunE: func(cmd *cobra.Command, args []string) error {
		url := resolveAgentURL()
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Agent URL: %s\n", url)
		h, err := fetchHealth()
		if err != nil {
			return fmt.Errorf("health check failed: %w", err)
		}
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Health:    %s\n", h["status"])
		st, err := fetchStatus()
		if err != nil {
			return fmt.Errorf("status check failed: %w", err)
		}
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Uptime:    %s\n", st.Uptime)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Scrapes:   %d\n", st.Scrapes)
		_, _ = fmt.Fprintln(cmd.OutOrStdout(), "Connected successfully.")
		return nil
	},
}

var versionCmd = &cobra.Command{
	Use:   "version",
	Short: "Print kwctl version",
	Run: func(cmd *cobra.Command, args []string) {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "kwctl %s\n", version.Version)
	},
}

var completionCmd = &cobra.Command{
	Use:   "completion [bash|zsh|fish|powershell]",
	Short: "Generate shell completion script",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		switch args[0] {
		case "bash":
			return rootCmd.GenBashCompletion(cmd.OutOrStdout())
		case "zsh":
			return rootCmd.GenZshCompletion(cmd.OutOrStdout())
		case "fish":
			return rootCmd.GenFishCompletion(cmd.OutOrStdout(), true)
		case "powershell":
			return rootCmd.GenPowerShellCompletionWithDesc(cmd.OutOrStdout())
		default:
			return fmt.Errorf("unsupported shell %q", args[0])
		}
	},
}
