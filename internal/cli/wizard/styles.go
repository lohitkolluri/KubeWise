package wizard

import (
	"charm.land/lipgloss/v2"
	"charm.land/lipgloss/v2/compat"
)

// Design system tokens — consistent with internal/cli/theme.go.
var (
	colorPrimary   = compat.AdaptiveColor{Light: lipgloss.Color("#7C3AED"), Dark: lipgloss.Color("#A78BFA")}
	colorAccent    = compat.AdaptiveColor{Light: lipgloss.Color("#0D9488"), Dark: lipgloss.Color("#2DD4BF")}
	colorCritical  = compat.AdaptiveColor{Light: lipgloss.Color("#DC2626"), Dark: lipgloss.Color("#F87171")}
	colorWarning   = compat.AdaptiveColor{Light: lipgloss.Color("#CA8A04"), Dark: lipgloss.Color("#FBBF24")}
	colorSuccess   = compat.AdaptiveColor{Light: lipgloss.Color("#16A34A"), Dark: lipgloss.Color("#4ADE80")}
	colorNeutral   = compat.AdaptiveColor{Light: lipgloss.Color("#6B7280"), Dark: lipgloss.Color("#9CA3AF")}
	colorMuted = compat.AdaptiveColor{Light: lipgloss.Color("#6B7280"), Dark: lipgloss.Color("#6B7280")}
	colorBorder    = compat.AdaptiveColor{Light: lipgloss.Color("#D1D5DB"), Dark: lipgloss.Color("#374151")}
	colorHighlight = compat.AdaptiveColor{Light: lipgloss.Color("#111827"), Dark: lipgloss.Color("#F9FAFB")}

	// Container
	appStyle = lipgloss.NewStyle().Padding(1, 2)

	// Title / logo
	titleStyle = lipgloss.NewStyle().
			Bold(true).
			Foreground(colorPrimary).
			Padding(0, 1).
			MarginBottom(1)

	// Step indicator
	stepStyle       = lipgloss.NewStyle().Foreground(colorMuted).MarginBottom(1)
	activeStepStyle = lipgloss.NewStyle().
			Foreground(colorPrimary).
			Bold(true)

	// Input / form
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
			Padding(1, 2).
			Width(72)

	// Summary table
	summaryKeyStyle     = lipgloss.NewStyle().Foreground(colorMuted).Width(24)
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
)
