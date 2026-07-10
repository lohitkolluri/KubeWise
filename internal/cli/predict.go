package cli

import (
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/spf13/cobra"

	"github.com/lohitkolluri/KubeWise/pkg/models"
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
	RunE: func(cmd *cobra.Command, _ []string) error {
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
	out := cmd.OutOrStdout()
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
			line := fmt.Sprintf("[%s] %s %s score=%.2f eta=%.0fs %s",
				time.Now().Format("15:04:05"), p.Type, p.Entity, p.Score, p.ETASeconds, p.Action)
			if writerTTY(out) {
				_, _ = fmt.Fprintln(out, infoStyle.Render(line))
			} else {
				_, _ = fmt.Fprintln(out, line)
			}
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
			printEmpty(cmd.OutOrStdout(), "No active predictions.")
			return nil
		}
		rows := make([][]string, 0, len(preds))
		for _, p := range preds {
			rows = append(rows, []string{
				trunc(p.Type, 12),
				trunc(p.Entity, 24),
				fmt.Sprintf("%.2f", p.Score),
				fmt.Sprintf("%.0f", p.ETASeconds),
				trunc(p.Action, 30),
			})
		}
		printDataTable(cmd.OutOrStdout(), []string{"TYPE", "ENTITY", "SCORE", "ETA(s)", "ACTION"}, rows)
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
