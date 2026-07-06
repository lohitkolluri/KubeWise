package cli

import (
	"fmt"
	"os"
	"time"

	"github.com/spf13/cobra"
)

const cliVersion = "0.2.0"

var (
	kubeconfig   string
	contextName  string
	agentNS      string
	agentSvc     string
	outputFormat string
	profileName  string
)

var rootCmd = &cobra.Command{
	Use:   "kwctl",
	Short: "KubeWise — AI SRE CLI for Kubernetes",
	Long: `KubeWise control center CLI. Connect to a running agent to monitor,
predict failures, review anomalies, manage config, and tail logs.

Quick start:
  kwctl ui                   # full interactive control center (recommended)
  kwctl connect              # verify agent connectivity
  kwctl logs -f              # stream agent pod logs

Profiles: ~/.config/kwctl/config.yaml`,
	SilenceErrors: true,
	SilenceUsage:  true,
	RunE: func(cmd *cobra.Command, args []string) error {
		if isTerminal(os.Stdout) {
			return runControlCenter(2 * time.Second)
		}
		return cmd.Help()
	},
}

func Execute() {
	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func init() {
	cobra.OnInitialize(applyProfileDefaults)

	rootCmd.PersistentFlags().StringVarP(&kubeconfig, "kubeconfig", "", "", "path to kubeconfig file")
	rootCmd.PersistentFlags().StringVarP(&contextName, "context", "", "", "kubeconfig context")
	rootCmd.PersistentFlags().StringVarP(&agentNS, "agent-namespace", "n", "kubewise", "agent namespace")
	rootCmd.PersistentFlags().StringVarP(&agentSvc, "agent-service", "s", "kubewise-agent", "agent service name")
	rootCmd.PersistentFlags().StringVarP(&outputFormat, "output", "o", "table", "output format: table, json, yaml")
	rootCmd.PersistentFlags().StringVar(&profileName, "profile", "", "kwctl profile name (from ~/.config/kwctl/config.yaml)")
	rootCmd.PersistentFlags().StringVar(&agentURL, "agent-url", "", "agent base URL (overrides profile)")
	rootCmd.PersistentFlags().IntVar(&httpTimeout, "timeout", 0, "HTTP timeout in seconds")
}

func isTerminal(f *os.File) bool {
	info, err := f.Stat()
	if err != nil {
		return false
	}
	return (info.Mode() & os.ModeCharDevice) != 0
}
