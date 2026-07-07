package cli

import (
	"github.com/charmbracelet/bubbles/key"
)

// uiKeyMap centralizes bindings (Charm best practice: key.Matches + help.KeyMap).
type uiKeyMap struct {
	// Navigation
	TabPrev key.Binding
	TabNext key.Binding
	Tab1    key.Binding
	Tab2    key.Binding
	Tab3    key.Binding
	Tab4    key.Binding
	Tab5    key.Binding
	Tab6    key.Binding
	Tab7    key.Binding
	Tab8    key.Binding

	// List navigation
	Up     key.Binding
	Down   key.Binding
	Top    key.Binding
	Bottom key.Binding

	// Actions
	Detail  key.Binding
	Back    key.Binding
	Refresh key.Binding

	// Palette & help
	Palette key.Binding
	Help    key.Binding
	Quit    key.Binding

	// Config / mode
	DryRun     key.Binding
	Mode       key.Binding
	Restart    key.Binding
	ToggleLive key.Binding

	// Approvals
	Approve key.Binding
	Reject  key.Binding

	// Logs
	LogFollow key.Binding

	// Confirm / cancel
	Confirm key.Binding
	Cancel  key.Binding
}

func defaultUIKeys() uiKeyMap {
	return uiKeyMap{
		// Navigation
		TabPrev: key.NewBinding(
			key.WithKeys("left", "h"),
			key.WithHelp("←/h", "prev tab"),
		),
		TabNext: key.NewBinding(
			key.WithKeys("right", "l"),
			key.WithHelp("→/l", "next tab"),
		),
		Tab1: key.NewBinding(key.WithKeys("1"), key.WithHelp("1", "dashboard")),
		Tab2: key.NewBinding(key.WithKeys("2"), key.WithHelp("2", "predictions")),
		Tab3: key.NewBinding(key.WithKeys("3"), key.WithHelp("3", "anomalies")),
		Tab4: key.NewBinding(key.WithKeys("4"), key.WithHelp("4", "audit")),
		Tab5: key.NewBinding(key.WithKeys("5"), key.WithHelp("5", "approvals")),
		Tab6: key.NewBinding(key.WithKeys("6"), key.WithHelp("6", "config")),
		Tab7: key.NewBinding(key.WithKeys("7"), key.WithHelp("7", "logs")),
		Tab8: key.NewBinding(key.WithKeys("8"), key.WithHelp("8", "health")),

		// List navigation
		Up: key.NewBinding(
			key.WithKeys("up", "k"),
			key.WithHelp("↑/k", "scroll up"),
		),
		Down: key.NewBinding(
			key.WithKeys("down", "j"),
			key.WithHelp("↓/j", "scroll down"),
		),
		Top: key.NewBinding(
			key.WithKeys("g"),
			key.WithHelp("g", "go to top"),
		),
		Bottom: key.NewBinding(
			key.WithKeys("G"),
			key.WithHelp("G", "go to bottom"),
		),

		// Actions
		Detail: key.NewBinding(
			key.WithKeys("enter"),
			key.WithHelp("enter", "show detail"),
		),
		Back: key.NewBinding(
			key.WithKeys("esc"),
			key.WithHelp("esc", "back / close"),
		),
		Refresh: key.NewBinding(
			key.WithKeys("r"),
			key.WithHelp("r", "refresh data"),
		),

		// Palette & help
		Palette: key.NewBinding(
			key.WithKeys("ctrl+p"),
			key.WithHelp("ctrl+p", "command palette"),
		),
		Help: key.NewBinding(
			key.WithKeys("?"),
			key.WithHelp("?", "toggle help"),
		),
		Quit: key.NewBinding(
			key.WithKeys("q", "ctrl+c"),
			key.WithHelp("q", "quit"),
		),

		// Config / mode
		DryRun: key.NewBinding(
			key.WithKeys("d"),
			key.WithHelp("d", "toggle observe/live"),
		),
		Mode: key.NewBinding(
			key.WithKeys("m"),
			key.WithHelp("m", "cycle remediation mode"),
		),
		Restart: key.NewBinding(
			key.WithKeys("R"),
			key.WithHelp("R", "restart agent"),
		),
		ToggleLive: key.NewBinding(
			key.WithKeys("L"),
			key.WithHelp("L", "toggle live mode"),
		),

		// Approvals
		Approve: key.NewBinding(
			key.WithKeys("a"),
			key.WithHelp("a", "approve"),
		),
		Reject: key.NewBinding(
			key.WithKeys("x"),
			key.WithHelp("x", "reject"),
		),

		// Logs
		LogFollow: key.NewBinding(
			key.WithKeys("f"),
			key.WithHelp("f", "toggle log follow"),
		),

		// Confirm / cancel
		Confirm: key.NewBinding(
			key.WithKeys("y", "Y"),
			key.WithHelp("y", "confirm"),
		),
		Cancel: key.NewBinding(
			key.WithKeys("n", "N", "esc"),
			key.WithHelp("n", "cancel"),
		),
	}
}

func (k uiKeyMap) ShortHelp() []key.Binding {
	return []key.Binding{
		k.TabPrev, k.TabNext, k.Palette, k.Help, k.Quit,
	}
}

func (k uiKeyMap) FullHelp() [][]key.Binding {
	return [][]key.Binding{
		// Row 1: Navigation
		{k.TabPrev, k.TabNext, k.Palette, k.Quit, k.Help},
		// Row 2: Tab shortcuts
		{k.Tab1, k.Tab2, k.Tab3, k.Tab4, k.Tab5},
		// Row 3: Tab shortcuts (cont)
		{k.Tab6, k.Tab7, k.Tab8, k.Refresh, k.Detail, k.Back},
		// Row 4: List navigation
		{k.Up, k.Down, k.Top, k.Bottom},
		// Row 5: Actions
		{k.DryRun, k.Mode, k.ToggleLive, k.Restart, k.Approve},
		// Row 6: Actions (cont) + logs
		{k.Reject, k.LogFollow, k.Confirm, k.Cancel},
	}
}
