package cli

import (
	"strings"

	"charm.land/lipgloss/v2"
)

// contentHeight returns the inner panel content height in terminal rows.
func (m controlModel) contentHeight() int {
	above := lipgloss.Height(m.renderHeader()) + 1 // newline
	if strip := m.toasts.strip(); strip != "" {
		above += lipgloss.Height(strip) + 1
	}
	above += lipgloss.Height(m.renderTabs()) + 1 // newline before panel

	below := 1 + lipgloss.Height(m.renderFooter()) // newline + footer

	_, frameY := panelStyle.GetFrameSize()
	h := m.height - above - below - frameY
	if h < 4 {
		return 4
	}
	return h
}

func (m controlModel) panelInnerHeight() int {
	return m.contentHeight()
}

func clipToLines(s string, maxLines int) string {
	if maxLines <= 0 {
		return ""
	}
	lines := strings.Split(strings.TrimRight(s, "\n"), "\n")
	if len(lines) <= maxLines {
		return strings.Join(lines, "\n")
	}
	lines = lines[:maxLines]
	if maxLines > 0 {
		lines[maxLines-1] = mutedStyle.Render("  … resize terminal for more")
	}
	return strings.Join(lines, "\n")
}

func truncUptime(s string) string {
	if s == "" {
		return "—"
	}
	if i := strings.IndexByte(s, '.'); i > 0 {
		s = s[:i]
	}
	return trunc(s, 14)
}
