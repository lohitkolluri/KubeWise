package cli

import (
	"github.com/spf13/cobra"
)

func init() {
	rootCmd.AddCommand(interactiveCmd)
}

var interactiveCmd = &cobra.Command{
	Use:     "interactive",
	Aliases: []string{"i", "menu"},
	Short:   "Interactive control center",
	Long:    `Launch the KubeWise control center TUI (alias for kwctl ui).`,
	RunE: func(_ *cobra.Command, _ []string) error {
		return runControlCenter(uiInterval, false)
	},
}
