package cli

import (
	"time"

	"github.com/spf13/cobra"
)

var watchInterval time.Duration

func init() {
	watchCmd.Flags().DurationVar(&watchInterval, "interval", 2*time.Second, "refresh interval")
	rootCmd.AddCommand(watchCmd)
}

var watchCmd = &cobra.Command{
	Use:     "watch",
	Aliases: []string{"w"},
	Short:   "Live TUI dashboard",
	Long:    `Real-time control center (alias for kwctl ui).`,
	RunE: func(cmd *cobra.Command, args []string) error {
		return runControlCenter(watchInterval)
	},
}
