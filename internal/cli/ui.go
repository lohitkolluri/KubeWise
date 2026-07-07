package cli

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/help"
	"github.com/charmbracelet/bubbles/key"
	"github.com/charmbracelet/bubbles/spinner"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/lohitkolluri/KubeWise/pkg/models"
	"github.com/spf13/cobra"
)

var uiInterval time.Duration
var uiMouse bool
var uiAltScreen bool

func init() {
	uiCmd.Flags().DurationVar(&uiInterval, "interval", 2*time.Second, "auto-refresh interval")
	uiCmd.Flags().BoolVar(&uiMouse, "mouse", false, "enable mouse interactions (disables terminal text selection/copy in many terminals)")
	uiCmd.Flags().BoolVar(&uiAltScreen, "altscreen", true, "use terminal alternate screen buffer")
	rootCmd.AddCommand(uiCmd)
}

var uiCmd = &cobra.Command{
	Use:     "ui",
	Aliases: []string{"dashboard", "console"},
	Short:   "Interactive control center (TUI)",
	Long: `Launch the full KubeWise control center — live status, predictions,
anomalies, remediations, config editing, and agent logs in one screen.

  ←/→ / 1-6     switch panels
  ctrl+p          command palette
  ?               toggle keybinding help
  j/k / ↑↓        scroll lists & logs
  g / G           top / bottom of list
  r               refresh
  enter           detail view
  R               restart agent (confirmed)
  q               quit`,
	RunE: func(cmd *cobra.Command, args []string) error {
		return runControlCenter(uiInterval)
	},
}

func runControlCenter(interval time.Duration) error {
	if interval < time.Second {
		interval = 2 * time.Second
	}
	m := newControlModel(interval)
	opts := []tea.ProgramOption{}
	if uiAltScreen {
		opts = append(opts, tea.WithAltScreen())
	}
	if uiMouse {
		// NOTE: mouse capture prevents normal terminal selection/copy in many terminals.
		opts = append(opts, tea.WithMouseCellMotion())
	}
	p := tea.NewProgram(m, opts...)
	_, err := p.Run()
	return err
}

const (
	tabDashboard = iota
	tabPredict
	tabAnomalies
	tabAudit
	tabApprovals
	tabConfig
	tabLogs
	tabCount
)

var tabLabels = []string{"Dashboard", "Predict", "Anomalies", "Audit", "Approvals", "Config", "Logs"}

type confirmKind int

const (
	confirmNone confirmKind = iota
	confirmRestart
	confirmEnableLive
)

type controlModel struct {
	interval    time.Duration
	tab         int
	width       int
	height      int
	detail      string
	detailTitle string
	statusMsg   string
	err         error
	lastUpdate  time.Time
	loading     bool
	ready       bool
	confirm     confirmKind
	logsFollow  bool
	auditStatus string
	auditSince  string

	status    agentStatus
	healthOK  bool
	preds     []models.PredictionResult
	anomalies []models.AnomalyRecord
	audits    []models.AuditRecord
	config    *models.AgentConfig
	remMode   models.RemediationModeView
	pending   []models.AuditRecord
	logs      string
	logsVP    viewport.Model

	cursor [tabCount]int
	keys   uiKeyMap
	help   help.Model
	spin   spinner.Model

	palette           paletteState
	paletteInputTitle string
	paletteInputApply func(m *controlModel, value string) (string, error)
	paletteQuitApp    bool
}

func newControlModel(interval time.Duration) controlModel {
	s := spinner.New()
	s.Spinner = spinner.Dot
	s.Style = lipgloss.NewStyle().Foreground(colorAccent)

	m := controlModel{
		interval: interval,
		tab:      tabDashboard,
		palette:  newPaletteState(),
		keys:     defaultUIKeys(),
		spin:     s,
		logsFollow: true,
	}
	m.help = help.New()
	m.help.ShowAll = false
	m.logsVP = viewport.New(80, 20)
	m.logsVP.MouseWheelEnabled = true
	return m
}

