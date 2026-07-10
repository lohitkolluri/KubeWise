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
				out := cmd.OutOrStdout()
				printSection(out, "Anomaly")
				printKV(out, "ID:", r.ID)
				printKV(out, "Entity:", r.Entity)
				printKV(out, "Namespace:", r.Namespace)
				printKV(out, "Metric:", r.MetricName)
				printKV(out, "Pattern:", r.Pattern)
				printKVStyled(out, "Score:", fmt.Sprintf("%.2f", r.Score), scoreStyle(r.Score))
				printKVStyled(out, "Status:", string(r.Status), statusStyle(string(r.Status)))
				if r.DetectedAt != nil {
					printKV(out, "Detected:", r.DetectedAt.Format(timeRFC3339))
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
			printEmpty(cmd.OutOrStdout(), "No anomalies.")
			return nil
		}
		rows := make([][]string, 0, len(records))
		for _, r := range records {
			typ := r.Pattern
			if typ == "" {
				typ = "statistical"
			}
			rows = append(rows, []string{
				trunc(typ, 12),
				trunc(r.Entity, 24),
				fmt.Sprintf("%.2f", r.Score),
				string(r.Status),
				trunc(r.MetricName, 30),
			})
		}
		printDataTable(cmd.OutOrStdout(), []string{"TYPE", "ENTITY", "SCORE", "STATUS", "METRIC"}, rows)
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
