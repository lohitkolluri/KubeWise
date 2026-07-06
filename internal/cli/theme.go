package cli

import "github.com/charmbracelet/lipgloss"

// Adaptive palette — readable on light and dark terminals.
var (
	colorPrimary   = lipgloss.AdaptiveColor{Light: "#7C3AED", Dark: "#A78BFA"}
	colorAccent    = lipgloss.AdaptiveColor{Light: "#DB2777", Dark: "#F472B6"}
	colorSuccess   = lipgloss.AdaptiveColor{Light: "#15803D", Dark: "#4ADE80"}
	colorWarning   = lipgloss.AdaptiveColor{Light: "#B45309", Dark: "#FBBF24"}
	colorDanger    = lipgloss.AdaptiveColor{Light: "#B91C1C", Dark: "#F87171"}
	colorMuted     = lipgloss.AdaptiveColor{Light: "#6B7280", Dark: "#9CA3AF"}
	colorSubtle    = lipgloss.AdaptiveColor{Light: "#4B5563", Dark: "#D1D5DB"}
	colorBorder    = lipgloss.AdaptiveColor{Light: "#D1D5DB", Dark: "#374151"}
	colorHighlight = lipgloss.AdaptiveColor{Light: "#111827", Dark: "#F9FAFB"}
	colorPanelBG   = lipgloss.AdaptiveColor{Light: "#F9FAFB", Dark: "#1F2937"}
	colorScrim     = lipgloss.AdaptiveColor{Light: "#E5E7EB", Dark: "#111827"}
	colorStatusBar = lipgloss.AdaptiveColor{Light: "#F3F4F6", Dark: "#1F2937"}

	brandStyle = lipgloss.NewStyle().Bold(true).Foreground(colorAccent)
	logoStyle  = lipgloss.NewStyle().
			Bold(true).
			Foreground(colorPrimary).
			Background(lipgloss.AdaptiveColor{Light: "#EDE9FE", Dark: "#1E1B2E"}).
			Padding(0, 1)

	tabActiveStyle = lipgloss.NewStyle().
			Bold(true).
			Foreground(colorHighlight).
			Background(colorPrimary).
			Padding(0, 1)

	tabInactiveStyle = lipgloss.NewStyle().
				Foreground(colorMuted).
				Padding(0, 1)

	panelStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(colorBorder).
			Padding(0, 1)

	statLabelStyle = lipgloss.NewStyle().Foreground(colorMuted)
	statValueStyle = lipgloss.NewStyle().Bold(true).Foreground(colorSuccess)
	errStyle       = lipgloss.NewStyle().Foreground(colorDanger)
	warnStyle      = lipgloss.NewStyle().Foreground(colorWarning)
	mutedStyle     = lipgloss.NewStyle().Foreground(colorMuted)
	keyStyle       = lipgloss.NewStyle().Bold(true).Foreground(colorAccent)
	statusOKStyle  = lipgloss.NewStyle().Foreground(colorSuccess).Bold(true)
	statusBarStyle = lipgloss.NewStyle().
			Foreground(colorSubtle).
			Background(colorStatusBar).
			Padding(0, 1)

	listSelectedStyle = lipgloss.NewStyle().
				Background(lipgloss.AdaptiveColor{Light: "#E0E7FF", Dark: "#312E81"}).
				Foreground(colorHighlight).
				Bold(true)

	emptyStateStyle = lipgloss.NewStyle().
				Foreground(colorMuted).
				Italic(true).
				Padding(1, 2)

	confirmBoxStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(colorWarning).
			Padding(1, 2).
			Background(colorPanelBG)
)

const (
	uiHeaderLines = 2
	uiTabLines    = 1
	uiFooterLines = 1
)
