package cli

import (
	"bufio"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"
)

func init() {
	authCmd.AddCommand(authShowCmd, authSetTokenCmd, authUnsetTokenCmd)
	rootCmd.AddCommand(authCmd)

	authSetTokenCmd.Flags().BoolVar(&authFromStdin, "stdin", false, "read token from stdin (recommended to avoid shell history)")
	authSetTokenCmd.Flags().StringVar(&authFromEnv, "from-env", "", "read token from an environment variable (e.g. KUBEWISE_API_TOKEN)")
}

var authCmd = &cobra.Command{
	Use:   "auth",
	Short: "Manage kwctl authentication (API token)",
	Long:  `Manage the API token used to authenticate to the agent HTTP API (stored in your kwctl profile).`,
}

var authShowCmd = &cobra.Command{
	Use:   "show",
	Short: "Show whether an API token is configured",
	RunE: func(cmd *cobra.Command, _ []string) error {
		p := activeProfile()
		path, _ := profilePath()
		fmt.Fprintf(cmd.OutOrStdout(), "File:   %s\n", path)
		fmt.Fprintf(cmd.OutOrStdout(), "Agent:  %s\n", p.AgentURL)
		if p.APIToken != "" {
			fmt.Fprintln(cmd.OutOrStdout(), "Token:  (set)")
		} else {
			fmt.Fprintln(cmd.OutOrStdout(), "Token:  (not set)")
			fmt.Fprintln(cmd.OutOrStdout(), "\nSet one with:")
			fmt.Fprintln(cmd.OutOrStdout(), "  kwctl auth set-token --stdin")
		}
		return nil
	},
}

var (
	authFromStdin bool
	authFromEnv   string
)

var authSetTokenCmd = &cobra.Command{
	Use:   "set-token [TOKEN]",
	Short: "Set API token in the active profile",
	Long: `Stores the agent API token in your local kwctl profile (~/.config/kwctl/config.yaml).

Examples:
  kwctl auth set-token --stdin
  kwctl auth set-token --from-env KUBEWISE_API_TOKEN
  kwctl auth set-token my-token`,
	Args: cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		var tok string
		switch {
		case authFromStdin:
			in := bufio.NewReader(cmd.InOrStdin())
			b, err := in.ReadString('\n')
			if err != nil && strings.TrimSpace(b) == "" {
				return fmt.Errorf("read token from stdin: %w", err)
			}
			tok = strings.TrimSpace(b)
		case authFromEnv != "":
			tok = strings.TrimSpace(os.Getenv(authFromEnv))
		case len(args) == 1:
			tok = strings.TrimSpace(args[0])
		default:
			return fmt.Errorf("provide TOKEN, or use --stdin / --from-env")
		}
		if tok == "" {
			return fmt.Errorf("token must not be empty")
		}
		if err := setProfileField(profileName, "api-token", tok); err != nil {
			return err
		}
		fmt.Fprintln(cmd.OutOrStdout(), "API token saved in profile.")
		return nil
	},
}

var authUnsetTokenCmd = &cobra.Command{
	Use:   "unset-token",
	Short: "Remove API token from the active profile",
	RunE: func(cmd *cobra.Command, _ []string) error {
		if err := setProfileField(profileName, "api-token", ""); err != nil {
			return err
		}
		fmt.Fprintln(cmd.OutOrStdout(), "API token removed from profile.")
		return nil
	},
}
