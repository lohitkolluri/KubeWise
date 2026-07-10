package cli

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"charm.land/bubbles/v2/help"
	"charm.land/bubbles/v2/key"
	"charm.land/bubbles/v2/progress"
	"charm.land/bubbles/v2/spinner"
	"charm.land/bubbles/v2/viewport"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"
	"github.com/spf13/cobra"

	"github.com/lohitkolluri/KubeWise/internal/cli/kwtable"
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
	p := tea.NewProgram(m)
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
	lastUpdate  time.Time
	nextRefresh time.Time
	uptime      string

	// Toasts & chrome
	toasts      toastStack
	refreshProg progress.Model
	actionProg  progress.Model
	actionBusy  bool
	actionLabel string

	// Mode state
	ready       bool
	logsFollow  bool
	auditStatus string
	auditSince  string

	// Confirmations
	confirm         confirmKind
	confirmTargetID string // for confirmApproveRemediation
	confirmAffirm   bool   // true = primary action focused

	// Tables (kwtable — bubbles/table fork with full-row selection styling)
	predTable         kwtable.Model
	anomTable         kwtable.Model
	auditTable        kwtable.Model
	approvalTable     kwtable.Model
	healthTable       kwtable.Model

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

	// Mouse
	mouseEnabled bool
}

type auditGroup struct {
	Key     string
	Records []models.AuditRecord
}

type kpiTone int

const (
	kpiToneNeutral kpiTone = iota
	kpiToneInfo
	kpiToneSuccess
	kpiToneWarn
	kpiToneCritical
)

type kpiDef struct {
	label string
	value string
	style lipgloss.Style
	tone  kpiTone
}

func newControlModel(interval time.Duration) controlModel {
	s := spinner.New()
	s.Spinner = spinner.Dot
	s.Style = lipgloss.NewStyle().Foreground(colorAccent)

	m := controlModel{
		interval:      interval,
		tab:           tabDashboard,
		palette:       newPaletteState(),
		keys:          defaultUIKeys(),
		spin:          s,
		refreshProg:   newRefreshProgress(),
		actionProg:    newActionProgress(),
		logsFollow:    true,
		mouseEnabled:  true,
		detailVP:      viewport.New(viewport.WithWidth(80), viewport.WithHeight(10)),
		predTable:     initTable(predCols()),
		anomTable:     initTable(anomCols()),
		auditTable:    initTable(auditCols()),
		approvalTable: initTable(approvalCols()),
		healthTable:   initTable(healthCols()),
	}
	m.help = help.New()
	m.help.ShowAll = false
	m.logsVP = viewport.New(viewport.WithWidth(80), viewport.WithHeight(20))
	m.logsVP.MouseWheelEnabled = true
	m.detailVP.MouseWheelEnabled = true
	m.detailVP.Style = lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(colorAccent).
		Padding(0, 1)
	return m
}

func (m controlModel) Init() tea.Cmd {
	m.nextRefresh = time.Now().Add(m.interval)
	return tea.Batch(
		m.spin.Tick,
		m.refreshProg.Init(),
		m.actionProg.Init(),
		m.refreshAll(),
		m.refreshProg.SetPercent(0.1),
		scheduleTick(m.interval),
	)
}

type uiTickMsg time.Time
type restartDoneMsg struct{ err error }

func scheduleTick(d time.Duration) tea.Cmd {
	return tea.Tick(d, func(t time.Time) tea.Msg { return uiTickMsg(t) })
}

func restartAgentAsync() tea.Cmd {
	return func() tea.Msg {
		return restartDoneMsg{err: restartAgentDeployment()}
	}
}

func (m controlModel) logsAtBottom() bool {
	total := m.logsVP.TotalLineCount()
	if total <= 0 {
		return true
	}
	bottom := total - m.logsVP.Height()
	if bottom < 0 {
		bottom = 0
	}
	return m.logsVP.YOffset() >= bottom
}

