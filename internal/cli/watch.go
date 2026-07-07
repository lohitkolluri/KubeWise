package cli

import (
	"time"

	"github.com/spf13/cobra"
)

var watchInterval time.Duration
var watchMouse bool
var watchAltScreen bool

func init() {
	watchCmd.Flags().DurationVar(&watchInterval, "interval", 2*time.Second, "refresh interval")
	watchCmd.Flags().BoolVar(&watchMouse, "mouse", false, "enable mouse interactions (disables terminal text selection/copy in many terminals)")
	watchCmd.Flags().BoolVar(&watchAltScreen, "altscreen", true, "use terminal alternate screen buffer")
	rootCmd.AddCommand(watchCmd)
}

var watchCmd = &cobra.Command{
	Use:     "watch",
	Aliases: []string{"w"},
	Short:   "Live TUI dashboard",
	Long:    `Real-time control center (alias for kwctl ui).`,
	RunE: func(cmd *cobra.Command, args []string) error {
		// Keep watch flags in sync with ui behavior.
		uiMouse = watchMouse
		uiAltScreen = watchAltScreen
		return runControlCenter(watchInterval)
	},
}
