package cli

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var (
	kubeconfig    string
	contextName   string
	agentNS       string
	agentSvc      string
	outputFormat  string
)

var rootCmd = &cobra.Command{
	Use:   "kwctl",
	Short: "KubeWise v2 — AI SRE for Kubernetes",
	Long: `KubeWise agent CLI. Connects to a running KubeWise agent
to inspect predictions, anomalies, and configuration.`,
	SilenceErrors: true,
	SilenceUsage:  true,
}

func Execute() {
	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func init() {
	rootCmd.PersistentFlags().StringVarP(&kubeconfig, "kubeconfig", "", "", "path to kubeconfig file")
	rootCmd.PersistentFlags().StringVarP(&contextName, "context", "", "", "kubeconfig context")
	rootCmd.PersistentFlags().StringVarP(&agentNS, "agent-namespace", "n", "kubewise", "agent namespace")
	rootCmd.PersistentFlags().StringVarP(&agentSvc, "agent-service", "s", "kubewise-agent", "agent service name")
	rootCmd.PersistentFlags().StringVarP(&outputFormat, "output", "o", "table", "output format: table, json, yaml")
}
