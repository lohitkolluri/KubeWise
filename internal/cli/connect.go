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
	Long: `Ping the agent health and status endpoints. Connect first:

  kwctl up
  kwctl connect`,
	RunE: func(cmd *cobra.Command, _ []string) error {
		out := cmd.OutOrStdout()
		printBanner(out)
		printSection(out, "Agent connectivity")
		printKV(out, "URL:", resolveAgentURL())

		h, err := fetchHealth()
		if err != nil {
			return fmt.Errorf("health check failed: %w", err)
		}
		printKVStyled(out, "Health:", h["status"], statusStyle(h["status"]))

		st, err := fetchStatus()
		if err != nil {
			return fmt.Errorf("status check failed: %w", err)
		}
		printKV(out, "Uptime:", st.Uptime)
		printKV(out, "Scrapes:", fmt.Sprintf("%d", st.Scrapes))
		printOK(out, "Connected successfully")
		printNextSteps(out,
			"kwctl ui      # control center",
			"kwctl status  # quick summary",
		)
		return nil
	},
}

var versionCmd = &cobra.Command{
	Use:   "version",
	Short: "Print kwctl version",
	Run: func(cmd *cobra.Command, _ []string) {
		out := cmd.OutOrStdout()
		if writerTTY(out) {
			_, _ = fmt.Fprintln(out, logoStyle.Render("KubeWise")+mutedStyle.Render(" kwctl ")+brandStyle.Render(version.Version))
			return
		}
		_, _ = fmt.Fprintf(out, "kwctl %s\n", version.Version)
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
