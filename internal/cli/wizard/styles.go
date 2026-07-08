package wizard

import "github.com/charmbracelet/lipgloss"

// Design system tokens — consistent with internal/cli/theme.go.
var (
	colorPrimary   = lipgloss.AdaptiveColor{Light: "#7C3AED", Dark: "#A78BFA"}
	colorAccent    = lipgloss.AdaptiveColor{Light: "#0D9488", Dark: "#2DD4BF"}
	colorCritical  = lipgloss.AdaptiveColor{Light: "#DC2626", Dark: "#F87171"}
	colorWarning   = lipgloss.AdaptiveColor{Light: "#CA8A04", Dark: "#FBBF24"}
	colorSuccess   = lipgloss.AdaptiveColor{Light: "#16A34A", Dark: "#4ADE80"}
	colorNeutral   = lipgloss.AdaptiveColor{Light: "#6B7280", Dark: "#9CA3AF"}
	colorSubtle    = lipgloss.AdaptiveColor{Light: "#4B5563", Dark: "#D1D5DB"}
	colorMuted     = lipgloss.AdaptiveColor{Light: "#6B7280", Dark: "#6B7280"}
	colorSurface   = lipgloss.AdaptiveColor{Light: "#F9FAFB", Dark: "#1F2937"}
	colorCardBG    = lipgloss.AdaptiveColor{Light: "#FFFFFF", Dark: "#1F2937"}
	colorBorder    = lipgloss.AdaptiveColor{Light: "#D1D5DB", Dark: "#374151"}
	colorHighlight = lipgloss.AdaptiveColor{Light: "#111827", Dark: "#F9FAFB"}

	// Container
	appStyle = lipgloss.NewStyle().Padding(1, 2)

	// Title / logo
	titleStyle = lipgloss.NewStyle().
			Bold(true).
			Foreground(colorPrimary).
			Background(lipgloss.AdaptiveColor{Light: "#EDE9FE", Dark: "#2D2A4E"}).
			Padding(0, 1).
			MarginBottom(1)

	// Step indicator
	stepStyle       = lipgloss.NewStyle().Foreground(colorMuted).MarginBottom(1)
	activeStepStyle = lipgloss.NewStyle().
			Foreground(colorPrimary).
			Bold(true)

	// Input / form
	inputLabelStyle = lipgloss.NewStyle().Foreground(colorSubtle).Width(20)
	inputValueStyle = lipgloss.NewStyle().Foreground(colorHighlight)
	helpStyle       = lipgloss.NewStyle().Foreground(colorMuted).Italic(true).MarginTop(1)

	// Progress
	progressDoneStyle    = lipgloss.NewStyle().Foreground(colorSuccess).SetString("✓")
	progressCurrentStyle = lipgloss.NewStyle().Foreground(colorPrimary).Bold(true).SetString("▸")
	progressPendingStyle = lipgloss.NewStyle().Foreground(colorNeutral).SetString("○")

	// Status
	errStyle     = lipgloss.NewStyle().Foreground(colorCritical).Bold(true)
	warnStyle    = lipgloss.NewStyle().Foreground(colorWarning)
	infoStyle    = lipgloss.NewStyle().Foreground(colorNeutral)
	successStyle = lipgloss.NewStyle().Foreground(colorSuccess).Bold(true)

	// Box / card
	boxStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(colorBorder).
			Background(colorCardBG).
			Padding(1, 2).
			Width(72)

	// Summary table
	summaryKeyStyle     = lipgloss.NewStyle().Foreground(colorMuted).Width(24)
	summaryValueStyle   = lipgloss.NewStyle().Foreground(colorHighlight)
	summarySectionStyle = lipgloss.NewStyle().
				Bold(true).
				Foreground(colorAccent).
				PaddingTop(1).
				PaddingBottom(0)

	// Spinner
	spinnerStyle = lipgloss.NewStyle().Foreground(colorAccent)

	// Button-like
	buttonStyle = lipgloss.NewStyle().
			Foreground(colorPrimary).
			Bold(true)

	buttonDimStyle = lipgloss.NewStyle().Foreground(colorMuted)
)
