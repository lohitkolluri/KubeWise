package cli

import (
	"charm.land/lipgloss/v2"
	"charm.land/lipgloss/v2/compat"
)

//nolint:unused // design system tokens used across the TUI
var (
	// Severity / semantic colors
	colorCritical = compat.AdaptiveColor{Light: lipgloss.Color("#DC2626"), Dark: lipgloss.Color("#F87171")} // red
	colorError    = compat.AdaptiveColor{Light: lipgloss.Color("#EA580C"), Dark: lipgloss.Color("#FB923C")} // orange
	colorWarning  = compat.AdaptiveColor{Light: lipgloss.Color("#CA8A04"), Dark: lipgloss.Color("#FBBF24")} // amber
	colorInfo     = compat.AdaptiveColor{Light: lipgloss.Color("#0284C7"), Dark: lipgloss.Color("#38BDF8")} // sky
	colorSuccess  = compat.AdaptiveColor{Light: lipgloss.Color("#16A34A"), Dark: lipgloss.Color("#4ADE80")} // green
	colorNeutral  = compat.AdaptiveColor{Light: lipgloss.Color("#6B7280"), Dark: lipgloss.Color("#9CA3AF")} // gray

	// Accent / brand
	colorPrimary   = compat.AdaptiveColor{Light: lipgloss.Color("#7C3AED"), Dark: lipgloss.Color("#A78BFA")} // purple
	colorAccent    = compat.AdaptiveColor{Light: lipgloss.Color("#0D9488"), Dark: lipgloss.Color("#2DD4BF")} // teal
	colorHighlight = compat.AdaptiveColor{Light: lipgloss.Color("#111827"), Dark: lipgloss.Color("#F9FAFB")} // near white
	colorSubtle    = compat.AdaptiveColor{Light: lipgloss.Color("#4B5563"), Dark: lipgloss.Color("#D1D5DB")} // soft text
	colorMuted     = compat.AdaptiveColor{Light: lipgloss.Color("#6B7280"), Dark: lipgloss.Color("#6B7280")} // dim text
	colorBorder    = compat.AdaptiveColor{Light: lipgloss.Color("#D1D5DB"), Dark: lipgloss.Color("#374151")}
	colorSurface   = compat.AdaptiveColor{Light: lipgloss.Color("#F9FAFB"), Dark: lipgloss.Color("#1F2937")} // panel bg
	colorScrim     = compat.AdaptiveColor{Light: lipgloss.Color("#E5E7EB"), Dark: lipgloss.Color("#111827")} // overlay bg
	colorStatusBar = compat.AdaptiveColor{Light: lipgloss.Color("#F3F4F6"), Dark: lipgloss.Color("#111827")}
	colorSelected  = compat.AdaptiveColor{Light: lipgloss.Color("#E0E7FF"), Dark: lipgloss.Color("#312E81")} // selection bg
	colorCardBG    = compat.AdaptiveColor{Light: lipgloss.Color("#FFFFFF"), Dark: lipgloss.Color("#1F2937")}

	// ── Logo / brand ──
	brandStyle = lipgloss.NewStyle().Bold(true).Foreground(colorAccent)
	logoStyle  = lipgloss.NewStyle().
			Bold(true).
			Foreground(colorPrimary).
			Background(compat.AdaptiveColor{Light: lipgloss.Color("#EDE9FE"), Dark: lipgloss.Color("#2D2A4E")}).
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

	tabIndicatorStyle = lipgloss.NewStyle().
				Foreground(colorAccent).
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
			Background(compat.AdaptiveColor{Light: lipgloss.Color("#FEE2E2"), Dark: lipgloss.Color("#3B1111")}).
			Bold(true).
			Padding(0, 1)

	modeObserveStyle = lipgloss.NewStyle().
				Foreground(colorSuccess).
				Background(compat.AdaptiveColor{Light: lipgloss.Color("#DCFCE7"), Dark: lipgloss.Color("#0A2E1A")}).
				Bold(true).
				Padding(0, 1)

	modeDryRunStyle = lipgloss.NewStyle().
			Foreground(colorWarning).
			Background(compat.AdaptiveColor{Light: lipgloss.Color("#FEF3C7"), Dark: lipgloss.Color("#2E250A")}).
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
	listHeaderStyle      = lipgloss.NewStyle().Foreground(colorMuted)

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

	// ── Dashboard ──
	dashSectionTitleStyle = lipgloss.NewStyle().
				Bold(true).
				Foreground(colorAccent).
				PaddingTop(1)

	dashKPILabelStyle = lipgloss.NewStyle().
				Foreground(colorMuted)

	dashAlertStyle = lipgloss.NewStyle().
			Foreground(colorWarning).
			Bold(true).
			Padding(0, 1)

	dashAlertDetailStyle = lipgloss.NewStyle().
				Foreground(colorMuted)

	// ── Dashboard KPI value styles ──
	kpiCriticalStyle = lipgloss.NewStyle().Foreground(colorCritical).Bold(true)
	kpiWarnStyle     = lipgloss.NewStyle().Foreground(colorWarning).Bold(true)
	kpiSuccessStyle  = lipgloss.NewStyle().Foreground(colorSuccess).Bold(true)

	// ── Detail view ──
	detailLabelStyle   = lipgloss.NewStyle().Foreground(colorMuted).Width(16)
	detailValueStyle   = lipgloss.NewStyle().Foreground(colorHighlight)
	detailSectionStyle = lipgloss.NewStyle().
				Bold(true).
				Foreground(colorAccent)

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
			Padding(1, 2)

	confirmTitleStyle = lipgloss.NewStyle().Bold(true).Foreground(colorWarning)

	// ── Status bar ──
	statusBarStyle = lipgloss.NewStyle().
			Foreground(colorSubtle).
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