func (m *controlModel) maybeDisableLogsFollowAfterScroll() tea.Cmd {
	if m.tab != tabLogs {
		return nil
	}
	if m.logsFollow && !m.logsAtBottom() {
		m.logsFollow = false
		m.notify("logs: follow OFF (scrolled)", toastInfo)
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
	return m.palette.phase != paletteClosed || m.detail != "" || m.detailMeta != "" || m.confirmOpen()
}

// ── Update ──

func (m controlModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.help.SetWidth(msg.Width)
		// Clamp viewport dimensions for tiny terminals.
		vw := msg.Width - 6
		if vw < 1 {
			vw = 1
		}
		vh := m.contentHeight()
		if vh < 1 {
			vh = 1
		}
		m.logsVP.SetWidth(vw)
		m.logsVP.SetHeight(vh)
		m.detailVP.SetWidth(vw)
		m.detailVPHeight = vh
		m.resizeDetailViewport()
		if m.detail != "" || m.detailMeta != "" {
			m.relayoutDetail()
		}
		m.resizePalette()
		// Resize tables
		tw := vw - 4
		if tw < 10 {
			tw = 10
		}
		th := vh - 2
		if th < 3 {
			th = 3
		}
		m.predTable.SetWidth(tw)
		m.predTable.SetHeight(th)
		m.anomTable.SetWidth(tw)
		m.anomTable.SetHeight(th)
		m.auditTable.SetWidth(tw)
		m.auditTable.SetHeight(th)
		m.approvalTable.SetWidth(tw)
		m.approvalTable.SetHeight(th)
		m.healthTable.SetWidth(tw)
		m.healthTable.SetHeight(th)
		m.resizeChrome()
		return m, nil

	case progress.FrameMsg:
		var cmds []tea.Cmd
		if m.loading {
			var cmd tea.Cmd
			m.refreshProg, cmd = m.refreshProg.Update(msg)
			cmds = append(cmds, cmd)
		}
		if m.actionBusy {
			var cmd tea.Cmd
			m.actionProg, cmd = m.actionProg.Update(msg)
			cmds = append(cmds, cmd)
		}
		if len(cmds) == 0 {
			return m, nil
		}
		return m, tea.Batch(cmds...)

	case spinner.TickMsg:
		var cmd tea.Cmd
		m.spin, cmd = m.spin.Update(msg)
		return m, cmd

	case restartDoneMsg:
		m.actionBusy = false
		m.actionLabel = ""
		if msg.err != nil {
			m.err = msg.err
			m.notifyErr(msg.err.Error())
			return m, m.actionProg.SetPercent(0)
		}
		m.notify("Agent deployment restarting…", toastSuccess)
		return m, m.actionProg.SetPercent(1)

	case tea.KeyPressMsg:
		if m.palette.phase != paletteClosed {
			return m.handlePaletteKey(msg)
		}
		if m.confirmOpen() {
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

	case tea.MouseMsg:
		if m.palette.phase != paletteClosed {
			var cmd tea.Cmd
			m.palette.list, cmd = m.palette.list.Update(msg)
			return m, cmd
		}
		if m.confirmOpen() {
			return m, nil
		}
		if m.detail != "" {
			var cmd tea.Cmd
			m.detailVP, cmd = m.detailVP.Update(msg)
			return m, cmd
		}
		switch m.tab {
		case tabLogs:
			var cmd tea.Cmd
			m.logsVP, cmd = m.logsVP.Update(msg)
			return m, cmd
		}
		return m, nil

	case uiTickMsg:
		m.toasts.prune(time.Now())
		if m.shouldPauseRefresh() || m.loading {
			return m, scheduleTick(m.interval)
		}
		m.loading = true
		m.nextRefresh = time.Now().Add(m.interval)
		return m, tea.Batch(m.refreshAll(), m.refreshProg.SetPercent(0.2), scheduleTick(m.interval))

	case dataMsg:
		m.loading = false
		m.ready = true
		m.nextRefresh = time.Now().Add(m.interval)

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
			m.predTable.SetRows(buildPredRows(msg.preds))
			m.anomTable.SetRows(buildAnomRows(msg.anomalies))
			m.auditTable.SetRows(buildAuditRows(m.auditGroups))
			m.approvalTable.SetRows(buildApprovalRows(m.approvalGroups))
			m.healthTable.SetRows(buildHealthRows(msg.healthScores))
			m.logsVP.SetContent(colorizeLogs(msg.logs))
			if m.logsFollow {
				m.logsVP.GotoBottom()
			}
		}
		return m, m.refreshProg.SetPercent(1)
	}
	return m, nil
}

// ── Confirmation handling ──

// (see ui_confirm.go)

// ── Main key handler ──