func (m controlModel) Init() tea.Cmd {
	return tea.Batch(
		m.spin.Tick,
		m.refreshAll(),
		scheduleTick(m.interval),
	)
}

type uiTickMsg time.Time
type toastClearMsg struct{}

func scheduleTick(d time.Duration) tea.Cmd {
	return tea.Tick(d, func(t time.Time) tea.Msg { return uiTickMsg(t) })
}

func scheduleToastClear() tea.Cmd {
	return tea.Tick(3*time.Second, func(time.Time) tea.Msg { return toastClearMsg{} })
}

func (m controlModel) refreshAll() tea.Cmd {
	return func() tea.Msg {
		st, err := fetchStatus()
		if err != nil {
			return dataMsg{err: err}
		}
		_, herr := fetchHealth()
		preds, _ := fetchPredictions()
		anomalies, _ := fetchAnomalies(30)
		audits, _ := fetchAuditFiltered(25, m.auditStatus, m.auditSince, "")
		cfg, cfgErr := fetchAgentConfig()
		if cfgErr != nil && strings.Contains(cfgErr.Error(), "no config") {
			cfg = nil
		}
		logs, _ := fetchAgentLogs(80)
		remMode, _ := fetchRemediationMode()
		pending, _ := fetchApprovals(30)
		return dataMsg{
			status:     st,
			healthOK:   herr == nil,
			preds:      preds,
			anomalies:  anomalies,
			audits:     audits,
			config:     cfg,
			remMode:    remMode,
			pending:    pending,
			logs:       logs,
			lastUpdate: time.Now(),
		}
	}
}

type dataMsg struct {
	status     agentStatus
	healthOK   bool
	preds      []models.PredictionResult
	anomalies  []models.AnomalyRecord
	audits     []models.AuditRecord
	config     *models.AgentConfig
	remMode    models.RemediationModeView
	pending    []models.AuditRecord
	logs       string
	err        error
	lastUpdate time.Time
}

func (m controlModel) shouldPauseRefresh() bool {
	return m.palette.phase != paletteClosed || m.detail != "" || m.confirm != confirmNone
}

func (m controlModel) contentHeight() int {
	h := m.height - uiHeaderLines - uiTabLines - uiFooterLines - 2
	if m.help.ShowAll {
		h -= 2
	}
	if h < 6 {
		return 6
	}
	return h
}

func (m controlModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.help.Width = msg.Width
		m.logsVP.Width = msg.Width - 6
		m.logsVP.Height = m.contentHeight()
		m.resizePalette()
		return m, nil

	case spinner.TickMsg:
		var cmd tea.Cmd
		m.spin, cmd = m.spin.Update(msg)
		return m, cmd

	case toastClearMsg:
		if m.statusMsg != "" && m.statusMsg != "Refreshing…" {
			m.statusMsg = ""
		}
		return m, nil

	case tea.KeyMsg:
		if m.palette.phase != paletteClosed {
			return m.handlePaletteKey(msg)
		}
		if m.confirm != confirmNone {
			return m.handleConfirmKey(msg)
		}
		if m.detail != "" {
			if key.Matches(msg, m.keys.Back) || key.Matches(msg, m.keys.Quit) {
				m.detail = ""
				m.detailTitle = ""
			}
			return m, nil
		}
		return m.handleMainKey(msg)

	case uiTickMsg:
		if m.shouldPauseRefresh() || m.loading {
			return m, scheduleTick(m.interval)
		}
		m.loading = true
		return m, tea.Batch(m.refreshAll(), scheduleTick(m.interval))

	case dataMsg:
		m.loading = false
		m.ready = true
		if msg.err != nil {
			m.err = msg.err
		} else {
			m.err = nil
			m.status = msg.status
			m.healthOK = msg.healthOK
			m.preds = msg.preds
			m.anomalies = msg.anomalies
			m.audits = msg.audits
			m.config = msg.config
			m.remMode = msg.remMode
			m.pending = msg.pending
			m.logs = msg.logs
			m.lastUpdate = msg.lastUpdate
			m.logsVP.SetContent(msg.logs)
			if m.logsFollow {
				m.logsVP.GotoBottom()
			}
			if m.statusMsg == "Refreshing…" {
				m.statusMsg = ""
			}
		}
		return m, nil
	}
	return m, nil
}

