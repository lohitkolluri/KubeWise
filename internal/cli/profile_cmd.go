package cli

import (
	"fmt"
	"strings"

	"github.com/spf13/cobra"
)

func init() {
	profileCmd.AddCommand(profileShowCmd, profileSetCmd, profileListCmd, profileUseCmd)
	rootCmd.AddCommand(profileCmd)
}

var profileCmd = &cobra.Command{
	Use:   "profile",
	Short: "Manage local kwctl client profiles",
	Long:  `Profiles store agent URL, namespace, output defaults, and credentials in ~/.config/kwctl/config.yaml.`,
}

var profileShowCmd = &cobra.Command{
	Use:   "show",
	Short: "Show active profile",
	RunE: func(cmd *cobra.Command, args []string) error {
		pf, err := loadProfileFile()
		if err != nil {
			return err
		}
		p := pf.Profiles[pf.Current]
		path, _ := profilePath()
		fmt.Fprintf(cmd.OutOrStdout(), "File:        %s\n", path)
		fmt.Fprintf(cmd.OutOrStdout(), "Active:      %s\n", pf.Current)
		fmt.Fprintf(cmd.OutOrStdout(), "Agent URL:   %s\n", p.AgentURL)
		fmt.Fprintf(cmd.OutOrStdout(), "Namespace:   %s\n", p.AgentNamespace)
		fmt.Fprintf(cmd.OutOrStdout(), "Service:     %s\n", p.AgentService)
		fmt.Fprintf(cmd.OutOrStdout(), "Output:      %s\n", p.Output)
		fmt.Fprintf(cmd.OutOrStdout(), "Timeout:     %ds\n", p.TimeoutSeconds)
		if p.APIToken != "" {
			fmt.Fprintln(cmd.OutOrStdout(), "API Token:   (set)")
		}
		return nil
	},
}

var profileListCmd = &cobra.Command{
	Use:   "list",
	Short: "List profile names",
	RunE: func(cmd *cobra.Command, args []string) error {
		pf, err := loadProfileFile()
		if err != nil {
			return err
		}
		for name := range pf.Profiles {
			marker := " "
			if name == pf.Current {
				marker = "*"
			}
			fmt.Fprintf(cmd.OutOrStdout(), "%s %s\n", marker, name)
		}
		return nil
	},
}

var profileUseCmd = &cobra.Command{
	Use:   "use NAME",
	Short: "Switch active profile",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		pf, err := loadProfileFile()
		if err != nil {
			return err
		}
		if _, ok := pf.Profiles[args[0]]; !ok {
			return fmt.Errorf("profile %q not found", args[0])
		}
		pf.Current = args[0]
		return saveProfileFile(pf)
	},
}

var profileSetCmd = &cobra.Command{
	Use:   "set KEY=VALUE [KEY=VALUE...]",
	Short: "Set profile fields (agent-url, output, timeout, api-token, ...)",
	Long: `Keys: agent-url, agent-namespace, agent-service, api-token, output, timeout, kubeconfig, context

Example:
  kwctl profile set agent-url=http://localhost:8080 output=json`,
	Args: cobra.MinimumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		name := profileName
		if name == "" {
			pf, _ := loadProfileFile()
			if pf != nil {
				name = pf.Current
			}
		}
		for _, arg := range args {
			key, value, err := parseSetArg(arg)
			if err != nil {
				return err
			}
			if err := setProfileField(name, key, value); err != nil {
				return err
			}
			fmt.Fprintf(cmd.OutOrStdout(), "set %s=%s\n", key, maskIfToken(key, value))
		}
		return nil
	},
}

func maskIfToken(key, value string) string {
	if strings.Contains(key, "token") && value != "" {
		return "(hidden)"
	}
	return value
}
