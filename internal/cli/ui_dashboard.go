package cli

import (
	"fmt"
	"strings"

	"charm.land/lipgloss/v2"
	lgtable "charm.land/lipgloss/v2/table"

	"charm.land/bubbles/v2/table"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

const (
	dashKPICols      = 4
	dashKPIColsWide  = 8 // single row on wide terminals
	dashSectionBlank = 1 // empty lines between sections
	dashRecentLimit  = 5 // recent activity rows on dashboard
)

// dashSectionSep is the join delimiter that leaves dashSectionBlank empty lines.
func dashSectionSep() string {
	return strings.Repeat("\n", dashSectionBlank+1)
}

func dashContentWidth(fullWidth int) int {
	w := fullWidth - 6 // panel border + horizontal padding
	if w < 40 {
		return 40
	}
	return w
}

func (m controlModel) renderDashboard() string {
	return m.renderDashboardFit(m.panelInnerHeight())
}

func (m controlModel) renderDashboardFit(maxH int) string {
	if maxH < 8 {
		return mutedStyle.Render("Terminal too small — increase window height")
	}

	innerW := dashContentWidth(m.width)
	budget := maxH
	var sections []string

	if banner := m.renderDashBanner(); banner != "" {
		h := lipgloss.Height(banner)
		if h >= budget {
			return clipToLines(banner, maxH)
		}
		sections = append(sections, banner)
		budget -= h + dashSectionBlank + 1
	}

	kpis := renderDashKPIGrid(m.dashboardKPIs(), innerW)
	kpiH := lipgloss.Height(kpis)
	if kpiH >= budget {
		sections = append(sections, kpis)
		return clipToLines(joinDashSections(sections...), maxH)
	}
	sections = append(sections, kpis)
	budget -= kpiH + dashSectionBlank + 1

	recentRows := dashRecentRowLimit(budget)
	if recent := m.renderDashRecentSection(innerW, recentRows); recent != "" {
		h := lipgloss.Height(recent)
		if h <= budget {
			sections = append(sections, recent)
			budget -= h + dashSectionBlank + 1
		}
	}

	if len(m.healthScores) > 0 && budget > 5 {
		rows := dashHealthRowLimit(budget)
		if health := m.renderDashHealthSection(innerW, rows); health != "" {
			h := lipgloss.Height(health)
			if h <= budget {
				sections = append(sections, health)
				budget -= h + dashSectionBlank + 1
			}
		}
	}

	if len(m.pending) > 0 && budget > 5 {
		rows := dashApprovalRowLimit(budget)
		if approvals := m.renderDashApprovalsSection(innerW, rows); approvals != "" {
			h := lipgloss.Height(approvals)
			if h <= budget {
				sections = append(sections, approvals)
				budget -= h + dashSectionBlank + 1
			}
		}
	}

	if m.config != nil && budget > 2 {
		sections = append(sections, renderDashAgentFooter(m.config, innerW))
	}

	return clipToLines(joinDashSections(sections...), maxH)
}

func dashRecentRowLimit(budget int) int {
	return dashTableRowLimit(budget, dashRecentLimit, 4)
}

func dashHealthRowLimit(budget int) int {
	return dashTableRowLimit(budget, 10, 4)
}

func dashApprovalRowLimit(budget int) int {
	return dashTableRowLimit(budget, 6, 4)
}

// dashTableRowLimit estimates how many data rows fit in the remaining budget.
func dashTableRowLimit(budget, maxRows, overhead int) int {
	if budget <= overhead {
		return 1
	}
	rows := budget - overhead
	if rows > maxRows {
		rows = maxRows
	}
	if rows < 1 {
		return 1
	}
	return rows
}

func joinDashSections(parts ...string) string {
	var kept []string
	for _, p := range parts {
		if strings.TrimSpace(p) != "" {
			kept = append(kept, p)
		}
	}
	if len(kept) == 0 {
		return ""
	}
	gap := dashSectionSep()
	return strings.Join(kept, gap)
}

func (m controlModel) renderDashBanner() string {
	if !m.ready {
		return dashAlertStyle.Render("  ◌  Loading agent data…")
	}
	if m.err == nil {
		return ""
	}
	var detail string
	if m.status.Uptime != "" {
		detail = fmt.Sprintf("Last known: up %s · %d scrapes", m.uptime, m.status.Scrapes)
	} else {
		detail = "Agent unreachable — check connection and press r to retry"
	}
	return dashAlertStyle.Render("  ⚠  Connection error") + "\n" +
		dashAlertDetailStyle.Render("     "+detail)
}

func (m controlModel) dashboardKPIs() []kpiDef {
	kpis := make([]kpiDef, 0, 9)
	kpis = append(kpis, []kpiDef{
		{"Uptime", truncUptime(m.status.Uptime), infoStyle, kpiToneInfo},
		{"Scrapes", fmt.Sprintf("%d", m.status.Scrapes), infoStyle, kpiToneInfo},
		{"Gate passed", fmt.Sprintf("%d", m.status.GatePassed), successStyle, kpiToneSuccess},
		{"Gate dropped", fmt.Sprintf("%d", m.status.GateDropped), warnStyle, kpiToneWarn},
		{"Predictions", fmt.Sprintf("%d", len(m.preds)), kpiValueStyleForCount(len(m.preds), 5), kpiToneFromCount(len(m.preds), 5)},
		{"Anomalies", fmt.Sprintf("%d", len(m.anomalies)), kpiValueStyleForCount(len(m.anomalies), 3), kpiToneFromCount(len(m.anomalies), 3)},
		{"Pending", fmt.Sprintf("%d", len(m.pending)), kpiValueStyleForCount(len(m.pending), 1), kpiToneFromCount(len(m.pending), 1)},
	}...)

	healthVal := "—"
	healthSty := mutedStyle
	healthTone := kpiToneNeutral
	if m.healthSum != nil {
		healthVal = fmt.Sprintf("%.0f", m.healthSum.OverallScore)
		healthSty = scoreStyle(m.healthSum.OverallScore / 100.0)
		healthTone = kpiToneFromScore(m.healthSum.OverallScore / 100.0)
	}
	kpis = append(kpis, kpiDef{"Health", healthVal, healthSty, healthTone})

	accVal := "—"
	accSty := mutedStyle
	accTone := kpiToneNeutral
	if m.accSnap != nil {
		accVal = fmt.Sprintf("%.1f%%", m.accSnap.Overall.F1Score*100)
		accSty = scoreStyle(m.accSnap.Overall.F1Score)
		accTone = kpiToneFromScore(m.accSnap.Overall.F1Score)
	}
	kpis = append(kpis, kpiDef{"Accuracy", accVal, accSty, accTone})

	return kpis
}

func (m controlModel) renderDashRecentSection(width, maxRows int) string {
	rows := buildRecentRows(m.preds, m.anomalies, dashRecentLimit)
	if len(rows) == 0 {
		body := lipgloss.JoinVertical(lipgloss.Left,
			successStyle.Render("  ✓ All clear"),
			mutedStyle.Render("  No active predictions or anomalies"),
		)
		return renderDashSection("Recent activity", body, width)
	}
	if maxRows < 1 {
		maxRows = 1
	}
	table := renderDashTable(colTitles(recentCols()), kwRowsToStrings(rows), width, min(maxRows, len(rows)))
	return renderDashSection("Recent activity", table, width)
}

func (m controlModel) renderDashHealthSection(width, maxRows int) string {
	rows := buildHealthRowsTop(m.healthScores, maxRows)
	if len(rows) == 0 {
		return ""
	}
	table := renderDashTable(colTitles(healthCols()), kwRowsToStrings(rows), width, len(rows))
	return renderDashSection("Cluster health", table, width)
}

func (m controlModel) renderDashApprovalsSection(width, maxRows int) string {
	groups := m.approvalGroups
	if len(groups) == 0 {
		groups = groupAuditRecords(m.pending, true)
	}
	rows := buildApprovalRowsTop(groups, maxRows)
	if len(rows) == 0 {
		return ""
	}
	table := renderDashTable(colTitles(approvalCols()), kwRowsToStrings(rows), width, len(rows))
	return renderDashSection("Pending approvals", table, width)
}

func renderDashSection(title, body string, width int) string {
	header := dashSectionTitleStyle.Width(width).Render("▎ " + title)
	return header + "\n" + body
}

// renderDashKPIGrid shows all metrics in 1–2 dense rows (no bordered cards).
func renderDashKPIGrid(kpis []kpiDef, width int) string {
	if len(kpis) == 0 {
		return ""
	}
	cols := dashKPICols
	if width >= 96 {
		cols = dashKPIColsWide
	}
	gap := 2
	cellW := (width - gap*(cols-1)) / cols
	if cellW < 10 {
		cellW = 10
	}

	var lines []string
	for rowStart := 0; rowStart < len(kpis); rowStart += cols {
		var cells []string
		for c := 0; c < cols; c++ {
			idx := rowStart + c
			if idx >= len(kpis) {
				break
			}
			k := kpis[idx]
			label := dashKPILabelStyle.Render(trunc(k.label, 11))
			value := k.style.Render(k.value)
			cell := lipgloss.NewStyle().Width(cellW).Render(label + " " + value)
			cells = append(cells, cell)
			if c < cols-1 && rowStart+c+1 < len(kpis) && c+1 < cols {
				cells = append(cells, strings.Repeat(" ", gap))
			}
		}
		lines = append(lines, lipgloss.JoinHorizontal(lipgloss.Top, cells...))
	}
	return strings.Join(lines, "\n")
}

func kpiToneFromCount(n, threshold int) kpiTone {
	if n >= threshold*2 {
		return kpiToneCritical
	}
	if n >= threshold {
		return kpiToneWarn
	}
	if n > 0 {
		return kpiToneInfo
	}
	return kpiToneNeutral
}

func kpiToneFromScore(score float64) kpiTone {
	switch {
	case score >= 0.8:
		return kpiToneSuccess
	case score >= 0.5:
		return kpiToneWarn
	default:
		return kpiToneCritical
	}
}

func renderDashTable(headers []string, rows [][]string, width, maxRows int) string {
	if len(headers) == 0 {
		return ""
	}
	t := lgtable.New().
		Headers(headers...).
		Width(width).
		Border(lipgloss.RoundedBorder()).
		BorderStyle(lipgloss.NewStyle().Foreground(colorBorder)).
		BorderTop(false).
		BorderLeft(false).
		BorderRight(false).
		BorderBottom(true).
		BorderColumn(false).
		BorderHeader(true).
		BorderRow(false).
		Wrap(false).
		StyleFunc(dashTableCellStyle)
	if maxRows > 0 {
		t = t.Height(min(maxRows+1, len(rows)+1)) // +1 header
	}
	for _, row := range rows {
		t = t.Row(row...)
	}
	return t.String()
}

func dashTableCellStyle(row, _ int) lipgloss.Style {
	base := lipgloss.NewStyle().Padding(0, 1)
	if row == lgtable.HeaderRow {
		return base.Bold(true).Foreground(colorMuted)
	}
	if row%2 == 0 {
		return base.Foreground(colorSubtle)
	}
	return base.Foreground(colorHighlight)
}

func renderDashAgentFooter(cfg *models.AgentConfig, width int) string {
	if cfg == nil {
		return ""
	}
	line := fmt.Sprintf(
		"%s %s  %s %s  %s %s/%s  %s %s",
		mutedStyle.Render("Agent"),
		cfg.ScrapeInterval,
		mutedStyle.Render("Prom"),
		trunc(cfg.PrometheusAddress, 28),
		mutedStyle.Render("LLM"),
		cfg.LLMProvider,
		trunc(cfg.LLMModel, 16),
		mutedStyle.Render("Mode"),
		cfg.Remediation.Mode,
	)
	return lipgloss.NewStyle().MarginTop(1).Width(width).Render(line)
}

func colTitles(cols []table.Column) []string {
	titles := make([]string, len(cols))
	for i, c := range cols {
		titles[i] = c.Title
	}
	return titles
}

func kwRowsToStrings(rows []table.Row) [][]string {
	out := make([][]string, len(rows))
	for i, row := range rows {
		out[i] = []string(row)
	}
	return out
}