func (m *controlModel) handleConfirmKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	if key.Matches(msg, m.keys.Confirm) {
		kind := m.confirm
		m.confirm = confirmNone
		if kind == confirmRestart {
			if err := restartAgentDeployment(); err != nil {
				m.err = err
			} else {
				m.statusMsg = "Agent deployment restarting…"
				m.err = nil
			}
			return m, scheduleToastClear()
		}
		if kind == confirmEnableLive {
			mode, err := setRemediationLive(true)
			if err != nil {
				m.err = err
			} else {
				m.remMode = mode
				m.statusMsg = "LIVE mode — remediations will execute"
				m.err = nil
			}
			return m, tea.Batch(m.refreshAll(), scheduleToastClear())
		}
		return m, nil
	}
	if key.Matches(msg, m.keys.Cancel) {
		m.confirm = confirmNone
		m.statusMsg = "Cancelled"
		return m, scheduleToastClear()
	}
	return m, nil
}

func (m *controlModel) handleMainKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch {
	case key.Matches(msg, m.keys.Quit):
		return m, tea.Quit
	case key.Matches(msg, m.keys.Palette):
		return m, m.openPalette()
	case key.Matches(msg, m.keys.Help):
		m.help.ShowAll = !m.help.ShowAll
		m.logsVP.Height = m.contentHeight()
		return m, nil
	case key.Matches(msg, m.keys.Refresh):
		m.loading = true
		m.statusMsg = "Refreshing…"
		return m, m.refreshAll()
	case key.Matches(msg, m.keys.TabPrev):
		m.tab = (m.tab + tabCount - 1) % tabCount
		m.clampCursor()
	case key.Matches(msg, m.keys.TabNext):
		m.tab = (m.tab + 1) % tabCount
		m.clampCursor()
	case key.Matches(msg, m.keys.Tab1):
		m.tab = tabDashboard
		m.clampCursor()
	case key.Matches(msg, m.keys.Tab2):
		m.tab = tabPredict
		m.clampCursor()
	case key.Matches(msg, m.keys.Tab3):
		m.tab = tabAnomalies
		m.clampCursor()
	case key.Matches(msg, m.keys.Tab4):
		m.tab = tabAudit
		m.clampCursor()
	case key.Matches(msg, m.keys.Tab5):
		m.tab = tabApprovals
		m.clampCursor()
	case key.Matches(msg, m.keys.Tab6):
		m.tab = tabConfig
		m.clampCursor()
	case key.Matches(msg, m.keys.Tab7):
		m.tab = tabLogs
		m.clampCursor()
	case key.Matches(msg, m.keys.Down):
		if m.tab == tabLogs {
			var cmd tea.Cmd
			m.logsVP, cmd = m.logsVP.Update(msg)
			return m, cmd
		}
		m.cursor[m.tab]++
		m.clampCursor()
	case key.Matches(msg, m.keys.Up):
		if m.tab == tabLogs {
			var cmd tea.Cmd
			m.logsVP, cmd = m.logsVP.Update(msg)
			return m, cmd
		}
		if m.cursor[m.tab] > 0 {
			m.cursor[m.tab]--
		}
	case key.Matches(msg, m.keys.Top):
		m.cursor[m.tab] = 0
	case key.Matches(msg, m.keys.Bottom):
		m.cursor[m.tab] = m.listLen(m.tab) - 1
		if m.cursor[m.tab] < 0 {
			m.cursor[m.tab] = 0
		}
	case key.Matches(msg, m.keys.Detail):
		m.showDetailForSelection()
	case key.Matches(msg, m.keys.DryRun), key.Matches(msg, m.keys.ToggleLive):
		m.toggleRemediationMode()
	case key.Matches(msg, m.keys.Approve):
		if m.tab == tabApprovals && len(m.pending) > 0 {
			i := m.cursor[tabApprovals]
			if i >= 0 && i < len(m.pending) {
				if err := approveRemediation(m.pending[i].ID); err != nil {
					m.err = err
				} else {
					m.statusMsg = "Approved & executed"
					m.err = nil
					return m, tea.Batch(m.refreshAll(), scheduleToastClear())
				}
			}
		}
	case key.Matches(msg, m.keys.Reject):
		if m.tab == tabApprovals && len(m.pending) > 0 {
			i := m.cursor[tabApprovals]
			if i >= 0 && i < len(m.pending) {
				id := m.pending[i].ID
				return m, m.openPrompt(palettePrompt{
					title:       "Reject remediation",
					placeholder: "reason (required)",
					value:       "rejected via kwctl",
					apply: func(m *controlModel, value string) (string, error) {
						if strings.TrimSpace(value) == "" {
							return "", fmt.Errorf("reason must not be empty")
						}
						if err := rejectRemediation(id, value); err != nil {
							return "", err
						}
						return "Remediation rejected", nil
					},
				})
			}
		}
	case key.Matches(msg, m.keys.LogFollow):
		if m.tab == tabLogs {
			m.logsFollow = !m.logsFollow
			if m.logsFollow {
				m.logsVP.GotoBottom()
				m.statusMsg = "logs: follow ON"
			} else {
				m.statusMsg = "logs: follow OFF"
			}
			return m, scheduleToastClear()
		}
	case key.Matches(msg, m.keys.Mode):
		if m.tab == tabConfig && m.config != nil {
			m.config.Remediation.Mode = nextRemediationMode(m.config.Remediation.Mode)
			if err := putAgentConfig(m.config); err != nil {
				m.err = err
			} else {
				m.statusMsg = fmt.Sprintf("mode=%s saved", m.config.Remediation.Mode)
				m.err = nil
				return m, scheduleToastClear()
			}
		}
	case key.Matches(msg, m.keys.Restart):
		m.confirm = confirmRestart
		return m, nil
	default:
		if m.tab == tabLogs {
			var cmd tea.Cmd
			m.logsVP, cmd = m.logsVP.Update(msg)
			return m, cmd
		}
	}
	return m, nil
}

