package cli

import "github.com/charmbracelet/lipgloss"

//nolint:unused // design system tokens used across the TUI
var (
	// Severity / semantic colors
	colorCritical = lipgloss.AdaptiveColor{Light: "#DC2626", Dark: "#F87171"} // red
	colorError    = lipgloss.AdaptiveColor{Light: "#EA580C", Dark: "#FB923C"} // orange
	colorWarning  = lipgloss.AdaptiveColor{Light: "#CA8A04", Dark: "#FBBF24"} // amber
	colorInfo     = lipgloss.AdaptiveColor{Light: "#0284C7", Dark: "#38BDF8"} // sky
	colorSuccess  = lipgloss.AdaptiveColor{Light: "#16A34A", Dark: "#4ADE80"} // green
	colorNeutral  = lipgloss.AdaptiveColor{Light: "#6B7280", Dark: "#9CA3AF"} // gray

	// Accent / brand
	colorPrimary   = lipgloss.AdaptiveColor{Light: "#7C3AED", Dark: "#A78BFA"} // purple
	colorAccent    = lipgloss.AdaptiveColor{Light: "#0D9488", Dark: "#2DD4BF"} // teal
	colorHighlight = lipgloss.AdaptiveColor{Light: "#111827", Dark: "#F9FAFB"} // near white
	colorSubtle    = lipgloss.AdaptiveColor{Light: "#4B5563", Dark: "#D1D5DB"} // soft text
	colorMuted     = lipgloss.AdaptiveColor{Light: "#6B7280", Dark: "#6B7280"} // dim text
	colorBorder    = lipgloss.AdaptiveColor{Light: "#D1D5DB", Dark: "#374151"}
	colorSurface   = lipgloss.AdaptiveColor{Light: "#F9FAFB", Dark: "#1F2937"} // panel bg
	colorScrim     = lipgloss.AdaptiveColor{Light: "#E5E7EB", Dark: "#111827"} // overlay bg
	colorStatusBar = lipgloss.AdaptiveColor{Light: "#F3F4F6", Dark: "#111827"}
	colorSelected  = lipgloss.AdaptiveColor{Light: "#E0E7FF", Dark: "#312E81"} // selection bg
	colorCardBG    = lipgloss.AdaptiveColor{Light: "#FFFFFF", Dark: "#1F2937"}

	// ── Logo / brand ──
	brandStyle = lipgloss.NewStyle().Bold(true).Foreground(colorAccent)
	logoStyle  = lipgloss.NewStyle().
			Bold(true).
			Foreground(colorPrimary).
			Background(lipgloss.AdaptiveColor{Light: "#EDE9FE", Dark: "#2D2A4E"}).
			Padding(0, 1)

	// ── Tabs ──
	tabActiveStyle = lipgloss.NewStyle().
			Bold(true).
			Foreground(colorHighlight).
			Background(colorPrimary).
			Padding(0, 1)

	tabInactiveStyle = lipgloss.NewStyle().
				Foreground(colorMuted).
				Padding(0, 1)

	tabBadgeStyle = lipgloss.NewStyle().
			Foreground(colorPrimary).
			Bold(true)

	// ── Panels ──
	panelStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(colorBorder).
			Padding(0, 1)

	// ── Text styles ──
	errStyle     = lipgloss.NewStyle().Foreground(colorCritical)
	warnStyle    = lipgloss.NewStyle().Foreground(colorWarning)
	infoStyle    = lipgloss.NewStyle().Foreground(colorInfo)
	successStyle = lipgloss.NewStyle().Foreground(colorSuccess).Bold(true)
	mutedStyle   = lipgloss.NewStyle().Foreground(colorMuted)
	subtleStyle  = lipgloss.NewStyle().Foreground(colorSubtle)
	keyStyle     = lipgloss.NewStyle().Bold(true).Foreground(colorAccent)
	keyDimStyle  = lipgloss.NewStyle().Foreground(colorNeutral)
	headingStyle = lipgloss.NewStyle().Bold(true).Foreground(colorHighlight)
	labelStyle   = lipgloss.NewStyle().Foreground(colorMuted)

	// ── Status ──
	statusOKStyle          = lipgloss.NewStyle().Foreground(colorSuccess).Bold(true)
	statusWarnStyle        = lipgloss.NewStyle().Foreground(colorWarning).Bold(true)
	statusHealthyDotStyle  = lipgloss.NewStyle().Foreground(colorSuccess).Bold(true).Render
	statusWarnDotStyle     = lipgloss.NewStyle().Foreground(colorWarning).Bold(true).Render
	statusCriticalDotStyle = lipgloss.NewStyle().Foreground(colorCritical).Bold(true).Render

	// ── Mode badges ──
	modeLiveStyle = lipgloss.NewStyle().
			Foreground(colorCritical).
			Background(lipgloss.AdaptiveColor{Light: "#FEE2E2", Dark: "#3B1111"}).
			Bold(true).
			Padding(0, 1)

	modeObserveStyle = lipgloss.NewStyle().
				Foreground(colorSuccess).
				Background(lipgloss.AdaptiveColor{Light: "#DCFCE7", Dark: "#0A2E1A"}).
				Bold(true).
				Padding(0, 1)

	modeDryRunStyle = lipgloss.NewStyle().
			Foreground(colorWarning).
			Background(lipgloss.AdaptiveColor{Light: "#FEF3C7", Dark: "#2E250A"}).
			Bold(true).
			Padding(0, 1)

	// ── List ──
	listSelectedStyle = lipgloss.NewStyle().
				Background(colorSelected).
				Foreground(colorHighlight).
				Bold(true)

	listRowCriticalStyle = lipgloss.NewStyle().Foreground(colorCritical)
	listRowWarnStyle     = lipgloss.NewStyle().Foreground(colorWarning)
	listRowInfoStyle     = lipgloss.NewStyle().Foreground(colorInfo)
	listRowSuccessStyle  = lipgloss.NewStyle().Foreground(colorSuccess)
	listRowDimStyle      = lipgloss.NewStyle().Foreground(colorMuted)
	listHeaderStyle      = lipgloss.NewStyle().Foreground(colorMuted).Bold(true)

	// ── Dashboard stat cards ──
	cardStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(colorBorder).
			Background(colorCardBG).
			Padding(0, 1).
			MarginRight(1).
			MarginBottom(1)

	cardCriticalStyle = cardStyle.BorderForeground(colorCritical)
	cardWarnStyle     = cardStyle.BorderForeground(colorWarning)
	cardSuccessStyle  = cardStyle.BorderForeground(colorSuccess)
	cardInfoStyle     = cardStyle.BorderForeground(colorInfo)
	cardNeutralStyle  = cardStyle.BorderForeground(colorNeutral)

	statLabelStyle = lipgloss.NewStyle().Foreground(colorMuted).Width(12)
	statValueStyle = lipgloss.NewStyle().Bold(true).Foreground(colorHighlight)
	statGridGap    = 1

	// ── Dashboard KPI styles (borderless, for compact dashboard grid) ──
	kpiCriticalStyle = lipgloss.NewStyle().Foreground(colorCritical).Bold(true)
	kpiWarnStyle     = lipgloss.NewStyle().Foreground(colorWarning).Bold(true)
	kpiSuccessStyle  = lipgloss.NewStyle().Foreground(colorSuccess).Bold(true)

	// ── Detail view ──
	detailLabelStyle   = lipgloss.NewStyle().Foreground(colorMuted).Width(16)
	detailValueStyle   = lipgloss.NewStyle().Foreground(colorHighlight)
	detailSectionStyle = lipgloss.NewStyle().
				Bold(true).
				Foreground(colorAccent).
				PaddingTop(1).
				PaddingBottom(0)

	detailBorderActiveStyle = lipgloss.NewStyle().
				Border(lipgloss.RoundedBorder()).
				BorderForeground(colorAccent).
				Padding(0, 1)

	detailBorderInactiveStyle = lipgloss.NewStyle().
					Border(lipgloss.RoundedBorder()).
					BorderForeground(colorBorder).
					Padding(0, 1)

	// ── Help overlay ──
	helpCategoryStyle = lipgloss.NewStyle().Bold(true).Foreground(colorAccent).PaddingTop(1)
	helpKeyStyle      = lipgloss.NewStyle().Bold(true).Foreground(colorAccent).Width(14)
	helpDescStyle     = lipgloss.NewStyle().Foreground(colorSubtle)
	helpCloseStyle    = lipgloss.NewStyle().Foreground(colorMuted)

	// ── Empty state ──
	emptyStateStyle = lipgloss.NewStyle().Foreground(colorMuted).Italic(true).Padding(1, 2)
	emptyEmojiStyle = lipgloss.NewStyle().Foreground(colorNeutral).Width(2)

	// ── Confirm dialog ──
	confirmBoxStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(colorWarning).
			Padding(1, 2).
			Background(colorSurface)

	confirmTitleStyle = lipgloss.NewStyle().Bold(true).Foreground(colorWarning)

	// ── Status bar ──
	statusBarStyle = lipgloss.NewStyle().
			Foreground(colorSubtle).
			Background(colorStatusBar).
			Padding(0, 1)

	statusBarActiveStyle = lipgloss.NewStyle().
				Foreground(colorSuccess).
				Background(colorStatusBar).
				Bold(true)

	statusBarWarnStyle = lipgloss.NewStyle().
				Foreground(colorWarning).
				Background(colorStatusBar).
				Bold(true)

	// ── Footer ──
	footerStyle = lipgloss.NewStyle().
			Foreground(colorMuted).
			PaddingLeft(1)

	// ── Separator ──
	dotSeparator = mutedStyle.Render(" · ")
	hbar         = mutedStyle.Render(repeatLine(48))
)

const (
	uiHeaderLines = 3
	uiTabLines    = 1
	uiFooterLines = 2
)
