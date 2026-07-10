package cli

import (
	"charm.land/bubbles/v2/progress"
	"charm.land/lipgloss/v2"
)

func newRefreshProgress() progress.Model {
	p := progress.New(
		progress.WithWidth(18),
		progress.WithColors(lipgloss.Color("#7C3AED"), lipgloss.Color("#2DD4BF")),
		progress.WithSpringOptions(6.0, 0.8),
	)
	p.ShowPercentage = false
	return p
}

func newActionProgress() progress.Model {
	p := progress.New(
		progress.WithWidth(28),
		progress.WithColors(lipgloss.Color("#0D9488"), lipgloss.Color("#374151")),
		progress.WithSpringOptions(5.0, 0.85),
	)
	p.Full = '█'
	p.Empty = '░'
	return p
}

func (m *controlModel) resizeChrome() {
	m.refreshProg.SetWidth(min(24, max(12, m.width/6)))
}

func (m controlModel) renderLoadingBar() string {
	// Sync refresh is indicated by the header spinner only — no footer bar.
	if !m.actionBusy {
		return ""
	}
	return mutedStyle.Render(m.actionLabel) + " " + m.actionProg.View()
}