func (m *controlModel) requestRestart() {
	m.confirm = confirmRestart
}

func (m *controlModel) clampCursor() {
	max := m.listLen(m.tab) - 1
	if max < 0 {
		max = 0
	}
	if m.cursor[m.tab] > max {
		m.cursor[m.tab] = max
	}
}

func (m controlModel) listLen(tab int) int {
	switch tab {
	case tabPredict:
		return len(m.preds)
	case tabAnomalies:
		return len(m.anomalies)
	case tabAudit:
		return len(m.audits)
	case tabApprovals:
		return len(m.pending)
	default:
		return 0
	}
}

func nextRemediationMode(current string) string {
	switch current {
	case "", models.RemediationModeDryRun:
		return models.RemediationModeAuto
	case models.RemediationModeAuto:
		return models.RemediationModeOff
	case models.RemediationModeOff:
		return models.RemediationModeSemi
	default:
		return models.RemediationModeDryRun
	}
}

func (m controlModel) View() string {
	if m.width == 0 {
		return logoStyle.Render(" KubeWise ") + "\n" + mutedStyle.Render("Connecting…")
	}

	var b strings.Builder
	b.WriteString(m.renderHeader())
	b.WriteString("\n")
	b.WriteString(m.renderTabs())
	b.WriteString("\n")

	if m.detail != "" {
		b.WriteString(m.renderDetail())
	} else {
		content := m.renderTabContent()
		b.WriteString(panelStyle.Width(m.width - 2).Height(m.contentHeight()).Render(content))
	}
	b.WriteString("\n")
	b.WriteString(m.renderFooter())

	base := b.String()
	if m.confirm != confirmNone {
		base += "\n" + m.renderConfirm()
	}
	if m.palette.phase != paletteClosed {
		return m.renderPaletteOverlay(base)
	}
	return base
}

