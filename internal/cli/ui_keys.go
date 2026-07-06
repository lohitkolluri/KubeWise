package cli

import (
	"github.com/charmbracelet/bubbles/key"
)

// uiKeyMap centralizes bindings (Charm best practice: key.Matches + help.KeyMap).
type uiKeyMap struct {
	TabPrev   key.Binding
	TabNext   key.Binding
	Tab1      key.Binding
	Tab2      key.Binding
	Tab3      key.Binding
	Tab4      key.Binding
	Tab5      key.Binding
	Tab6      key.Binding
	Tab7      key.Binding
	Up        key.Binding
	Down      key.Binding
	Top       key.Binding
	Bottom    key.Binding
	Detail    key.Binding
	Back      key.Binding
	Refresh   key.Binding
	Palette   key.Binding
	Help      key.Binding
	Quit      key.Binding
	DryRun    key.Binding
	Mode      key.Binding
	Restart   key.Binding
	Approve   key.Binding
	Reject    key.Binding
	ToggleLive key.Binding
	Confirm   key.Binding
	Cancel    key.Binding
}

func defaultUIKeys() uiKeyMap {
	return uiKeyMap{
		TabPrev: key.NewBinding(
			key.WithKeys("left", "h"),
			key.WithHelp("←", "prev tab"),
		),
		TabNext: key.NewBinding(
			key.WithKeys("right", "l"),
			key.WithHelp("→", "next tab"),
		),
		Tab1: key.NewBinding(key.WithKeys("1"), key.WithHelp("1", "dashboard")),
		Tab2: key.NewBinding(key.WithKeys("2"), key.WithHelp("2", "predict")),
		Tab3: key.NewBinding(key.WithKeys("3"), key.WithHelp("3", "anomalies")),
		Tab4: key.NewBinding(key.WithKeys("4"), key.WithHelp("4", "audit")),
	Tab5: key.NewBinding(key.WithKeys("5"), key.WithHelp("5", "approve")),
		Tab6: key.NewBinding(key.WithKeys("6"), key.WithHelp("6", "config")),
		Tab7: key.NewBinding(key.WithKeys("7"), key.WithHelp("7", "logs")),
		Up: key.NewBinding(
			key.WithKeys("up", "k"),
			key.WithHelp("↑/k", "up"),
		),
		Down: key.NewBinding(
			key.WithKeys("down", "j"),
			key.WithHelp("↓/j", "down"),
		),
		Top: key.NewBinding(
			key.WithKeys("g"),
			key.WithHelp("g", "top"),
		),
		Bottom: key.NewBinding(
			key.WithKeys("G"),
			key.WithHelp("G", "bottom"),
		),
		Detail: key.NewBinding(
			key.WithKeys("enter"),
			key.WithHelp("enter", "detail"),
		),
		Back: key.NewBinding(
			key.WithKeys("esc"),
			key.WithHelp("esc", "back"),
		),
		Refresh: key.NewBinding(
			key.WithKeys("r"),
			key.WithHelp("r", "refresh"),
		),
		Palette: key.NewBinding(
			key.WithKeys("ctrl+p"),
			key.WithHelp("ctrl+p", "commands"),
		),
		Help: key.NewBinding(
			key.WithKeys("?"),
			key.WithHelp("?", "help"),
		),
		Quit: key.NewBinding(
			key.WithKeys("q", "ctrl+c"),
			key.WithHelp("q", "quit"),
		),
		DryRun: key.NewBinding(
			key.WithKeys("d"),
			key.WithHelp("d", "dry-run"),
		),
		Mode: key.NewBinding(
			key.WithKeys("m"),
			key.WithHelp("m", "mode"),
		),
		Restart: key.NewBinding(
			key.WithKeys("R"),
			key.WithHelp("R", "restart"),
		),
		Approve: key.NewBinding(
			key.WithKeys("a"),
			key.WithHelp("a", "approve"),
		),
		Reject: key.NewBinding(
			key.WithKeys("x"),
			key.WithHelp("x", "reject"),
		),
		ToggleLive: key.NewBinding(
			key.WithKeys("L"),
			key.WithHelp("L", "live mode"),
		),
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
	return []key.Binding{k.TabPrev, k.TabNext, k.Palette, k.Help, k.Quit}
}

func (k uiKeyMap) FullHelp() [][]key.Binding {
	return [][]key.Binding{
		{k.TabPrev, k.TabNext, k.Tab1, k.Tab2, k.Tab3},
		{k.Tab4, k.Tab5, k.Tab6, k.Tab7, k.Up},
		{k.Top, k.Bottom, k.Detail, k.Refresh, k.Palette},
		{k.Approve, k.Reject, k.ToggleLive, k.DryRun, k.Restart},
	}
}
