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
	RunE: func(cmd *cobra.Command, _ []string) error {
		pf, err := loadProfileFile()
		if err != nil {
			return err
		}
		p := pf.Profiles[pf.Current]
		path, _ := profilePath()
		out := cmd.OutOrStdout()
		printBanner(out)
		printSection(out, "Active profile")
		printKV(out, "File:", path)
		printKV(out, "Active:", pf.Current)
		printKV(out, "Agent URL:", p.AgentURL)
		printKV(out, "Namespace:", p.AgentNamespace)
		printKV(out, "Service:", p.AgentService)
		printKV(out, "Output:", p.Output)
		printKV(out, "Timeout:", fmt.Sprintf("%ds", p.TimeoutSeconds))
		if p.APIToken != "" {
			printKVStyled(out, "API Token:", "(set)", successStyle)
		}
		return nil
	},
}

var profileListCmd = &cobra.Command{
	Use:   "list",
	Short: "List profile names",
	RunE: func(cmd *cobra.Command, _ []string) error {
		pf, err := loadProfileFile()
		if err != nil {
			return err
		}
		out := cmd.OutOrStdout()
		printSection(out, "Profiles")
		for name := range pf.Profiles {
			marker := " "
			style := subtleStyle
			if name == pf.Current {
				marker = "▸"
				style = keyStyle
			}
			if writerTTY(out) {
				_, _ = fmt.Fprintln(out, style.Render(marker+" "+name))
			} else {
				_, _ = fmt.Fprintf(out, "%s %s\n", marker, name)
			}
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
		if err := saveProfileFile(pf); err != nil {
			return err
		}
		printOK(cmd.OutOrStdout(), "Switched to profile %q", args[0])
		return nil
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
		out := cmd.OutOrStdout()
		for _, arg := range args {
			key, value, err := parseSetArg(arg)
			if err != nil {
				return err
			}
			if err := setProfileField(name, key, value); err != nil {
				return err
			}
			printKV(out, key+":", maskIfToken(key, value))
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
