package cli

import (
	"context"
	"fmt"
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
	RunE: func(cmd *cobra.Command, _ []string) error {
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
		out := cmd.OutOrStdout()
		if len(list.Items) == 0 {
			printEmpty(out, fmt.Sprintf("No events in %s (since %s)", agentNS, eventsSince))
			return nil
		}
		rows := make([][]string, 0, len(list.Items))
		for _, e := range list.Items {
			obj := e.InvolvedObject.Kind + "/" + e.InvolvedObject.Name
			rows = append(rows, []string{
				trunc(e.Type, 10),
				trunc(obj, 12),
				fmt.Sprintf("%d", e.Count),
				trunc(e.Message, 50),
			})
		}
		printDataTable(out, []string{"TYPE", "OBJECT", "COUNT", "MESSAGE"}, rows)
		return nil
	},
}