func (m controlModel) renderHeader() string {
	health := statusOKStyle.Render("● healthy")
	if !m.healthOK && m.ready {
		health = errStyle.Render("● unreachable")
	}
	if !m.ready {
		health = mutedStyle.Render("● connecting")
	}

	sync := ""
	if m.loading {
		sync = m.spin.View() + " "
	}

	left := logoStyle.Render("KubeWise") + " " + brandStyle.Render("control center") + " " + m.renderModeBadge()
	right := fmt.Sprintf("%s%s  %s  %s",
		sync,
		health,
		mutedStyle.Render(trunc(resolveAgentURL(), 36)),
		mutedStyle.Render(m.lastUpdate.Format("15:04:05")))
	gap := m.width - lipgloss.Width(left) - lipgloss.Width(right) - 2
	if gap < 1 {
		gap = 1
	}
	return left + strings.Repeat(" ", gap) + right
}

func (m controlModel) renderTabs() string {
	var parts []string
	for i, label := range tabLabels {
		style := tabInactiveStyle
		if i == m.tab {
			style = tabActiveStyle
		}
		marker := ""
		if i == m.tab {
			marker = " ◂"
		}
		parts = append(parts, style.Render(fmt.Sprintf("%d %s%s", i+1, label, marker)))
	}
	hint := mutedStyle.Render("  ←/→")
	return strings.Join(parts, "") + hint
}

func (m controlModel) renderTabContent() string {
	if !m.ready && m.loading {
		return emptyStateStyle.Render("Loading agent data…")
	}
	if m.err != nil {
		return errStyle.Render("⚠ "+m.err.Error()) + "\n\n" + mutedStyle.Render("Press r to retry, or ctrl+p for commands")
	}
	switch m.tab {
	case tabDashboard:
		return m.renderDashboard()
	case tabPredict:
		return m.renderList(m.predsLines(), m.cursor[tabPredict], len(m.preds))
	case tabAnomalies:
		return m.renderList(m.anomalyLines(), m.cursor[tabAnomalies], len(m.anomalies))
	case tabAudit:
		return m.renderList(m.auditLines(), m.cursor[tabAudit], len(m.audits))
	case tabApprovals:
		return m.renderList(m.approvalLines(), m.cursor[tabApprovals], len(m.pending))
	case tabConfig:
		return m.renderConfig()
	case tabLogs:
		if m.logs == "" {
			return emptyStateStyle.Render("No logs available.\nEnsure kubeconfig can reach the cluster and the agent pod is running.")
		}
		follow := "follow=ON"
		if !m.logsFollow {
			follow = "follow=OFF"
		}
		scroll := mutedStyle.Render(fmt.Sprintf("lines %d · scroll ↑↓ pgup/pgdn", strings.Count(m.logs, "\n")+1))
		return scroll + mutedStyle.Render(" · " + follow + " (f)") + "\n" + m.logsVP.View()
	default:
		return ""
	}
}

