package cli

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/spf13/cobra"
)

var (
	logsFollow    bool
	logsTail      int64
	logsContainer string
)

func init() {
	logsCmd.Flags().BoolVarP(&logsFollow, "follow", "f", false, "stream logs")
	logsCmd.Flags().Int64Var(&logsTail, "tail", 100, "number of lines to show")
	logsCmd.Flags().StringVar(&logsContainer, "container", "agent", "container name")
	rootCmd.AddCommand(logsCmd)
	agentCmd.AddCommand(agentRestartCmd)
	rootCmd.AddCommand(agentCmd)
}

var logsCmd = &cobra.Command{
	Use:   "logs",
	Short: "Tail agent pod logs",
	Long: `Fetch logs from the KubeWise agent pod in the configured namespace.

Requires kubectl/kubeconfig access to the cluster.`,
	RunE: runLogs,
}

var agentCmd = &cobra.Command{
	Use:   "agent",
	Short: "Manage the KubeWise agent deployment",
}

var agentRestartCmd = &cobra.Command{
	Use:   "restart",
	Short: "Rolling restart of the agent deployment",
	Long:  `Triggers a kubectl-style rolling restart. Use after config changes to pick up new settings.`,
	RunE: func(cmd *cobra.Command, _ []string) error {
		if err := restartAgentDeployment(); err != nil {
			return err
		}
		printOK(cmd.OutOrStdout(), "Restarted deployment %s/%s", agentNS, agentSvc)
		return nil
	},
}

func runLogs(cmd *cobra.Command, _ []string) error {
	out := cmd.OutOrStdout()
	if logsFollow {
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		if writerTTY(out) {
			_, _ = fmt.Fprintln(out, mutedStyle.Render(fmt.Sprintf("Streaming logs from %s/%s (ctrl+c to stop)…", agentNS, agentSvc)))
			_, _ = fmt.Fprintln(out)
		} else {
			_, _ = fmt.Fprintf(out, "Streaming logs from %s/%s (ctrl+c to stop)…\n\n", agentNS, agentSvc)
		}
		return streamAgentLogs(ctx, logsTail, func(line string) {
			if writerTTY(out) {
				_, _ = fmt.Fprintln(out, colorizeLogLine(line))
			} else {
				_, _ = fmt.Fprintln(out, line)
			}
		})
	}
	text, err := fetchAgentLogs(logsTail)
	if err != nil {
		return err
	}
	if writerTTY(out) {
		_, _ = fmt.Fprint(out, colorizeLogs(text))
	} else {
		_, _ = fmt.Fprint(out, text)
	}
	return nil
}