func (m *controlModel) handleMainKey(msg tea.KeyPressMsg) (tea.Model, tea.Cmd) {
	switch {
	case key.Matches(msg, m.keys.Quit):
		return m, tea.Quit
	case key.Matches(msg, m.keys.Palette):
		return m, m.openPalette()
	case key.Matches(msg, m.keys.Help):
		m.help.ShowAll = !m.help.ShowAll
		m.logsVP.SetHeight(m.contentHeight())
		return m, nil
	case key.Matches(msg, m.keys.Refresh):
		m.notify("Refreshing…", toastInfo)
		return m, tea.Batch(m.refreshAll(), m.refreshProg.SetPercent(0.15))
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
		if tbl := m.tableForTab(m.tab); tbl != nil {
			var cmd tea.Cmd
			*tbl, cmd = tbl.Update(msg)
			return m, cmd
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
		if tbl := m.tableForTab(m.tab); tbl != nil {
			var cmd tea.Cmd
			*tbl, cmd = tbl.Update(msg)
			return m, cmd
		}
		if m.cursor[m.tab] > 0 {
			m.cursor[m.tab]--
		}
	case key.Matches(msg, m.keys.Top):
		if m.tab == tabLogs {
			m.logsVP.GotoTop()
			return m, m.maybeDisableLogsFollowAfterScroll()
		}
		if tbl := m.tableForTab(m.tab); tbl != nil {
			tbl.GotoTop()
			return m, nil
		}
		m.cursor[m.tab] = 0
	case key.Matches(msg, m.keys.Bottom):
		if m.tab == tabLogs {
			m.logsVP.GotoBottom()
			if !m.logsFollow {
				m.logsFollow = true
				m.notify("logs: follow ON", toastInfo)
			}
			return m, nil
		}
		if tbl := m.tableForTab(m.tab); tbl != nil {
			tbl.GotoBottom()
			return m, nil
		}
		m.cursor[m.tab] = m.listLen(m.tab) - 1
		if m.cursor[m.tab] < 0 {
			m.cursor[m.tab] = 0
		}
	case key.Matches(msg, m.keys.Detail):
		m.showDetailForSelection()
	case key.Matches(msg, m.keys.Copy):
		if m.detail != "" || m.detailMeta != "" {
			content := m.detailMeta + "\n" + m.detail
			content = ansi.Strip(content)
			m.notify("copied to clipboard", toastSuccess)
			return m, tea.SetClipboard(content)
		}
	case key.Matches(msg, m.keys.DryRun), key.Matches(msg, m.keys.ToggleLive):
		return m.toggleRemediationMode()
	case key.Matches(msg, m.keys.Approve):
		if m.tab == tabApprovals && len(m.pending) > 0 {
			i := m.cursor[tabApprovals]
			groups := m.approvalGroups
			if len(groups) == 0 {
				groups = groupAuditRecords(m.pending, true)
			}
			if i >= 0 && i < len(groups) {
				r := groups[i].Records[0]
				return m, m.openConfirmForm(confirmApproveRemediation, r.ID)
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
				m.notify("logs: follow ON", toastInfo)
			} else {
				m.notify("logs: follow OFF", toastInfo)
			}
			return m, nil
		}
	case key.Matches(msg, m.keys.Mode):
		if m.tab == tabConfig && m.config != nil {
			m.config.Remediation.Mode = nextRemediationMode(m.config.Remediation.Mode)
			if err := putAgentConfig(m.config); err != nil {
				m.err = err
			} else {
				m.notify(fmt.Sprintf("mode=%s saved", m.config.Remediation.Mode), toastSuccess)
				m.err = nil
				return m, nil
			}
		}
	case key.Matches(msg, m.keys.Restart):
		return m, m.openConfirmForm(confirmRestart, "")
	case key.Matches(msg, m.keys.ToggleMouse):
		m.mouseEnabled = !m.mouseEnabled
		if m.mouseEnabled {
			m.notify("mouse capture ON — Ctrl+S to toggle", toastInfo)
		} else {
			m.notify("mouse capture OFF — native selection allowed", toastInfo)
		}
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
	// Table tabs manage their own cursor internally
	if m.tableForTab(m.tab) != nil {
		return
	}
	max := m.listLen(m.tab) - 1
	if max < 0 {
		max = 0
	}
	if m.cursor[m.tab] > max {
		m.cursor[m.tab] = max
	}
}

func (m *controlModel) tableForTab(tab int) *kwtable.Model {
	switch tab {
	case tabPredict:
		return &m.predTable
	case tabAnomalies:
		return &m.anomTable
	case tabAudit:
		return &m.auditTable
	case tabApprovals:
		return &m.approvalTable
	case tabHealth:
		return &m.healthTable
	default:
		return nil
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

func (m controlModel) View() tea.View {
	if m.width == 0 {
		return tea.NewView(logoStyle.Render(" KubeWise ") + "\n" + mutedStyle.Render("Connecting…"))
	}

	var base string
	if m.confirmOpen() {
		base = m.renderConfirmOverlay()
	} else if m.palette.phase != paletteClosed {
		base = m.renderPalette()
	} else {
		var b strings.Builder
		b.WriteString(m.renderHeader())
		b.WriteString("\n")
		if toastStrip := m.toasts.strip(); toastStrip != "" {
			b.WriteString(toastStrip)
			b.WriteString("\n")
		}
		b.WriteString(m.renderTabs())
		b.WriteString("\n")

		if m.detail != "" || m.detailMeta != "" {
			b.WriteString(m.renderDetail())
		} else {
			content := clipToLines(m.renderTabContent(), m.panelInnerHeight())
			b.WriteString(panelStyle.Width(m.width - 2).Height(m.contentHeight()).Render(content))
		}
		b.WriteString("\n")
		b.WriteString(m.renderFooter())
		base = b.String()
	}

	v := tea.NewView(base)
	v.AltScreen = uiAltScreen
	if m.confirmOpen() {
		v.MouseMode = tea.MouseModeNone
	} else if m.mouseEnabled {
		v.MouseMode = tea.MouseModeCellMotion
	}
	return v
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

	// Freshness + refresh countdown
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
	if !m.nextRefresh.IsZero() && m.ready && !m.loading {
		if rem := time.Until(m.nextRefresh).Round(time.Second); rem > 0 {
			freshness += " " + mutedStyle.Render(fmt.Sprintf("↻%ds", int(rem.Seconds())))
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
	var labels []string
	var indicators []string
	counts := m.tabCounts()
	for i, label := range tabLabels {
		style := tabInactiveStyle
		if i == m.tab {
			style = tabActiveStyle
		}
		text := fmt.Sprintf("%d %s", i+1, label)
		if n := counts[i]; n > 0 && i != m.tab {
			text += fmt.Sprintf(" %s%d", tabBadgeStyle.Render("●"), n)
		}
		rendered := style.Render(text)
		w := lipgloss.Width(rendered)
		labels = append(labels, rendered)
		if i == m.tab {
			indicators = append(indicators, tabIndicatorStyle.Width(w).Render(strings.Repeat("─", max(1, w))))
		} else {
			indicators = append(indicators, strings.Repeat(" ", w))
		}
	}
	hint := mutedStyle.Render("  ←/→")
	tabLine := strings.Join(labels, "")
	indicatorLine := strings.Join(indicators, "")
	return tabLine + hint + "\n" + indicatorLine
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
		return m.tableOrEmpty(m.predTable.View(), emptyStateStyle.Render("No active predictions")+"\n"+
			mutedStyle.Render("  Predictions appear when the agent detects upcoming issues"))
	case tabAnomalies:
		if m.errTab[tabAnomalies] != nil {
			return m.renderTabError("anomalies", m.errTab[tabAnomalies])
		}
		return m.tableOrEmpty(m.anomTable.View(), emptyStateStyle.Render("No anomalies")+"\n"+
			mutedStyle.Render("  Anomalies appear when metrics deviate from expected patterns"))
	case tabAudit:
		if m.errTab[tabAudit] != nil {
			return m.renderTabError("audit", m.errTab[tabAudit])
		}
		return m.tableOrEmpty(m.auditTable.View(), emptyStateStyle.Render("No remediation records")+"\n"+
			mutedStyle.Render("  Audit records appear after remediations are attempted"))
	case tabApprovals:
		if m.errTab[tabApprovals] != nil {
			return m.renderTabError("approvals", m.errTab[tabApprovals])
		}
		return m.tableOrEmpty(m.approvalTable.View(), emptyStateStyle.Render("No pending approvals")+"\n"+
			mutedStyle.Render("  T3 actions appear here when live mode is enabled and need your approval"))
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
		pos := fmt.Sprintf("%d/%d", min(m.logsVP.YOffset()+m.logsVP.Height(), m.logsVP.TotalLineCount()), max(1, m.logsVP.TotalLineCount()))
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
		return m.tableOrEmpty(m.healthTable.View(), emptyStateStyle.Render("No health scores")+"\n"+
			mutedStyle.Render("  Health scores appear after the agent computes them during scrape cycles"))
	default:
		return ""
	}
}

// colorizeLogs applies charm log level styling to agent log lines.

func (m controlModel) tableOrEmpty(view string, emptyMsg string) string {
	// If the table view is just headers + empty space, show empty message
	if view == "" || strings.Count(view, "\n") <= 1 {
		return emptyMsg
	}
	return view
}

func (m controlModel) renderTabError(tabName string, err error) string {
	return warnStyle.Render(fmt.Sprintf("⚠ %s unavailable", tabName)) + "\n" +
		mutedStyle.Render(err.Error()) + "\n\n" +
		mutedStyle.Render("Press r to retry")
}

// ── Dashboard (see ui_dashboard.go) ──

func kpiValueStyleForCount(n, threshold int) lipgloss.Style {
	if n >= threshold*2 {
		return kpiCriticalStyle
	}
	if n >= threshold {
		return kpiWarnStyle
	}
	return kpiSuccessStyle
}

// ── Table helpers (kwtable) ──

func initTable(cols []kwtable.Column) kwtable.Model {
	t := kwtable.New(
		kwtable.WithColumns(cols),
		kwtable.WithFocused(true),
		kwtable.WithHeight(10),
	)
	t.SetStyles(kwtable.Styles{
		Header: lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Padding(0, 1),
		Cell:   lipgloss.NewStyle().Foreground(colorSubtle).Padding(0, 1),
		Selected: lipgloss.NewStyle().
			Background(colorSelected).
			Foreground(colorHighlight).
			Bold(true).
			Padding(0, 1),
	})
	return t
}

func predCols() []kwtable.Column {
	return []kwtable.Column{
		{Title: "TYPE", Width: 14},
		{Title: "ENTITY", Width: 24},
		{Title: "SCORE", Width: 8},
		{Title: "ETA", Width: 8},
		{Title: "ACTION", Width: 20},
	}
}

func anomCols() []kwtable.Column {
	return []kwtable.Column{
		{Title: "PATTERN", Width: 10},
		{Title: "ENTITY", Width: 24},
		{Title: "SCORE", Width: 8},
		{Title: "STATUS", Width: 12},
	}
}

func recentCols() []kwtable.Column {
	return []kwtable.Column{
		{Title: "TYPE", Width: 10},
		{Title: "ENTITY", Width: 24},
		{Title: "SCORE", Width: 8},
		{Title: "STATUS", Width: 12},
	}
}

func auditCols() []kwtable.Column {
	return []kwtable.Column{
		{Title: "STATUS", Width: 14},
		{Title: "ACTION", Width: 28},
		{Title: "COUNT", Width: 7},
		{Title: "TIER", Width: 8},
		{Title: "REASON", Width: 33},
	}
}

func approvalCols() []kwtable.Column {
	return []kwtable.Column{
		{Title: "TIER", Width: 10},
		{Title: "ACTION", Width: 28},
		{Title: "COUNT", Width: 7},
		{Title: "CONF", Width: 8},
		{Title: "REASON", Width: 33},
	}
}

func healthCols() []kwtable.Column {
	return []kwtable.Column{
		{Title: "ENTITY", Width: 24},
		{Title: "NAMESPACE", Width: 16},
		{Title: "SCORE", Width: 8},
		{Title: "FACTORS", Width: 40},
	}
}

// ── Table row builders ──

func buildPredRows(preds []models.PredictionResult) []kwtable.Row {
	if len(preds) == 0 {
		return nil
	}
	rows := make([]kwtable.Row, len(preds))
	for i, p := range preds {
		rows[i] = kwtable.Row{
			p.Type,
			p.Entity,
			scoreStyle(p.Score).Render(fmt.Sprintf("%.0f%%", p.Score*100)),
			fmt.Sprintf("%.0f", p.ETASeconds),
			p.Action,
		}
	}
	return rows
}

func buildAnomRows(anomalies []models.AnomalyRecord) []kwtable.Row {
	if len(anomalies) == 0 {
		return nil
	}
	rows := make([]kwtable.Row, len(anomalies))
	for i, a := range anomalies {
		pat := a.Pattern
		if pat == "" {
			pat = "statistical"
		}
		rows[i] = kwtable.Row{
			pat,
			a.Entity,
			scoreStyle(a.Score).Render(fmt.Sprintf("%.2f", a.Score)),
			statusStyle(a.Status).Render(a.Status),
		}
	}
	return rows
}

func buildAuditRows(groups []auditGroup) []kwtable.Row {
	if len(groups) == 0 {
		return nil
	}
	rows := make([]kwtable.Row, len(groups))
	for i, g := range groups {
		r := g.Records[0]
		action := fmt.Sprintf("%s/%s", r.Plan.Action.Type, r.Plan.Action.Target)
		status := normalizedAuditStatus(r)
		count := ""
		if len(g.Records) > 1 {
			count = fmt.Sprintf("%dx", len(g.Records))
		}
		rows[i] = kwtable.Row{
			statusStyle(status).Render(status),
			action,
			count,
			riskTierStyle(string(r.RiskTier)).Render(string(r.RiskTier)),
			r.Reason,
		}
	}
	return rows
}

func buildApprovalRows(groups []auditGroup) []kwtable.Row {
	if len(groups) == 0 {
		return nil
	}
	rows := make([]kwtable.Row, len(groups))
	for i, g := range groups {
		r := g.Records[0]
		action := fmt.Sprintf("%s %s/%s", r.Plan.Action.Type, r.Plan.Action.Namespace, r.Plan.Action.Target)
		maxConf := 0.0
		for _, rr := range g.Records {
			if rr.Plan.Diagnosis.Confidence > maxConf {
				maxConf = rr.Plan.Diagnosis.Confidence
			}
		}
		count := ""
		if len(g.Records) > 1 {
			count = fmt.Sprintf("%dx", len(g.Records))
		}
		rows[i] = kwtable.Row{
			riskTierStyle(string(r.RiskTier)).Render(string(r.RiskTier)),
			action,
			count,
			fmt.Sprintf("%.0f%%", maxConf*100),
			r.Reason,
		}
	}
	return rows
}

func buildHealthRows(scores []models.HealthScore) []kwtable.Row {
	if len(scores) == 0 {
		return nil
	}
	rows := make([]kwtable.Row, len(scores))
	for i, hs := range scores {
		var factors []string
		for _, f := range hs.Factors {
			label := f.Name
			if len(label) > 8 {
				label = label[:8]
			}
			factors = append(factors, fmt.Sprintf("%s=%d", label, int(f.Score*100)))
		}
		factorStr := ""
		if len(factors) > 0 {
			factorStr = strings.Join(factors, " ")
		}
		rows[i] = kwtable.Row{
			hs.Entity,
			hs.Namespace,
			scoreStyle(hs.Score / 100.0).Render(fmt.Sprintf("%.0f", hs.Score)),
			factorStr,
		}
	}
	return rows
}

func buildHealthRowsTop(scores []models.HealthScore, limit int) []kwtable.Row {
	rows := buildHealthRows(scores)
	if limit > 0 && len(rows) > limit {
		rows = rows[:limit]
	}
	return rows
}

func buildApprovalRowsTop(groups []auditGroup, limit int) []kwtable.Row {
	rows := buildApprovalRows(groups)
	if limit > 0 && len(rows) > limit {
		rows = rows[:limit]
	}
	return rows
}

func buildRecentRows(preds []models.PredictionResult, anomalies []models.AnomalyRecord, limit int) []kwtable.Row {
	if limit <= 0 {
		limit = 5
	}
	if len(preds) == 0 && len(anomalies) == 0 {
		return nil
	}

	type recentItem struct {
		at  time.Time
		row kwtable.Row
	}
	items := make([]recentItem, 0, len(preds)+len(anomalies))

	for _, p := range preds {
		items = append(items, recentItem{
			at: p.Timestamp,
			row: kwtable.Row{
				trunc(p.Type, 10),
				trunc(p.Entity, 24),
				scoreStyle(p.Score).Render(fmt.Sprintf("%.0f%%", p.Score*100)),
				infoStyle.Render(fmt.Sprintf("ETA %.0fs", p.ETASeconds)),
			},
		})
	}
	for _, a := range anomalies {
		at := time.Time{}
		if a.DetectedAt != nil {
			at = *a.DetectedAt
		}
		pat := a.Pattern
		if pat == "" {
			pat = "statistical"
		}
		items = append(items, recentItem{
			at: at,
			row: kwtable.Row{
				trunc(pat, 10),
				trunc(a.Entity, 24),
				scoreStyle(a.Score).Render(fmt.Sprintf("%.2f", a.Score)),
				statusStyle(a.Status).Render(a.Status),
			},
		})
	}

	sort.Slice(items, func(i, j int) bool {
		return items[i].at.After(items[j].at)
	})
	if len(items) > limit {
		items = items[:limit]
	}

	rows := make([]kwtable.Row, len(items))
	for i, item := range items {
		rows[i] = item.row
	}
	return rows
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
	top := m.detailVP.YOffset()

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
	m.detailVP.SetHeight(innerH)
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
		pct = float64(m.detailVP.YOffset()) / float64(den)
	}

	m.detailVP.SetContent(m.detail)

	newTotal := m.detailVP.TotalLineCount()
	newShown := m.detailVP.VisibleLineCount()
	newDen := newTotal - newShown
	if newDen > 0 {
		m.detailVP.SetYOffset(int(pct * float64(newDen)))
		if m.detailVP.YOffset() < 0 {
			m.detailVP.SetYOffset(0)
		}
		if m.detailVP.YOffset() > newDen {
			m.detailVP.SetYOffset(newDen)
		}
	} else {
		m.detailVP.SetYOffset(0)
	}
}

// ── Footer ──

func (m controlModel) renderFooter() string {
	var b strings.Builder

	if bar := m.renderLoadingBar(); bar != "" {
		b.WriteString(bar)
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

func (m *controlModel) executePendingConfirm() (tea.Model, tea.Cmd) {
	kind := m.confirm
	target := m.confirmTargetID
	m.confirm = confirmNone
	m.confirmTargetID = ""
	m.confirmAffirm = false

	switch kind {
	case confirmEnableLive:
		mode, err := setRemediationLive(true)
		if err != nil {
			m.err = err
		} else {
			m.remMode = mode
			m.notify("LIVE mode — remediations will execute", toastSuccess)
			m.err = nil
			return m, tea.Batch(m.refreshAll(), m.refreshProg.SetPercent(0.2))
		}
	case confirmApproveRemediation:
		if err := approveRemediation(target); err != nil {
			m.err = err
			m.notifyErr(err.Error())
		} else {
			m.notify("Remediation approved and executed", toastSuccess)
			m.err = nil
			return m, tea.Batch(m.refreshAll(), m.refreshProg.SetPercent(0.2))
		}
	default: // confirmRestart
		m.actionBusy = true
		m.actionLabel = "Restarting agent"
		return m, tea.Batch(m.actionProg.SetPercent(0.4), restartAgentAsync())
	}
	return m, nil
}

// ── Toggle mode ──

func (m *controlModel) toggleRemediationMode() (tea.Model, tea.Cmd) {
	if m.remMode.Live {
		mode, err := setRemediationLive(false)
		if err != nil {
			m.err = err
			return m, nil
		}
		m.remMode = mode
		m.notify("OBSERVE mode — dry-run only", toastSuccess)
		m.err = nil
		return m, nil
	}
	return m, m.openConfirmForm(confirmEnableLive, "")
}

// ── Palette overlay ──

func (m controlModel) renderPalette() string {
	var b strings.Builder
	if m.palette.phase == paletteInput {
		b.WriteString(detailSectionStyle.Render(m.paletteInputTitle))
		b.WriteString("\n")
		b.WriteString(m.palette.input.View())
	} else {
		b.WriteString(m.palette.search.View())
		b.WriteString("\n")
		b.WriteString(m.palette.list.View())
	}
	overlay := lipgloss.Place(m.width, m.height, lipgloss.Center, lipgloss.Center,
		confirmBoxStyle.Width(min(60, m.width-4)).Render(b.String()))
	return overlay
}

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
		i := m.predTable.Cursor()
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
		i := m.anomTable.Cursor()
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
		i := m.auditTable.Cursor()
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
		i := m.approvalTable.Cursor()
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
		i := m.healthTable.Cursor()
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