func (m controlModel) renderDashboard() string {
	var b strings.Builder
	stats := []struct{ label, value string }{
		{"Uptime", m.status.Uptime},
		{"Scrapes", fmt.Sprintf("%d", m.status.Scrapes)},
		{"Gate pass", fmt.Sprintf("%d", m.status.GatePassed)},
		{"Gate drop", fmt.Sprintf("%d", m.status.GateDropped)},
		{"Predictions", fmt.Sprintf("%d", len(m.preds))},
		{"Anomalies", fmt.Sprintf("%d", len(m.anomalies))},
		{"Pending", fmt.Sprintf("%d", len(m.pending))},
	}
	cols := 3
	cellW := (m.width - 8) / cols
	if cellW < 10 {
		cellW = 10
	}
	for i, s := range stats {
		if i > 0 && i%cols == 0 {
			b.WriteString("\n")
		}
		cell := statLabelStyle.Render(s.label+":") + " " + statValueStyle.Render(s.value)
		b.WriteString(lipgloss.NewStyle().Width(cellW).Render(cell))
	}
	b.WriteString("\n\n")
	b.WriteString(brandStyle.Render("Recent activity"))
	b.WriteString("\n")
	if len(m.preds) == 0 && len(m.anomalies) == 0 {
		b.WriteString(emptyStateStyle.Render("  ✓ All clear — no active predictions or anomalies"))
	} else {
		for i, p := range m.preds {
			if i >= 3 {
				break
			}
			b.WriteString(fmt.Sprintf("  ▸ %s %s %.0f%% ETA %.0fs\n",
				warnStyle.Render(trunc(p.Type, 12)), trunc(p.Entity, 24), p.Score*100, p.ETASeconds))
		}
		for i, a := range m.anomalies {
			if i >= 3 {
				break
			}
			pat := a.Pattern
			if pat == "" {
				pat = "statistical"
			}
			b.WriteString(fmt.Sprintf("  ▸ %s %s %.2f %s\n",
				errStyle.Render(trunc(pat, 12)), trunc(a.Entity, 24), a.Score, a.Status))
		}
	}
	if m.config != nil {
		b.WriteString("\n")
		b.WriteString(brandStyle.Render("Config"))
		b.WriteString("\n")
		b.WriteString(formatConfigSummary(m.config))
	}
	return b.String()
}

func (m controlModel) predsLines() []string {
	if len(m.preds) == 0 {
		return []string{emptyStateStyle.Render("No active predictions")}
	}
	var lines []string
	lines = append(lines, mutedStyle.Render(fmt.Sprintf("%-12s %-22s %-8s %-8s %s", "TYPE", "ENTITY", "SCORE", "ETA", "ACTION")))
	for _, p := range m.preds {
		lines = append(lines, fmt.Sprintf("%-12s %-22s %-8.2f %-8.0f %s",
			trunc(p.Type, 12), trunc(p.Entity, 22), p.Score, p.ETASeconds, trunc(p.Action, 20)))
	}
	return lines
}

func (m controlModel) anomalyLines() []string {
	if len(m.anomalies) == 0 {
		return []string{emptyStateStyle.Render("No anomalies")}
	}
	var lines []string
	lines = append(lines, mutedStyle.Render(fmt.Sprintf("%-12s %-22s %-8s %s", "PATTERN", "ENTITY", "SCORE", "STATUS")))
	for _, a := range m.anomalies {
		pat := a.Pattern
		if pat == "" {
			pat = "statistical"
		}
		lines = append(lines, fmt.Sprintf("%-12s %-22s %-8.2f %s",
			trunc(pat, 12), trunc(a.Entity, 22), a.Score, a.Status))
	}
	return lines
}

func (m controlModel) auditLines() []string {
	if len(m.audits) == 0 {
		return []string{emptyStateStyle.Render("No remediation records")}
	}
	var lines []string
	lines = append(lines, mutedStyle.Render(fmt.Sprintf("%-12s %-28s %-8s %s", "STATUS", "ACTION", "TIER", "REASON")))
	for _, r := range m.audits {
		action := fmt.Sprintf("%s/%s", r.Plan.Action.Type, r.Plan.Action.Target)
		status := string(r.Status)
		// Make it explicit when the system rejected a plan vs an operator rejection.
		if r.Status == models.AuditRejected {
			low := strings.ToLower(strings.TrimSpace(r.Reason))
			if !strings.Contains(low, "operator") && !strings.Contains(low, "kwctl") {
				status = "auto_rejected"
			}
		}
		lines = append(lines, fmt.Sprintf("%-12s %-28s %-8s %s",
			trunc(status, 12), trunc(action, 26), r.RiskTier, trunc(r.Reason, 40)))
	}
	return lines
}

