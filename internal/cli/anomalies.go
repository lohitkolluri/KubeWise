package cli

import (
	"fmt"
	"strings"

	"github.com/spf13/cobra"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

var (
	anomalyLimit     int
	anomalyNamespace string
	anomalyStatus    string
)

func init() {
	anomaliesCmd.Flags().IntVarP(&anomalyLimit, "limit", "l", 20, "max records to fetch")
	anomaliesCmd.Flags().StringVar(&anomalyNamespace, "namespace", "", "filter by namespace")
	anomaliesCmd.Flags().StringVar(&anomalyStatus, "status", "", "filter by status (detected, correlated, ...)")
	anomaliesCmd.AddCommand(anomaliesDescribeCmd)
	rootCmd.AddCommand(anomaliesCmd)
}

var anomaliesCmd = &cobra.Command{
	Use:   "anomalies",
	Short: "Show detected anomalies",
	Long:  `Fetch and display anomaly records from the agent.`,
	RunE:  runAnomaliesList,
}

var anomaliesDescribeCmd = &cobra.Command{
	Use:   "describe ID",
	Short: "Describe a single anomaly by ID prefix",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		records, err := fetchAnomalies(100)
		if err != nil {
			return err
		}
		id := args[0]
		for _, r := range records {
			if r.ID == id || strings.HasPrefix(r.ID, id) {
				if outputFormat == "json" {
					return writeOutput(cmd.OutOrStdout(), "json", r, nil)
				}
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "ID:          %s\n", r.ID)
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Entity:      %s\n", r.Entity)
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Namespace:   %s\n", r.Namespace)
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Metric:      %s\n", r.MetricName)
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Pattern:     %s\n", r.Pattern)
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Score:       %.2f\n", r.Score)
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Status:      %s\n", r.Status)
				if r.DetectedAt != nil {
					_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Detected:    %s\n", r.DetectedAt.Format(timeRFC3339))
				}
				return nil
			}
		}
		return fmt.Errorf("anomaly %q not found", id)
	},
}

const timeRFC3339 = "2006-01-02 15:04:05 MST"

func runAnomaliesList(cmd *cobra.Command, args []string) error {
	if err := validateOutputFormat(); err != nil {
		return err
	}
	records, err := fetchAnomalies(anomalyLimit)
	if err != nil {
		return err
	}
	records = filterAnomalies(records)
	return writeOutput(cmd.OutOrStdout(), outputFormat, records, func() error {
		if len(records) == 0 {
			_, _ = fmt.Fprintln(cmd.OutOrStdout(), "No anomalies.")
			return nil
		}
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-12s %-24s %-10s %-8s %s\n",
			"TYPE", "ENTITY", "SCORE", "STATUS", "METRIC")
		_, _ = fmt.Fprintln(cmd.OutOrStdout(), strings.Repeat("-", 80))
		for _, r := range records {
			typ := r.Pattern
			if typ == "" {
				typ = "statistical"
			}
			_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-12s %-24s %-10.2f %-8s %s\n",
				trunc(typ, 10), trunc(r.Entity, 22), r.Score, string(r.Status), trunc(r.MetricName, 30))
		}
		return nil
	})
}

func filterAnomalies(records []models.AnomalyRecord) []models.AnomalyRecord {
	var out []models.AnomalyRecord
	for _, r := range records {
		if anomalyNamespace != "" && r.Namespace != anomalyNamespace && !strings.HasPrefix(r.Entity, anomalyNamespace+"/") {
			continue
		}
		if anomalyStatus != "" && string(r.Status) != anomalyStatus {
			continue
		}
		out = append(out, r)
	}
	return out
}
