package cli

import (
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
	"github.com/spf13/cobra"
)

var (
	predictWatch     bool
	predictInterval  time.Duration
	predictNamespace string
)

func init() {
	predictCmd.Flags().BoolVarP(&predictWatch, "watch", "w", false, "watch for prediction updates")
	predictCmd.Flags().DurationVar(&predictInterval, "interval", 5*time.Second, "watch refresh interval")
	predictCmd.Flags().StringVar(&predictNamespace, "namespace", "", "filter by namespace")
	rootCmd.AddCommand(predictCmd)
}

var predictCmd = &cobra.Command{
	Use:     "predict",
	Aliases: []string{"predictions"},
	Short:   "Show active predictions",
	Long:    `Fetch and display predictions from the agent. Use --watch to tail updates.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if err := validateOutputFormat(); err != nil {
			return err
		}
		if predictWatch {
			return runPredictWatch(cmd)
		}
		return renderPredictions(cmd, nil)
	},
}

func runPredictWatch(cmd *cobra.Command) error {
	seen := make(map[string]struct{})
	done := make(chan os.Signal, 1)
	signal.Notify(done, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(done)

	for {
		select {
		case <-done:
			return nil
		default:
		}
		preds, err := fetchPredictions()
		if err != nil {
			return err
		}
		preds = filterPredictions(preds)
		for _, p := range preds {
			key := p.Type + "|" + p.Entity + "|" + p.MetricName
			if _, ok := seen[key]; ok {
				continue
			}
			seen[key] = struct{}{}
			fmt.Fprintf(cmd.OutOrStdout(), "[%s] %s %s score=%.2f eta=%.0fs %s\n",
				time.Now().Format("15:04:05"), p.Type, p.Entity, p.Score, p.ETASeconds, p.Action)
		}
		select {
		case <-done:
			return nil
		case <-time.After(predictInterval):
		}
	}
}

func renderPredictions(cmd *cobra.Command, preds []models.PredictionResult) error {
	if preds == nil {
		var err error
		preds, err = fetchPredictions()
		if err != nil {
			return err
		}
	}
	preds = filterPredictions(preds)
	return writeOutput(cmd.OutOrStdout(), outputFormat, preds, func() error {
		if len(preds) == 0 {
			fmt.Fprintln(cmd.OutOrStdout(), "No active predictions.")
			return nil
		}
		fmt.Fprintf(cmd.OutOrStdout(), "%-12s %-24s %-10s %-10s %s\n",
			"TYPE", "ENTITY", "SCORE", "ETA(s)", "ACTION")
		fmt.Fprintln(cmd.OutOrStdout(), repeatLine(80))
		for _, p := range preds {
			fmt.Fprintf(cmd.OutOrStdout(), "%-12s %-24s %-10.2f %-10.0f %s\n",
				trunc(p.Type, 10), trunc(p.Entity, 22), p.Score, p.ETASeconds, trunc(p.Action, 30))
		}
		return nil
	})
}

func filterPredictions(preds []models.PredictionResult) []models.PredictionResult {
	if predictNamespace == "" {
		return preds
	}
	var out []models.PredictionResult
	for _, p := range preds {
		if p.Namespace == predictNamespace || strings.HasPrefix(p.Entity, predictNamespace+"/") {
			out = append(out, p)
		}
	}
	return out
}