func (m controlModel) renderList(lines []string, cursor, total int) string {
	var b strings.Builder
	for i, line := range lines {
		if i == 0 {
			b.WriteString(line)
			b.WriteString("\n")
			continue
		}
		if i-1 == cursor {
			b.WriteString(listSelectedStyle.Render("› " + line))
			b.WriteString("\n")
		} else {
			b.WriteString("  ")
			b.WriteString(line)
			b.WriteString("\n")
		}
	}
	if total > 0 {
		b.WriteString(mutedStyle.Render(fmt.Sprintf("\n%d/%d · enter detail · g/G top/bottom", cursor+1, total)))
	}
	return b.String()
}

func (m controlModel) renderConfig() string {
	if m.config == nil {
		return emptyStateStyle.Render("No agent config saved on agent.")
	}
	var b strings.Builder
	b.WriteString(formatConfigSummary(m.config))
	b.WriteString("\n")
	b.WriteString(mutedStyle.Render("ctrl+p commands  ·  "))
	b.WriteString(keyStyle.Render("d/L"))
	b.WriteString(mutedStyle.Render(" observe/live  "))
	b.WriteString(keyStyle.Render("m"))
	b.WriteString(mutedStyle.Render(" mode  "))
	b.WriteString(keyStyle.Render("R"))
	b.WriteString(mutedStyle.Render(" restart"))
	return b.String()
}

func (m controlModel) renderDetail() string {
	title := brandStyle.Render(m.detailTitle)
	body := panelStyle.Width(m.width - 6).Render(m.detail)
	help := mutedStyle.Render("\nesc back · q quit")
	return title + "\n" + body + help
}

func (m controlModel) renderFooter() string {
	var b strings.Builder
	if m.statusMsg != "" {
		b.WriteString(statusOKStyle.Render(m.statusMsg))
		b.WriteString("\n")
	}
	if m.help.ShowAll {
		b.WriteString(m.help.View(m.keys))
		b.WriteString("\n")
	} else {
		b.WriteString(m.help.ShortHelpView(m.keys.ShortHelp()))
		b.WriteString("\n")
	}
	return statusBarStyle.Width(m.width).Render(strings.TrimRight(b.String(), "\n"))
}

func (m controlModel) renderModeBadge() string {
	if m.remMode.Live {
		return warnStyle.Render("[LIVE]")
	}
	return statusOKStyle.Render("[OBSERVE]")
}

func (m controlModel) renderConfirm() string {
	var body string
	switch m.confirm {
	case confirmEnableLive:
		body = warnStyle.Render("Enable LIVE remediation?") + "\n" +
			mutedStyle.Render("T1/T2 actions will execute automatically. T3 still needs approval.") + "\n\n" +
			keyStyle.Render("y") + mutedStyle.Render(" enable  ") +
			keyStyle.Render("n") + mutedStyle.Render(" cancel")
	default:
		body = warnStyle.Render("Restart agent deployment?") + "\n" +
			mutedStyle.Render(fmt.Sprintf("Rolling restart %s/%s.", agentNS, agentSvc)) + "\n\n" +
			keyStyle.Render("y") + mutedStyle.Render(" confirm  ") +
			keyStyle.Render("n") + mutedStyle.Render(" cancel")
	}
	return lipgloss.Place(m.width, 6, lipgloss.Center, lipgloss.Bottom,
		confirmBoxStyle.Width(min(56, m.width-4)).Render(body))
}

