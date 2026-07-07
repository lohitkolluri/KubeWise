package cli

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

var eventsSince time.Duration

func init() {
	eventsCmd.Flags().DurationVar(&eventsSince, "since", 30*time.Minute, "only show events newer than this")
	rootCmd.AddCommand(eventsCmd)
}

var eventsCmd = &cobra.Command{
	Use:   "events",
	Short: "Show recent Kubernetes events in the agent namespace",
	RunE: func(cmd *cobra.Command, args []string) error {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		kc, err := newKubeClient()
		if err != nil {
			return err
		}
		list, err := kc.GetEvents(ctx, agentNS, eventsSince)
		if err != nil {
			return err
		}
		if len(list.Items) == 0 {
			_, _ = fmt.Fprintf(cmd.OutOrStdout(), "No events in %s (since %s)\n", agentNS, eventsSince)
			return nil
		}
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-10s %-12s %-8s %s\n", "TYPE", "OBJECT", "COUNT", "MESSAGE")
		_, _ = fmt.Fprintln(cmd.OutOrStdout(), strings.Repeat("-", 80))
		for _, e := range list.Items {
			obj := e.InvolvedObject.Kind + "/" + e.InvolvedObject.Name
			_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-10s %-12s %-8d %s\n",
				trunc(e.Type, 10), trunc(obj, 12), e.Count, trunc(e.Message, 50))
		}
		return nil
	},
}
