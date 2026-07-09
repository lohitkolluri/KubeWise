package cli

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/help"
	"github.com/charmbracelet/bubbles/key"
	"github.com/charmbracelet/bubbles/spinner"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/muesli/reflow/truncate"
	"github.com/spf13/cobra"

	"github.com/lohitkolluri/KubeWise/pkg/models"
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

  Navigation:
    ←/→ / 1-8     switch panels
    ↑/↓ / j/k     scroll lists
    g / G         top / bottom of list
    enter         detail view
    esc           back / close

  Commands:
    ctrl+p        command palette
    ?             toggle keybinding help
    r             refresh
    d/l           toggle observe/live mode
    m             cycle remediation mode
    R             restart agent (confirmed)
    q             quit

  Approvals:
    a             approve remediation
    x             reject remediation
`,
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
		opts = append(opts, tea.WithMouseCellMotion())
	}
	p := tea.NewProgram(m, opts...)
	_, err := p.Run()
	return err
}

// ── Tab constants ──

const (
	tabDashboard = iota
	tabPredict
	tabAnomalies
	tabAudit
	tabApprovals
	tabConfig
	tabLogs
	tabHealth
	tabCount
)

var (
	tabLabels = []string{"Dashboard", "Predict", "Anomalies", "Audit", "Approvals", "Config", "Logs", "Health"}
	tabIcons  = []string{"◎", "◈", "▲", "◆", "★", "⚙", "¶", "♥"} //nolint:unused
)

// ── Model ──

type confirmKind int

const (
	confirmNone confirmKind = iota
	confirmRestart
	confirmEnableLive
	confirmApproveRemediation
)

type controlModel struct {
	interval time.Duration
	tab      int
	width    int
	height   int

	// Data
	status         agentStatus
	healthOK       bool
	preds          []models.PredictionResult
	anomalies      []models.AnomalyRecord
	audits         []models.AuditRecord
	auditGroups    []auditGroup
	config         *models.AgentConfig
	remMode        models.RemediationModeView
	pending        []models.AuditRecord
	approvalGroups []auditGroup
	logs           string

	// Health & Accuracy
	healthScores []models.HealthScore
	healthSum    *models.ClusterHealthSummary
	accSnap      *models.AccuracySnapshot

	// Per-tab loading & error state
	loading    bool
	loadingTab [tabCount]bool //nolint:unused
	err        error
	errTab     [tabCount]error

	// Detail view
	detail         string
	detailMeta     string
	detailTitle    string
	detailVP       viewport.Model
	detailVPHeight int
	detailContent  string
	detailWrapW    int

	// Status bar
	statusMsg  string
	lastUpdate time.Time
	uptime     string

	// Mode state
	ready       bool
	logsFollow  bool
	auditStatus string
	auditSince  string

	// Confirmations
	confirm         confirmKind
	confirmTargetID string // for confirmApproveRemediation

	// UI components
	cursor [tabCount]int
	keys   uiKeyMap
	help   help.Model
	spin   spinner.Model
	logsVP viewport.Model

	// Palette
	palette           paletteState
	paletteInputTitle string
	paletteInputApply func(m *controlModel, value string) (string, error)
	paletteQuitApp    bool
}

type auditGroup struct {
	Key     string
	Records []models.AuditRecord
}

func newControlModel(interval time.Duration) controlModel {
	s := spinner.New()
	s.Spinner = spinner.Dot
	s.Style = lipgloss.NewStyle().Foreground(colorAccent)

	m := controlModel{
		interval:   interval,
		tab:        tabDashboard,
		palette:    newPaletteState(),
		keys:       defaultUIKeys(),
		spin:       s,
		logsFollow: true,
		detailVP:   viewport.New(80, 10),
	}
	m.help = help.New()
	m.help.ShowAll = false
	m.logsVP = viewport.New(80, 20)
	m.logsVP.MouseWheelEnabled = true
	m.detailVP.MouseWheelEnabled = true
	m.detailVP.Style = lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(colorAccent).
		Padding(0, 1)
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

func (m controlModel) logsAtBottom() bool {
	total := m.logsVP.TotalLineCount()
	if total <= 0 {
		return true
	}
	bottom := total - m.logsVP.Height
	if bottom < 0 {
		bottom = 0
	}
	return m.logsVP.YOffset >= bottom
}

func (m *controlModel) maybeDisableLogsFollowAfterScroll() tea.Cmd {
	if m.tab != tabLogs {
		return nil
	}
	if m.logsFollow && !m.logsAtBottom() {
		m.logsFollow = false
		m.statusMsg = "logs: follow OFF (scrolled)"
		return scheduleToastClear()
	}
	return nil
}

func (m controlModel) refreshAll() tea.Cmd {
	m.loading = true
	return func() tea.Msg {
		r := fetchAll(m.auditStatus, m.auditSince)
		return dataMsg{
			status:       r.status,
			healthOK:     r.healthOK,
			preds:        r.preds,
			anomalies:    r.anomalies,
			audits:       r.audits,
			config:       r.config,
			remMode:      r.remMode,
			pending:      r.pending,
			logs:         r.logs,
			lastUpdate:   r.lastUpdate,
			err:          r.err,
			predErr:      r.predErr,
			anomErr:      r.anomErr,
			auditErr:     r.auditErr,
			configErr:    r.configErr,
			remErr:       r.remErr,
			pendErr:      r.pendErr,
			logsErr:      r.logsErr,
			healthErr:    r.healthErr,
			accErr:       r.accErr,
			healthScores: r.healthScores,
			healthSum:    r.healthSum,
			accSnap:      r.accSnap,
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

	// Health & Accuracy
	healthScores []models.HealthScore
	healthSum    *models.ClusterHealthSummary
	accSnap      *models.AccuracySnapshot

	// Per-tab errors
	predErr   error
	anomErr   error
	auditErr  error
	configErr error
	remErr    error
	pendErr   error
	logsErr   error
	healthErr error
	accErr    error
}

func (m controlModel) shouldPauseRefresh() bool {
	return m.palette.phase != paletteClosed || m.detail != "" || m.detailMeta != "" || m.confirm != confirmNone
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

// ── Update ──

func (m controlModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.help.Width = msg.Width
		// Clamp viewport dimensions for tiny terminals.
		vw := msg.Width - 6
		if vw < 1 {
			vw = 1
		}
		vh := m.contentHeight()
		if vh < 1 {
			vh = 1
		}
		m.logsVP.Width = vw
		m.logsVP.Height = vh
		m.detailVP.Width = vw
		m.detailVPHeight = vh
		m.resizeDetailViewport()
		if m.detail != "" || m.detailMeta != "" {
			m.relayoutDetail()
		}
		m.resizePalette()
		return m, nil

	case spinner.TickMsg:
		var cmd tea.Cmd
		m.spin, cmd = m.spin.Update(msg)
		return m, cmd

	case toastClearMsg:
		if m.statusMsg != "" {
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
				m.detailMeta = ""
				m.detailTitle = ""
				m.detailContent = ""
				return m, nil
			}
			// Let the viewport handle scrolling keys (arrows, pgup/pgdn, mouse wheel, etc.)
			var cmd tea.Cmd
			m.detailVP, cmd = m.detailVP.Update(msg)
			return m, cmd
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

		// Track uptime from status
		if msg.err == nil {
			m.uptime = msg.status.Uptime
		}

		// Per-tab error tracking
		m.errTab[tabPredict] = msg.predErr
		m.errTab[tabAnomalies] = msg.anomErr
		m.errTab[tabAudit] = msg.auditErr
		m.errTab[tabConfig] = msg.configErr
		m.errTab[tabLogs] = msg.logsErr
		// Approvals and rem mode errors
		m.errTab[tabApprovals] = msg.pendErr
		m.errTab[tabHealth] = msg.healthErr
		m.errTab[tabDashboard] = msg.err

		// Set global error only if all tabs failed (connection issue)
		if msg.err != nil {
			// Status/health failed — connection problem
			m.err = msg.err
		} else {
			m.err = nil
		}

		if msg.err == nil {
			m.status = msg.status
			m.healthOK = msg.healthOK
			m.preds = msg.preds
			m.anomalies = msg.anomalies
			m.audits = msg.audits
			m.auditGroups = groupAuditRecords(msg.audits, false)
			m.config = msg.config
			m.remMode = msg.remMode
			m.pending = msg.pending
			m.approvalGroups = groupAuditRecords(msg.pending, true)
			m.logs = msg.logs
			m.healthScores = msg.healthScores
			m.healthSum = msg.healthSum
			m.accSnap = msg.accSnap
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

// ── Confirmation handling ──

func (m *controlModel) handleConfirmKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	if key.Matches(msg, m.keys.Confirm) {
		kind := m.confirm
		target := m.confirmTargetID
		m.confirm = confirmNone
		m.confirmTargetID = ""

		switch kind {
		case confirmRestart:
			if err := restartAgentDeployment(); err != nil {
				m.err = err
			} else {
				m.statusMsg = "Agent deployment restarting…"
				m.err = nil
				return m, scheduleToastClear()
			}
			return m, nil

		case confirmEnableLive:
			mode, err := setRemediationLive(true)
			if err != nil {
				m.err = err
			} else {
				m.remMode = mode
				m.statusMsg = "LIVE mode — remediations will execute"
				m.err = nil
				return m, tea.Batch(m.refreshAll(), scheduleToastClear())
			}
			return m, nil

		case confirmApproveRemediation:
			if err := approveRemediation(target); err != nil {
				m.err = err
			} else {
				m.statusMsg = "Remediation approved and executed"
				m.err = nil
				return m, tea.Batch(m.refreshAll(), scheduleToastClear())
			}
			return m, nil
		}
		return m, nil
	}
	if key.Matches(msg, m.keys.Cancel) {
		m.confirm = confirmNone
		m.confirmTargetID = ""
		m.statusMsg = "Cancelled"
		return m, scheduleToastClear()
	}
	return m, nil
}

// ── Main key handler ──

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
		m.statusMsg = "Refreshing…"
		return m, tea.Batch(m.refreshAll(), scheduleToastClear())
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
	case key.Matches(msg, m.keys.Tab8):
		m.tab = tabHealth
		m.clampCursor()
	case key.Matches(msg, m.keys.Down):
		if m.tab == tabLogs {
			var cmd tea.Cmd
			m.logsVP, cmd = m.logsVP.Update(msg)
			return m, tea.Batch(cmd, m.maybeDisableLogsFollowAfterScroll())
		}
		if m.cursor[m.tab] < m.listLen(m.tab)-1 {
			m.cursor[m.tab]++
		}
	case key.Matches(msg, m.keys.Up):
		if m.tab == tabLogs {
			var cmd tea.Cmd
			m.logsVP, cmd = m.logsVP.Update(msg)
			return m, tea.Batch(cmd, m.maybeDisableLogsFollowAfterScroll())
		}
		if m.cursor[m.tab] > 0 {
			m.cursor[m.tab]--
		}
	case key.Matches(msg, m.keys.Top):
		if m.tab == tabLogs {
			m.logsVP.GotoTop()
			return m, m.maybeDisableLogsFollowAfterScroll()
		}
		m.cursor[m.tab] = 0
	case key.Matches(msg, m.keys.Bottom):
		if m.tab == tabLogs {
			m.logsVP.GotoBottom()
			if !m.logsFollow {
				m.logsFollow = true
				m.statusMsg = "logs: follow ON"
				return m, scheduleToastClear()
			}
			return m, nil
		}
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
			groups := m.approvalGroups
			if len(groups) == 0 {
				groups = groupAuditRecords(m.pending, true)
			}
			if i >= 0 && i < len(groups) {
				r := groups[i].Records[0]
				// Show confirmation before approving
				m.confirm = confirmApproveRemediation
				m.confirmTargetID = r.ID
				return m, nil
			}
		}
	case key.Matches(msg, m.keys.Reject):
		if m.tab == tabApprovals && len(m.pending) > 0 {
			i := m.cursor[tabApprovals]
			groups := m.approvalGroups
			if len(groups) == 0 {
				groups = groupAuditRecords(m.pending, true)
			}
			if i >= 0 && i < len(groups) {
				id := groups[i].Records[0].ID
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
			return m, tea.Batch(cmd, m.maybeDisableLogsFollowAfterScroll())
		}
	}
	return m, nil
}

// ── Helpers ──

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
		if len(m.auditGroups) > 0 || len(m.audits) == 0 {
			return len(m.auditGroups)
		}
		return len(m.audits)
	case tabApprovals:
		if len(m.approvalGroups) > 0 || len(m.pending) == 0 {
			return len(m.approvalGroups)
		}
		return len(m.pending)
	case tabHealth:
		return len(m.healthScores)
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

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// ── Severity color helpers ──

func scoreStyle(score float64) lipgloss.Style {
	switch {
	case score >= 0.8:
		return listRowCriticalStyle
	case score >= 0.5:
		return listRowWarnStyle
	case score >= 0.3:
		return listRowInfoStyle
	default:
		return listRowDimStyle
	}
}

func riskTierStyle(tier string) lipgloss.Style {
	_ = tier // tier is string; cast at call site if needed
	switch strings.ToUpper(strings.TrimSpace(tier)) {
	case "T1":
		return listRowCriticalStyle
	case "T2":
		return listRowWarnStyle
	case "T3":
		return listRowInfoStyle
	default:
		return listRowDimStyle
	}
}

func statusStyle(s string) lipgloss.Style {
	switch strings.ToLower(s) {
	case "executed", "completed", "approved", "active", "healthy":
		return listRowSuccessStyle
	case "pending", "queued":
		return listRowInfoStyle
	case "rejected", "failed", "error", "critical":
		return listRowCriticalStyle
	case "auto_rejected", "skipped":
		return listRowDimStyle
	default:
		return listRowWarnStyle
	}
}

// ── View ──

func (m controlModel) View() string {
	if m.width == 0 {
		return logoStyle.Render(" KubeWise ") + "\n" + mutedStyle.Render("Connecting…")
	}

	var b strings.Builder
	b.WriteString(m.renderHeader())
	b.WriteString("\n")
	b.WriteString(m.renderTabs())
	b.WriteString("\n")

	if m.detail != "" || m.detailMeta != "" {
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
	return base
}

// ── Header ──

func (m controlModel) renderHeader() string {
	var health string
	if !m.ready {
		health = mutedStyle.Render("● connecting")
	} else if !m.healthOK {
		health = errStyle.Render("● unreachable")
	} else {
		health = successStyle.Render("● healthy")
	}

	sync := ""
	if m.loading {
		sync = m.spin.View() + " "
	}

	// Freshness indicator
	freshness := ""
	if !m.lastUpdate.IsZero() {
		age := time.Since(m.lastUpdate)
		switch {
		case age < 30*time.Second:
			freshness = successStyle.Render("live")
		case age < 2*time.Minute:
			freshness = mutedStyle.Render(fmt.Sprintf("%.0fs", age.Seconds()))
		default:
			freshness = warnStyle.Render(fmt.Sprintf("%.0fs", age.Seconds()))
		}
	}

	left := logoStyle.Render("KubeWise") + " " + brandStyle.Render("control center") + " " + m.renderModeBadge()
	right := fmt.Sprintf("%s%s  %s  %s %s",
		sync, health, freshness,
		mutedStyle.Render(m.lastUpdate.Format("15:04:05")),
		mutedStyle.Render(trunc(resolveAgentURL(), 28)))

	gap := m.width - lipgloss.Width(left) - lipgloss.Width(right) - 4
	if gap < 1 {
		gap = 1
	}
	return left + strings.Repeat(" ", gap) + right
}

// ── Tabs with badges ──

func (m controlModel) renderTabs() string {
	var parts []string
	counts := m.tabCounts()
	for i, label := range tabLabels {
		style := tabInactiveStyle
		if i == m.tab {
			style = tabActiveStyle
		}
		label = fmt.Sprintf("%d %s", i+1, label)
		if n := counts[i]; n > 0 && i != m.tab {
			label += fmt.Sprintf(" %s%d", tabBadgeStyle.Render("●"), n)
		}
		marker := ""
		if i == m.tab {
			marker = " ◂"
		}
		parts = append(parts, style.Render(label+marker))
	}
	hint := mutedStyle.Render("  ←/→")
	return strings.Join(parts, "") + hint
}

func (m controlModel) tabCounts() [tabCount]int {
	return [tabCount]int{
		tabPredict:   len(m.preds),
		tabAnomalies: len(m.anomalies),
		tabAudit:     m.listLen(tabAudit),
		tabApprovals: m.listLen(tabApprovals),
		tabHealth:    len(m.healthScores),
	}
}

func normalizedAuditStatus(r models.AuditRecord) string {
	status := string(r.Status)
	if r.Status == models.AuditRejected {
		low := strings.ToLower(strings.TrimSpace(r.Reason))
		if !strings.Contains(low, "operator") && !strings.Contains(low, "kwctl") {
			status = "auto_rejected"
		}
	}
	return status
}

func groupAuditRecords(records []models.AuditRecord, approvals bool) []auditGroup {
	if len(records) == 0 {
		return nil
	}
	type keyParts struct {
		status string
		tier   string
		action string
		ns     string
		target string
	}
	makeKey := func(r models.AuditRecord) keyParts {
		status := normalizedAuditStatus(r)
		if approvals {
			// Approvals are always pending; keep status out to group by action/tier.
			status = "pending"
		}
		return keyParts{
			status: status,
			tier:   string(r.RiskTier),
			action: r.Plan.Action.Type,
			ns:     r.Plan.Action.Namespace,
			target: r.Plan.Action.Target,
		}
	}

	gm := make(map[keyParts][]models.AuditRecord, len(records))
	for _, r := range records {
		k := makeKey(r)
		gm[k] = append(gm[k], r)
	}

	groups := make([]auditGroup, 0, len(gm))
	for k, rs := range gm {
		sort.SliceStable(rs, func(i, j int) bool {
			return rs[i].CreatedAt.After(rs[j].CreatedAt)
		})
		keyStr := fmt.Sprintf("%s | %s | %s %s/%s", k.status, k.tier, k.action, k.ns, k.target)
		groups = append(groups, auditGroup{Key: keyStr, Records: rs})
	}
	sort.SliceStable(groups, func(i, j int) bool {
		return groups[i].Records[0].CreatedAt.After(groups[j].Records[0].CreatedAt)
	})
	return groups
}

// ── Tab content ──

func (m controlModel) renderTabContent() string {
	// Show connection error if status fetch failed
	if m.err != nil && !m.ready {
		return errStyle.Render("⚠  Connection error: "+m.err.Error()) + "\n\n" +
			mutedStyle.Render("Press r to retry, or ctrl+p to check agent URL in profile settings")
	}

	switch m.tab {
	case tabDashboard:
		return m.renderDashboard()
	case tabPredict:
		if m.errTab[tabPredict] != nil {
			return m.renderTabError("predictions", m.errTab[tabPredict])
		}
		return m.renderList(m.predsLines(), m.cursor[tabPredict], len(m.preds))
	case tabAnomalies:
		if m.errTab[tabAnomalies] != nil {
			return m.renderTabError("anomalies", m.errTab[tabAnomalies])
		}
		return m.renderList(m.anomalyLines(), m.cursor[tabAnomalies], len(m.anomalies))
	case tabAudit:
		if m.errTab[tabAudit] != nil {
			return m.renderTabError("audit", m.errTab[tabAudit])
		}
		return m.renderList(m.auditLines(), m.cursor[tabAudit], m.listLen(tabAudit))
	case tabApprovals:
		if m.errTab[tabApprovals] != nil {
			return m.renderTabError("approvals", m.errTab[tabApprovals])
		}
		return m.renderList(m.approvalLines(), m.cursor[tabApprovals], m.listLen(tabApprovals))
	case tabConfig:
		if m.errTab[tabConfig] != nil {
			return m.renderTabError("config", m.errTab[tabConfig])
		}
		return m.renderConfig()
	case tabLogs:
		if m.errTab[tabLogs] != nil {
			return m.renderTabError("logs", m.errTab[tabLogs])
		}
		if m.logs == "" {
			return emptyStateStyle.Render("No logs available.") + "\n\n" +
				mutedStyle.Render("Ensure kubeconfig can reach the cluster and the agent pod is running.") + "\n" +
				mutedStyle.Render("Check: kubectl get pods -n ") + keyStyle.Render(agentNS)
		}
		lines := strings.Count(m.logs, "\n") + 1
		follow := "follow=ON"
		followSty := successStyle
		if !m.logsFollow {
			follow = "follow=OFF"
			followSty = mutedStyle
		}
		pos := fmt.Sprintf("%d/%d", min(m.logsVP.YOffset+m.logsVP.Height, m.logsVP.TotalLineCount()), max(1, m.logsVP.TotalLineCount()))
		header := mutedStyle.Render(fmt.Sprintf("lines %d", lines)) +
			mutedStyle.Render(" · ") +
			followSty.Render(follow) +
			mutedStyle.Render(" (f) · ") +
			mutedStyle.Render("pos "+pos)
		return header + "\n" + m.logsVP.View()
	case tabHealth:
		if m.errTab[tabHealth] != nil {
			return m.renderTabError("health scores", m.errTab[tabHealth])
		}
		return m.renderList(m.healthScoreLines(), m.cursor[tabHealth], len(m.healthScores))
	default:
		return ""
	}
}

func (m controlModel) renderTabError(tabName string, err error) string {
	return warnStyle.Render(fmt.Sprintf("⚠ %s unavailable", tabName)) + "\n" +
		mutedStyle.Render(err.Error()) + "\n\n" +
		mutedStyle.Render("Press r to retry")
}

// ── Dashboard ──

func (m controlModel) renderDashboard() string {
	var b strings.Builder

	// Health status
	if !m.ready {
		b.WriteString(emptyStateStyle.Render("Loading agent data…"))
		b.WriteString("\n\n")
	}

	if m.err != nil {
		b.WriteString(errStyle.Render("⚠  Connection error"))
		b.WriteString("\n")
		if m.status.Uptime != "" {
			b.WriteString(mutedStyle.Render(fmt.Sprintf("  Last known: up %s · %d scrapes", m.uptime, m.status.Scrapes)))
			b.WriteString("\n\n")
		} else {
			b.WriteString(mutedStyle.Render("  Agent unreachable. Check connection and retry."))
			b.WriteString("\n\n")
		}
	}

	// ── KPI grid (4 columns, no box borders) ──

	type kpiDef struct {
		label string
		value string
		style lipgloss.Style
	}

	var kpis []kpiDef

	// Row 1: operational
	kpis = append(kpis,
		kpiDef{"Uptime", m.status.Uptime, infoStyle},
		kpiDef{"Scrapes", fmt.Sprintf("%d", m.status.Scrapes), infoStyle},
		kpiDef{"Passed", fmt.Sprintf("%d", m.status.GatePassed), successStyle},
		kpiDef{"Dropped", fmt.Sprintf("%d", m.status.GateDropped), warnStyle},
	)

	// Row 2: business metrics
	kpis = append(kpis,
		kpiDef{"Predictions", fmt.Sprintf("%d", len(m.preds)), kpiValueStyleForCount(len(m.preds), 5)},
		kpiDef{"Anomalies", fmt.Sprintf("%d", len(m.anomalies)), kpiValueStyleForCount(len(m.anomalies), 3)},
		kpiDef{"Pending", fmt.Sprintf("%d", len(m.pending)), kpiValueStyleForCount(len(m.pending), 1)},
	)

	// Health value
	healthVal := "—"
	healthSty := mutedStyle
	if m.healthSum != nil {
		healthVal = fmt.Sprintf("%.0f", m.healthSum.OverallScore)
		healthSty = scoreStyle(m.healthSum.OverallScore / 100.0)
	}
	kpis = append(kpis, kpiDef{"Health", healthVal, healthSty})

	// Accuracy
	accVal := "—"
	accSty := mutedStyle
	if m.accSnap != nil {
		accVal = fmt.Sprintf("%.1f%%", m.accSnap.Overall.F1Score*100)
		accSty = scoreStyle(m.accSnap.Overall.F1Score)
	}
	kpis = append(kpis, kpiDef{"Acc F1", accVal, accSty})

	// Render grid — row-major: all labels then all values per row
	cols := 4
	cellW := (m.width - 8 - (cols-1)*2) / cols
	if cellW < 12 {
		cellW = 12
	}
	cellGap := strings.Repeat(" ", 2)

	for rowStart := 0; rowStart < len(kpis); rowStart += cols {
		rowEnd := rowStart + cols
		if rowEnd > len(kpis) {
			rowEnd = len(kpis)
		}
		// Labels line
		for i := rowStart; i < rowEnd; i++ {
			b.WriteString(mutedStyle.Width(cellW).Render(kpis[i].label))
			if i < rowEnd-1 {
				b.WriteString(cellGap)
			}
		}
		b.WriteString("\n")
		// Values line
		for i := rowStart; i < rowEnd; i++ {
			b.WriteString(kpis[i].style.Width(cellW).Bold(true).Render(kpis[i].value))
			if i < rowEnd-1 {
				b.WriteString(cellGap)
			}
		}
		b.WriteString("\n\n")
	}

	// ── Recent Activity ──

	b.WriteString(headingStyle.Render("Recent Activity"))
	b.WriteString("\n")
	if len(m.preds) == 0 && len(m.anomalies) == 0 {
		b.WriteString(successStyle.Render("  ✓ All clear"))
		b.WriteString("\n")
		b.WriteString(mutedStyle.Render("  No active predictions or anomalies"))
	} else {
		for i, p := range m.preds {
			if i >= 4 {
				break
			}
			sty := scoreStyle(p.Score)
			fmt.Fprintf(&b, "  ▸ %s %s  %s  ETA %.0fs\n",
				sty.Render(trunc(p.Type, 14)),
				mutedStyle.Render(trunc(p.Entity, 26)),
				sty.Render(fmt.Sprintf("%.0f%%", p.Score*100)),
				p.ETASeconds)
		}
		for i, a := range m.anomalies {
			if i >= 4 {
				break
			}
			pat := a.Pattern
			if pat == "" {
				pat = "statistical"
			}
			sty := scoreStyle(a.Score)
			fmt.Fprintf(&b, "  ▸ %s %s  %s %s\n",
				sty.Render(trunc(pat, 14)),
				mutedStyle.Render(trunc(a.Entity, 26)),
				sty.Render(fmt.Sprintf("%.2f", a.Score)),
				statusStyle(a.Status).Render(a.Status))
		}
	}

	// ── Cluster Health ──

	if len(m.healthScores) > 0 {
		b.WriteString("\n")
		b.WriteString(headingStyle.Render("Cluster Health"))
		b.WriteString("\n")
		for i, hs := range m.healthScores {
			if i >= 5 {
				break
			}
			sty := scoreStyle(hs.Score / 100.0)
			trend := "stable"
			if hs.Trend != "" {
				trend = hs.Trend
			}
			fmt.Fprintf(&b, "  ▸ %s  %s  %s  %s\n",
				sty.Render(fmt.Sprintf("%.0f", hs.Score)),
				trendStyle(trend).Render(trunc(trend, 10)),
				mutedStyle.Render(trunc(hs.Namespace, 16)),
				mutedStyle.Render(trunc(hs.Entity, 28)))
		}
	}

	// Agent info line
	if m.config != nil {
		b.WriteString("\n" + hbar + "\n")
		b.WriteString(mutedStyle.Render(fmt.Sprintf("Agent: %s/%s · LLM: %s/%s · Mode: %s",
			m.config.ScrapeInterval,
			trunc(m.config.PrometheusAddress, 20),
			m.config.LLMProvider,
			trunc(m.config.LLMModel, 20),
			m.config.Remediation.Mode)))
	}
	return b.String()
}

//nolint:unused
func scoreStyleForCount(n, threshold int) lipgloss.Style {
	if n >= threshold*2 {
		return cardCriticalStyle
	}
	if n >= threshold {
		return cardWarnStyle
	}
	return cardSuccessStyle
}

func kpiValueStyleForCount(n, threshold int) lipgloss.Style {
	if n >= threshold*2 {
		return kpiCriticalStyle
	}
	if n >= threshold {
		return kpiWarnStyle
	}
	return kpiSuccessStyle
}

func trendStyle(trend string) lipgloss.Style {
	switch strings.ToLower(trend) {
	case "improving", "improved":
		return successStyle
	case "degrading", "degraded", "declining", "critical":
		return errStyle
	default:
		return mutedStyle
	}
}

// col renders a fixed-width table column with ANSI/Unicode-safe truncation.
func col(text string, width int, style lipgloss.Style) string {
	if width <= 0 {
		return style.Render(text)
	}
	// Truncate by display width, not bytes/runes, before styling.
	t := truncate.StringWithTail(text, uint(width), "…")
	return style.Width(width).MaxWidth(width).Render(t)
}

func joinCols(cols ...string) string {
	return strings.Join(cols, " ")
}

// ── List views ──

func (m controlModel) predsLines() []string {
	if len(m.preds) == 0 {
		return []string{emptyStateStyle.Render("No active predictions") + "\n" +
			mutedStyle.Render("  Predictions appear when the agent detects upcoming issues")}
	}
	var lines []string
	lines = append(lines, joinCols(
		col("TYPE", 14, listHeaderStyle),
		col("ENTITY", 24, listHeaderStyle),
		col("SCORE", 8, listHeaderStyle),
		col("ETA", 8, listHeaderStyle),
		col("ACTION", 20, listHeaderStyle),
	))
	for _, p := range m.preds {
		style := scoreStyle(p.Score)
		lines = append(lines, joinCols(
			col(p.Type, 14, style),
			col(p.Entity, 24, mutedStyle),
			col(fmt.Sprintf("%.0f", p.Score*100), 8, style),
			col(fmt.Sprintf("%.0f", p.ETASeconds), 8, mutedStyle),
			col(p.Action, 20, mutedStyle),
		))
	}
	return lines
}

func (m controlModel) anomalyLines() []string {
	if len(m.anomalies) == 0 {
		return []string{emptyStateStyle.Render("No anomalies") + "\n" +
			mutedStyle.Render("  Anomalies appear when metrics deviate from expected patterns")}
	}
	var lines []string
	lines = append(lines, joinCols(
		col("PATTERN", 10, listHeaderStyle),
		col("ENTITY", 24, listHeaderStyle),
		col("SCORE", 8, listHeaderStyle),
		col("STATUS", 12, listHeaderStyle),
	))
	for _, a := range m.anomalies {
		pat := a.Pattern
		if pat == "" {
			pat = "statistical"
		}
		scoreSty := scoreStyle(a.Score)
		statSty := statusStyle(a.Status)
		lines = append(lines, joinCols(
			col(pat, 10, scoreSty),
			col(a.Entity, 24, mutedStyle),
			col(fmt.Sprintf("%.2f", a.Score), 8, scoreSty),
			col(a.Status, 12, statSty),
		))
	}
	return lines
}

func (m controlModel) auditLines() []string {
	if len(m.audits) == 0 {
		return []string{emptyStateStyle.Render("No remediation records") + "\n" +
			mutedStyle.Render("  Audit records appear after remediations are attempted")}
	}
	var lines []string
	groups := m.auditGroups
	if len(groups) == 0 {
		groups = groupAuditRecords(m.audits, false)
	}
	lines = append(lines, joinCols(
		col("STATUS", 14, listHeaderStyle),
		col("ACTION", 28, listHeaderStyle),
		col("COUNT", 7, listHeaderStyle),
		col("TIER", 8, listHeaderStyle),
		col("REASON", 33, listHeaderStyle),
	))
	for _, g := range groups {
		r := g.Records[0]
		action := fmt.Sprintf("%s/%s", r.Plan.Action.Type, r.Plan.Action.Target)
		status := normalizedAuditStatus(r)
		statSty := statusStyle(status)
		tierSty := riskTierStyle(string(r.RiskTier))
		count := ""
		if len(g.Records) > 1 {
			count = fmt.Sprintf("%dx", len(g.Records))
		}
		lines = append(lines, joinCols(
			col(status, 14, statSty),
			col(action, 28, mutedStyle),
			col(count, 7, mutedStyle),
			col(string(r.RiskTier), 8, tierSty),
			col(r.Reason, 33, mutedStyle),
		))
	}
	return lines
}

func (m controlModel) approvalLines() []string {
	if len(m.pending) == 0 {
		return []string{emptyStateStyle.Render("No pending approvals") + "\n" +
			mutedStyle.Render("  T3 actions appear here when live mode is enabled and need your approval")}
	}
	var lines []string
	groups := m.approvalGroups
	if len(groups) == 0 {
		groups = groupAuditRecords(m.pending, true)
	}
	lines = append(lines, joinCols(
		col("TIER", 10, listHeaderStyle),
		col("ACTION", 28, listHeaderStyle),
		col("COUNT", 7, listHeaderStyle),
		col("CONF", 8, listHeaderStyle),
		col("REASON", 33, listHeaderStyle),
	))
	for _, g := range groups {
		r := g.Records[0]
		action := fmt.Sprintf("%s %s/%s", r.Plan.Action.Type, r.Plan.Action.Namespace, r.Plan.Action.Target)
		tierSty := riskTierStyle(string(r.RiskTier))
		maxConf := 0.0
		for _, rr := range g.Records {
			if rr.Plan.Diagnosis.Confidence > maxConf {
				maxConf = rr.Plan.Diagnosis.Confidence
			}
		}
		confSty := scoreStyle(maxConf)
		count := ""
		if len(g.Records) > 1 {
			count = fmt.Sprintf("%dx", len(g.Records))
		}
		lines = append(lines, joinCols(
			col(string(r.RiskTier), 10, tierSty),
			col(action, 28, mutedStyle),
			col(count, 7, mutedStyle),
			col(fmt.Sprintf("%.0f%%", maxConf*100), 8, confSty),
			col(r.Reason, 33, mutedStyle),
		))
	}
	return lines
}

func (m controlModel) healthScoreLines() []string {
	if len(m.healthScores) == 0 {
		return []string{emptyStateStyle.Render("No health scores") + "\n" +
			mutedStyle.Render("  Health scores appear after the agent computes them during scrape cycles")}
	}
	var lines []string
	lines = append(lines, joinCols(
		col("ENTITY", 24, listHeaderStyle),
		col("NAMESPACE", 16, listHeaderStyle),
		col("SCORE", 8, listHeaderStyle),
		col("FACTORS", 40, listHeaderStyle),
	))
	for _, hs := range m.healthScores {
		sty := scoreStyle(hs.Score / 100.0)
		var factors []string
		for _, f := range hs.Factors {
			label := f.Name
			if len(label) > 8 {
				label = label[:8]
			}
			val := int(f.Score * 100)
			factors = append(factors, fmt.Sprintf("%s=%d", label, val))
		}
		factorStr := ""
		if len(factors) > 0 {
			factorStr = strings.Join(factors, " ")
		}
		lines = append(lines, joinCols(
			col(hs.Entity, 24, mutedStyle),
			col(hs.Namespace, 16, mutedStyle),
			col(fmt.Sprintf("%.0f", hs.Score), 8, sty),
			col(factorStr, 40, mutedStyle),
		))
	}
	return lines
}

// ── Render list ──

func (m controlModel) renderList(lines []string, cursor, total int) string {
	if len(lines) == 1 && strings.Contains(lines[0], "No ") {
		// Empty state
		return lines[0]
	}

	// Calculate visible range — header takes 1 line, each data item takes 1 line
	headerLines := 1
	maxVisible := m.contentHeight() - 1 // leave room for status line
	if maxVisible < 3 {
		maxVisible = 3
	}
	dataLines := lines[headerLines:]
	dataCount := len(dataLines)
	if dataCount == 0 {
		// No data rows (header-only). Render header and footer safely.
		var b strings.Builder
		b.WriteString(lines[0])
		b.WriteString("\n")
		if total > 0 {
			b.WriteString(mutedStyle.Render(fmt.Sprintf("\n%d/%d  enter detail  g/G top/bottom", 0, total)))
		}
		return b.String()
	}

	// Center cursor in visible window
	half := maxVisible / 2
	start := cursor - half
	if start < 0 {
		start = 0
	}
	end := start + maxVisible
	if end > dataCount {
		end = dataCount
		start = end - maxVisible
		if start < 0 {
			start = 0
		}
	}

	var b strings.Builder
	// Header
	b.WriteString(lines[0])
	b.WriteString("\n")

	// Visible data items
	for i := start; i < end; i++ {
		line := lines[headerLines+i]
		// i is a data index into dataLines; cursor is also data index (not absolute line index).
		if i == cursor {
			b.WriteString(listSelectedStyle.Render("› " + line))
		} else {
			b.WriteString("  " + line)
		}
		b.WriteString("\n")
	}

	if total > 0 {
		scrollInfo := ""
		if dataCount > maxVisible {
			pct := 0
			if dataCount > 1 {
				pct = int(float64(cursor) / float64(dataCount-1) * 100)
			}
			scrollInfo = fmt.Sprintf("  scroll %d%%", pct)
		}
		b.WriteString(mutedStyle.Render(fmt.Sprintf("\n%d/%d%s  enter detail  g/G top/bottom", cursor+1, total, scrollInfo)))
	}
	return b.String()
}

// ── Config ──

func (m controlModel) renderConfig() string {
	if m.config == nil {
		return emptyStateStyle.Render("No agent config saved on agent.") + "\n\n" +
			mutedStyle.Render("The agent will create a default config on first run.") + "\n" +
			mutedStyle.Render("Use ctrl+p to set scrape interval, Prometheus URL, LLM, etc.")
	}
	var b strings.Builder
	b.WriteString(formatConfigSummary(m.config))
	b.WriteString("\n" + hbar + "\n")
	b.WriteString(mutedStyle.Render("  "))
	b.WriteString(keyStyle.Render("d/L"))
	b.WriteString(mutedStyle.Render(" observe/live  "))
	b.WriteString(keyStyle.Render("m"))
	b.WriteString(mutedStyle.Render(" mode  "))
	b.WriteString(keyStyle.Render("R"))
	b.WriteString(mutedStyle.Render(" restart  "))
	b.WriteString(keyStyle.Render("ctrl+p"))
	b.WriteString(mutedStyle.Render(" commands"))
	return b.String()
}

// ── Detail ──

func (m controlModel) renderDetail() string {
	title := brandStyle.Render(m.detailTitle)
	content := m.detailVP.View()
	total := m.detailVP.TotalLineCount()
	shown := m.detailVP.VisibleLineCount()
	top := m.detailVP.YOffset

	var scrollInfo string
	if total > shown {
		den := total - shown
		pct := 0
		if den > 0 {
			pct = int(float64(top) / float64(den) * 100)
		}
		scrollInfo = mutedStyle.Render(fmt.Sprintf("  ↑↓ scroll %d%%", pct))
	}
	help := mutedStyle.Render("esc back · q quit") + scrollInfo

	var b strings.Builder
	b.WriteString(title)
	b.WriteByte('\n')
	if meta := strings.TrimRight(m.detailMeta, "\n"); meta != "" {
		b.WriteString(meta)
		b.WriteByte('\n')
	}
	b.WriteString(content)
	b.WriteByte('\n')
	b.WriteString(help)
	return b.String()
}

func (m *controlModel) resizeDetailViewport() {
	metaH := detailMetaLines(m.detailMeta)
	innerH := m.detailVPHeight - metaH
	if innerH < 1 {
		innerH = 1
	}
	m.detailVP.Height = innerH
}

func (m *controlModel) relayoutDetail() {
	m.showDetailForSelection()
}

func (m *controlModel) refreshDetailViewport() {
	w := m.detailContentWidth()
	cacheKey := fmt.Sprintf("%d\n%s\n%s", w, m.detailMeta, m.detail)
	if m.detailWrapW == w && m.detailContent == cacheKey {
		return
	}
	m.detailWrapW = w
	m.detailContent = cacheKey

	total := m.detailVP.TotalLineCount()
	shown := m.detailVP.VisibleLineCount()
	den := total - shown
	pct := 0.0
	if den > 0 {
		pct = float64(m.detailVP.YOffset) / float64(den)
	}

	m.detailVP.SetContent(m.detail)

	newTotal := m.detailVP.TotalLineCount()
	newShown := m.detailVP.VisibleLineCount()
	newDen := newTotal - newShown
	if newDen > 0 {
		m.detailVP.YOffset = int(pct * float64(newDen))
		if m.detailVP.YOffset < 0 {
			m.detailVP.YOffset = 0
		}
		if m.detailVP.YOffset > newDen {
			m.detailVP.YOffset = newDen
		}
	} else {
		m.detailVP.YOffset = 0
	}
}

// ── Footer ──

func (m controlModel) renderFooter() string {
	var b strings.Builder

	// Status message
	if m.statusMsg != "" {
		b.WriteString(successStyle.Render(m.statusMsg))
		b.WriteString("\n")
	}

	// Keybinding help
	if m.help.ShowAll {
		b.WriteString(m.renderStructuredHelp())
	} else {
		b.WriteString(m.help.ShortHelpView(m.keys.ShortHelp()))
		b.WriteString(" ")
	}

	return statusBarStyle.Width(m.width).Render(strings.TrimRight(b.String(), "\n"))
}

// ── Mode badge ──

func (m controlModel) renderModeBadge() string {
	if m.remMode.Live {
		return modeLiveStyle.Render("LIVE")
	}
	if m.remMode.Mode == models.RemediationModeDryRun || m.remMode.Mode == "" {
		return modeDryRunStyle.Render("OBSERVE")
	}
	return modeObserveStyle.Render(m.remMode.Mode)
}

// ── Confirm dialog ──

func (m controlModel) renderConfirm() string {
	var body string
	switch m.confirm {
	case confirmEnableLive:
		body = confirmTitleStyle.Render("Enable LIVE remediation?") + "\n\n" +
			mutedStyle.Render("T1/T2 actions will execute automatically.") + "\n" +
			mutedStyle.Render("T3 actions still require your approval.") + "\n\n" +
			keyStyle.Render("  y  ") + mutedStyle.Render("enable  ") +
			keyStyle.Render("  n  ") + mutedStyle.Render("cancel")
	case confirmApproveRemediation:
		body = confirmTitleStyle.Render("Approve remediation?") + "\n\n" +
			mutedStyle.Render(fmt.Sprintf("ID: %s", m.confirmTargetID)) + "\n" +
			warnStyle.Render("This action will be executed against the cluster.") + "\n\n" +
			keyStyle.Render("  y  ") + mutedStyle.Render("approve  ") +
			keyStyle.Render("  n  ") + mutedStyle.Render("cancel")
	default:
		body = confirmTitleStyle.Render("Restart agent deployment?") + "\n\n" +
			mutedStyle.Render(fmt.Sprintf("Rolling restart %s/%s.", agentNS, agentSvc)) + "\n\n" +
			keyStyle.Render("  y  ") + mutedStyle.Render("confirm  ") +
			keyStyle.Render("  n  ") + mutedStyle.Render("cancel")
	}
	return lipgloss.Place(m.width, 6, lipgloss.Center, lipgloss.Bottom,
		confirmBoxStyle.Width(min(60, m.width-4)).Render(body))
}

// ── Toggle mode ──

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

// ── Palette overlay ──

//nolint:unused
func (m controlModel) renderPaletteOverlay(base string) string {
	_ = base
	return m.renderPalette()
}

// ── Structured help ──

func (m controlModel) renderStructuredHelp() string {
	var b strings.Builder

	b.WriteString("\n")
	b.WriteString(helpCategoryStyle.Render("Navigation"))
	b.WriteString("\n")
	for _, kb := range []key.Binding{m.keys.TabPrev, m.keys.TabNext, m.keys.Palette, m.keys.Help, m.keys.Quit} {
		fmt.Fprintf(&b, "  %s %s\n",
			helpKeyStyle.Render(kb.Help().Key),
			helpDescStyle.Render(kb.Help().Desc))
	}

	b.WriteString(helpCategoryStyle.Render("Tab shortcuts"))
	b.WriteString("\n")
	for _, kb := range []key.Binding{m.keys.Tab1, m.keys.Tab2, m.keys.Tab3, m.keys.Tab4, m.keys.Tab5, m.keys.Tab6, m.keys.Tab7, m.keys.Tab8} {
		fmt.Fprintf(&b, "  %s %s\n",
			helpKeyStyle.Render(kb.Help().Key),
			helpDescStyle.Render(kb.Help().Desc))
	}

	b.WriteString(helpCategoryStyle.Render("List navigation"))
	b.WriteString("\n")
	for _, kb := range []key.Binding{m.keys.Up, m.keys.Down, m.keys.Top, m.keys.Bottom, m.keys.Detail, m.keys.Back} {
		fmt.Fprintf(&b, "  %s %s\n",
			helpKeyStyle.Render(kb.Help().Key),
			helpDescStyle.Render(kb.Help().Desc))
	}

	b.WriteString(helpCategoryStyle.Render("Actions"))
	b.WriteString("\n")
	for _, kb := range []key.Binding{m.keys.Refresh, m.keys.DryRun, m.keys.Mode, m.keys.ToggleLive, m.keys.Restart} {
		fmt.Fprintf(&b, "  %s %s\n",
			helpKeyStyle.Render(kb.Help().Key),
			helpDescStyle.Render(kb.Help().Desc))
	}

	b.WriteString(helpCategoryStyle.Render("Approvals"))
	b.WriteString("\n")
	for _, kb := range []key.Binding{m.keys.Approve, m.keys.Reject, m.keys.Confirm, m.keys.Cancel} {
		fmt.Fprintf(&b, "  %s %s\n",
			helpKeyStyle.Render(kb.Help().Key),
			helpDescStyle.Render(kb.Help().Desc))
	}

	b.WriteString(helpCategoryStyle.Render("Logs"))
	b.WriteString("\n")
	for _, kb := range []key.Binding{m.keys.LogFollow} {
		fmt.Fprintf(&b, "  %s %s\n",
			helpKeyStyle.Render(kb.Help().Key),
			helpDescStyle.Render(kb.Help().Desc))
	}

	b.WriteString("\n" + helpCloseStyle.Render("Press ? to close help"))
	return b.String()
}

// ── showDetailForSelection ──

func (m *controlModel) showDetailForSelection() {
	w := m.detailContentWidth()
	m.detailMeta = ""

	switch m.tab {
	case tabPredict:
		i := m.cursor[tabPredict]
		if i < 0 || i >= len(m.preds) {
			return
		}
		p := m.preds[i]
		m.detailTitle = fmt.Sprintf("Prediction  %s/%s", trunc(p.Namespace, 12), trunc(p.Entity, 24))
		b := new(strings.Builder)
		style := scoreStyle(p.Score)
		writeDetailKV(b, w, "Type:", p.Type)
		writeDetailKV(b, w, "Entity:", p.Entity)
		writeDetailKV(b, w, "Namespace:", p.Namespace)
		writeDetailKVStyled(b, w, "Score:", style, fmt.Sprintf("%.2f", p.Score))
		writeDetailKVStyled(b, w, "Confidence:", style, fmt.Sprintf("%.0f%%", p.Confidence*100))
		writeDetailKV(b, w, "ETA:", fmt.Sprintf("%s (%.0fs)", p.ETA(), p.ETASeconds))
		writeDetailKV(b, w, "Action:", p.Action)
		writeDetailKV(b, w, "Metric:", p.MetricName)
		writeDetailKV(b, w, "Timestamp:", p.Timestamp.Format(time.RFC3339))
		m.detail = b.String()
		m.resizeDetailViewport()
		m.refreshDetailViewport()

	case tabAnomalies:
		i := m.cursor[tabAnomalies]
		if i < 0 || i >= len(m.anomalies) {
			return
		}
		a := m.anomalies[i]
		m.detailTitle = fmt.Sprintf("Anomaly  %s", trunc(a.ID, 12))
		b := new(strings.Builder)
		writeDetailKV(b, w, "ID:", a.ID)
		writeDetailKV(b, w, "Entity:", a.Entity)
		writeDetailKV(b, w, "Namespace:", a.Namespace)
		writeDetailKV(b, w, "Pattern:", a.Pattern)
		writeDetailKV(b, w, "Metric:", a.MetricName)
		writeDetailKVStyled(b, w, "Score:", scoreStyle(a.Score), fmt.Sprintf("%.2f", a.Score))
		writeDetailKVStyled(b, w, "Status:", statusStyle(a.Status), a.Status)
		if a.DetectedAt != nil {
			writeDetailKV(b, w, "Detected:", a.DetectedAt.Format(time.RFC3339))
		}
		if a.RemediatedAt != nil {
			writeDetailKV(b, w, "Remediated:", a.RemediatedAt.Format(time.RFC3339))
		}
		m.detail = b.String()
		m.resizeDetailViewport()
		m.refreshDetailViewport()

	case tabAudit:
		i := m.cursor[tabAudit]
		groups := m.auditGroups
		if len(groups) == 0 {
			groups = groupAuditRecords(m.audits, false)
		}
		if i < 0 || i >= len(groups) {
			return
		}
		g := groups[i]
		r := g.Records[0]
		title := fmt.Sprintf("Remediation  %s", trunc(r.ID, 12))
		if len(g.Records) > 1 {
			title = fmt.Sprintf("Remediation  %s  (%dx)", trunc(r.ID, 12), len(g.Records))
		}
		m.detailTitle = title
		meta := new(strings.Builder)
		body := new(strings.Builder)
		if len(g.Records) > 1 {
			writeDetailKV(meta, w, "Grouped:", fmt.Sprintf("%d records", len(g.Records)))
			writeDetailKV(meta, w, "Key:", g.Key)
			writeDetailSection(meta, "Latest record")
		}
		buildAuditDetailMeta(meta, w, r)
		buildAuditDetailBody(body, w, r, false)
		if len(g.Records) > 1 {
			writeDetailSection(body, "Record IDs")
			for _, rr := range g.Records {
				body.WriteString(formatDetailKVRendered(w, mutedStyle.Render(rr.CreatedAt.Format(time.RFC3339)), detailValueStyle.Render(rr.ID)))
				body.WriteByte('\n')
			}
		}
		m.detailMeta = meta.String()
		m.detail = body.String()
		m.resizeDetailViewport()
		m.refreshDetailViewport()

	case tabApprovals:
		i := m.cursor[tabApprovals]
		groups := m.approvalGroups
		if len(groups) == 0 {
			groups = groupAuditRecords(m.pending, true)
		}
		if i < 0 || i >= len(groups) {
			return
		}
		g := groups[i]
		r := g.Records[0]
		title := fmt.Sprintf("Approval  %s", trunc(r.ID, 12))
		if len(g.Records) > 1 {
			title = fmt.Sprintf("Approval  %s  (%dx)", trunc(r.ID, 12), len(g.Records))
		}
		m.detailTitle = title
		meta := new(strings.Builder)
		body := new(strings.Builder)
		meta.WriteString(warnStyle.Render("PENDING HUMAN APPROVAL"))
		meta.WriteByte('\n')
		if len(g.Records) > 1 {
			writeDetailKV(meta, w, "Grouped:", fmt.Sprintf("%d records", len(g.Records)))
			writeDetailKV(meta, w, "Key:", g.Key)
		}
		buildAuditDetailMeta(meta, w, r)
		buildAuditDetailBody(body, w, r, true)
		if len(g.Records) > 1 {
			writeDetailSection(body, "Record IDs")
			for _, rr := range g.Records {
				body.WriteString(formatDetailKVRendered(w, mutedStyle.Render(rr.CreatedAt.Format(time.RFC3339)), detailValueStyle.Render(rr.ID)))
				body.WriteByte('\n')
			}
		}
		body.WriteByte('\n')
		body.WriteString(keyStyle.Render("a") + mutedStyle.Render(" approve  ") +
			keyStyle.Render("x") + mutedStyle.Render(" reject  ") +
			keyStyle.Render("esc") + mutedStyle.Render(" back"))
		m.detailMeta = meta.String()
		m.detail = body.String()
		m.resizeDetailViewport()
		m.refreshDetailViewport()

	case tabHealth:
		i := m.cursor[tabHealth]
		if i < 0 || i >= len(m.healthScores) {
			return
		}
		hs := m.healthScores[i]
		m.detailTitle = fmt.Sprintf("Health Score  %s/%s", trunc(hs.Namespace, 12), trunc(hs.Entity, 24))
		b := new(strings.Builder)
		writeDetailKV(b, w, "Entity:", hs.Entity)
		writeDetailKV(b, w, "Namespace:", hs.Namespace)
		sty := scoreStyle(hs.Score / 100.0)
		writeDetailKVStyled(b, w, "Score:", sty, fmt.Sprintf("%.1f/100", hs.Score))
		writeDetailKV(b, w, "Generated:", hs.GeneratedAt.Format(time.RFC3339))
		if len(hs.Factors) > 0 {
			writeDetailSection(b, "Factors")
			for _, f := range hs.Factors {
				fSty := scoreStyle(f.Score)
				contrib := f.Score * f.Weight * 100
				line := fmt.Sprintf("%s %s weight=%.2f value=%s contribution=%.1f",
					fSty.Render(trunc(f.Name, 20)),
					mutedStyle.Render(trunc(f.Detail, 40)),
					f.Weight,
					fSty.Render(fmt.Sprintf("%.2f", f.Score)),
					contrib)
				b.WriteString(line)
				b.WriteByte('\n')
			}
		}
		m.detail = b.String()
		m.resizeDetailViewport()
		m.refreshDetailViewport()
	}
}

// ── buildAuditDetail ──

func buildAuditDetailMeta(b *strings.Builder, width int, r models.AuditRecord) {
	writeDetailKV(b, width, "ID:", r.ID)
	writeDetailKVStyled(b, width, "Status:", statusStyle(string(r.Status)), string(r.Status))
	writeDetailKVStyled(b, width, "Tier:", riskTierStyle(string(r.RiskTier)), string(r.RiskTier))
	writeDetailKV(b, width, "Created:", r.CreatedAt.Format(time.RFC3339))
	if r.ExecutedAt != nil {
		writeDetailKV(b, width, "Executed:", r.ExecutedAt.Format(time.RFC3339))
	}
	if r.VerifiedAt != nil {
		writeDetailKV(b, width, "Verified:", r.VerifiedAt.Format(time.RFC3339))
	}
	if r.AnomalyID != "" {
		writeDetailKV(b, width, "Anomaly ID:", r.AnomalyID)
	}
	if len(r.AnomalyIDs) > 0 {
		writeDetailKVStyled(b, width, "Anomaly IDs:", mutedStyle, fmt.Sprintf("%v", r.AnomalyIDs))
	}
	if r.Reason != "" {
		writeDetailKV(b, width, "Reason:", r.Reason)
	}
	if r.Error != "" {
		writeDetailKVStyled(b, width, "Error:", errStyle, r.Error)
	}
}

// ── Utility helpers ──

func severitySymbol(reversible bool) string {
	if reversible {
		return successStyle.Render("yes")
	}
	return errStyle.Render("no")
}