func (m *controlModel) toggleRemediationMode() {
	if m.remMode.Live {
		mode, err := setRemediationLive(false)
		if err != nil {
			m.err = err
			return
		}
		m.remMode = mode
		m.statusMsg = "OBSERVE mode — dry-run only"
		m.err = nil
		return
	}
	m.confirm = confirmEnableLive
}

func (m controlModel) approvalLines() []string {
	if len(m.pending) == 0 {
		return []string{emptyStateStyle.Render("No pending approvals — T3 actions appear here when live")}
	}
	var lines []string
	lines = append(lines, mutedStyle.Render(fmt.Sprintf("%-10s %-28s %-8s %s", "TIER", "ACTION", "CONF", "REASON")))
	for _, r := range m.pending {
		action := fmt.Sprintf("%s %s/%s", r.Plan.Action.Type, r.Plan.Action.Namespace, r.Plan.Action.Target)
		lines = append(lines, fmt.Sprintf("%-10s %-28s %-8.2f %s",
			r.RiskTier, trunc(action, 26), r.Plan.Diagnosis.Confidence, trunc(r.Reason, 40)))
	}
	return lines
}

func (m controlModel) renderPaletteOverlay(base string) string {
	_ = base // palette replaces view while open for focus
	return m.renderPalette()
}

func (m *controlModel) showDetailForSelection() {
	switch m.tab {
	case tabPredict:
		i := m.cursor[tabPredict]
		if i < 0 || i >= len(m.preds) {
			return
		}
		p := m.preds[i]
		m.detailTitle = "Prediction"
		m.detail = fmt.Sprintf("Type:      %s\nEntity:    %s\nNamespace: %s\nScore:     %.2f\nETA:       %.0fs\nAction:    %s\nMetric:    %s",
			p.Type, p.Entity, p.Namespace, p.Score, p.ETASeconds, p.Action, p.MetricName)
	case tabAnomalies:
		i := m.cursor[tabAnomalies]
		if i < 0 || i >= len(m.anomalies) {
			return
		}
		a := m.anomalies[i]
		m.detailTitle = "Anomaly " + trunc(a.ID, 12)
		m.detail = fmt.Sprintf("ID:        %s\nEntity:    %s\nNamespace: %s\nPattern:   %s\nMetric:    %s\nScore:     %.2f\nStatus:    %s",
			a.ID, a.Entity, a.Namespace, a.Pattern, a.MetricName, a.Score, a.Status)
	case tabAudit:
		i := m.cursor[tabAudit]
		if i < 0 || i >= len(m.audits) {
			return
		}
		r := m.audits[i]
		m.detailTitle = "Remediation"
		m.detail = fmt.Sprintf("ID:         %s\nStatus:     %s\nTier:       %s\nAction:     %s\nTarget:     %s/%s\nReason:     %s\nConfidence: %.2f",
			r.ID, r.Status, r.RiskTier, r.Plan.Action.Type, r.Plan.Action.Namespace, r.Plan.Action.Target, r.Reason, r.Plan.Diagnosis.Confidence)
	case tabApprovals:
		i := m.cursor[tabApprovals]
		if i < 0 || i >= len(m.pending) {
			return
		}
		r := m.pending[i]
		m.detailTitle = "Pending approval " + trunc(r.ID, 12)
		m.detail = fmt.Sprintf("ID:         %s\nTier:       %s (requires approval)\nAction:     %s\nTarget:     %s/%s\nRoot cause: %s\nConfidence: %.2f\nBlast:      %s\nReversible: %v\n\nPress a to approve, x to reject",
			r.ID, r.RiskTier, r.Plan.Action.Type, r.Plan.Action.Namespace, r.Plan.Action.Target,
			r.Plan.Diagnosis.RootCause, r.Plan.Diagnosis.Confidence,
			r.Plan.Risk.BlastRadius, r.Plan.Risk.Reversible)
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
