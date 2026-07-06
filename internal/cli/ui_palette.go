package cli

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/charmbracelet/bubbles/key"
	"github.com/charmbracelet/bubbles/list"
	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/lohitkolluri/KubeWise/pkg/models"
	"github.com/sahilm/fuzzy"
)

type palettePhase int

const (
	paletteClosed palettePhase = iota
	paletteBrowse
	paletteInput
)

type paletteItem struct {
	title    string
	desc     string
	category string
	keywords string
	run      func(m *controlModel) paletteOutcome
}

func (i paletteItem) Title() string       { return i.title }
func (i paletteItem) Description() string { return i.desc }
func (i paletteItem) FilterValue() string {
	return i.title + " " + i.desc + " " + i.category + " " + i.keywords
}

type paletteOutcome struct {
	msg    string
	err    error
	prompt *palettePrompt
}

type palettePrompt struct {
	title       string
	placeholder string
	value       string
	apply       func(m *controlModel, value string) (string, error)
}

type paletteState struct {
	phase    palettePhase
	list     list.Model
	input    textinput.Model
	search   textinput.Model
	allItems []list.Item
}

func newPaletteState() paletteState {
	delegate := list.NewDefaultDelegate()
	delegate.ShowDescription = true
	delegate.Styles.SelectedTitle = delegate.Styles.SelectedTitle.Foreground(colorAccent)
	delegate.Styles.SelectedDesc = delegate.Styles.SelectedDesc.Foreground(colorMuted)

	l := list.New([]list.Item{}, delegate, 0, 0)
	l.SetShowTitle(false)
	l.SetShowFilter(false)
	l.SetShowStatusBar(false)
	l.SetShowHelp(false)
	l.SetShowPagination(true)
	l.SetFilteringEnabled(false)
	l.DisableQuitKeybindings()
	l.Styles.NoItems = lipgloss.NewStyle().Foreground(colorMuted).PaddingLeft(2)

	search := textinput.New()
	search.Prompt = "> "
	search.Placeholder = "Search commands…"
	search.CharLimit = 64
	search.PromptStyle = lipgloss.NewStyle().Foreground(colorAccent).Bold(true)
	search.TextStyle = lipgloss.NewStyle().Foreground(colorHighlight)

	ti := textinput.New()
	ti.Prompt = "› "
	ti.CharLimit = 256
	ti.Placeholder = "value…"

	return paletteState{list: l, input: ti, search: search}
}

var remediationModeCycle = []string{
	models.RemediationModeDryRun,
	models.RemediationModeAuto,
	models.RemediationModeSemi,
	models.RemediationModeOff,
}

var outputFormatCycle = []string{"table", "json", "yaml"}

func currentRemediationMode(m *controlModel) string {
	if m.remMode.Mode != "" {
		return m.remMode.Mode
	}
	if m.config != nil && m.config.Remediation.Mode != "" {
		return m.config.Remediation.Mode
	}
	return models.RemediationModeDryRun
}

func nextInCycle(values []string, current string) string {
	for i, v := range values {
		if v == current {
			return values[(i+1)%len(values)]
		}
	}
	return values[0]
}

func (m *controlModel) initPaletteItems() []list.Item {
	execLabel := "OBSERVE"
	if m.remMode.Live {
		execLabel = "LIVE"
	}
	remMode := currentRemediationMode(m)
	outFmt := outputFormat
	if outFmt == "" {
		outFmt = "table"
	}

	cmds := []paletteItem{
		{"Go to Dashboard", "Navigate · overview and stats", "Navigate", "home overview 1", paletteGoTab(tabDashboard)},
		{"Go to Predictions", "Navigate · active failure predictions", "Navigate", "predict forecast 2", paletteGoTab(tabPredict)},
		{"Go to Anomalies", "Navigate · detected anomalies", "Navigate", "anomaly detect 3", paletteGoTab(tabAnomalies)},
		{"Go to Audit", "Navigate · remediation audit log", "Navigate", "remediation audit 4", paletteGoTab(tabAudit)},
		{"Go to Approvals", "Navigate · pending T3 remediations", "Navigate", "approve pending 5", paletteGoTab(tabApprovals)},
		{"Go to Config", "Navigate · agent configuration", "Navigate", "settings config 6", paletteGoTab(tabConfig)},
		{"Go to Logs", "Navigate · agent pod logs", "Navigate", "log tail 7", paletteGoTab(tabLogs)},

		{"Refresh", "Action · fetch latest data from agent", "Action", "reload sync r", paletteRefresh()},
		{"Execution mode: " + execLabel, "Remediation · click to switch OBSERVE ↔ LIVE", "Remediation", "live observe dry run execute", paletteToggleExecution()},
		{"Remediation mode: " + remMode, "Remediation · click to cycle dry-run → auto → semi → off", "Remediation", "mode safe autonomous disable partial", paletteCycleRemediationMode()},
		{"Restart agent", "Agent · rolling restart deployment", "Agent", "rollout redeploy", paletteRestartAgent()},
		{"Connect check", "Agent · ping health endpoint", "Agent", "health ping", paletteHealthCheck()},

		{"Set scrape interval", "Config · e.g. 30s, 1m", "Config", "scrape interval", paletteConfigPrompt("scrape_interval", "Scrape interval", "30s", applyScrapeInterval)},
		{"Set Prometheus URL", "Config · Prometheus server address", "Config", "prometheus metrics", paletteConfigPrompt("prometheus", "Prometheus URL", "http://prometheus:9090", applyPrometheusURL)},
		{"Set LLM provider", "Config · openrouter, ollama, …", "Config", "llm provider", paletteConfigPrompt("llm_provider", "LLM provider", "openrouter", applyLLMProvider)},
		{"Set LLM model", "Config · model identifier", "Config", "llm model", paletteConfigPrompt("llm_model", "LLM model", "meta-llama/llama-3.1-8b-instruct", applyLLMModel)},
		{"Set rate limit", "Config · max remediations per hour", "Config", "rate limit", paletteConfigPrompt("rate_limit", "Rate limit", "10", applyRateLimit)},

		{"Set agent URL", "Profile · agent HTTP base URL", "Profile", "url localhost port-forward", paletteProfilePrompt("agent-url", "Agent URL", resolveAgentURL())},
		{"Set agent namespace", "Profile · K8s namespace for agent", "Profile", "namespace kubewise", paletteProfilePrompt("agent-namespace", "Agent namespace", agentNS)},
		{"Set HTTP timeout", "Profile · request timeout in seconds", "Profile", "timeout seconds", paletteProfilePrompt("timeout", "HTTP timeout (seconds)", paletteTimeoutStr())},
		{"Output format: " + outFmt, "Profile · click to cycle table → json → yaml", "Profile", "format output table json yaml", paletteCycleOutputFormat()},

		{"Quit", "Action · exit control center", "Action", "exit q", paletteQuit()},
	}

	items := make([]list.Item, len(cmds))
	for i, c := range cmds {
		items[i] = c
	}
	return items
}

func (m *controlModel) applyPaletteFilter(query string) {
	items := m.palette.allItems
	if query != "" {
		targets := make([]string, len(items))
		for i, it := range items {
			targets[i] = it.(paletteItem).FilterValue()
		}
		matches := fuzzy.Find(query, targets)
		filtered := make([]list.Item, len(matches))
		for i, match := range matches {
			filtered[i] = items[match.Index]
		}
		items = filtered
	}
	m.palette.list.SetItems(items)
	m.palette.list.ResetSelected()
}

func paletteTimeoutStr() string {
	if httpTimeout > 0 {
		return strconv.Itoa(httpTimeout)
	}
	return strconv.Itoa(activeProfile().TimeoutSeconds)
}

func paletteGoTab(tab int) func(m *controlModel) paletteOutcome {
	return func(m *controlModel) paletteOutcome {
		m.tab = tab
		m.clampCursor()
		return paletteOutcome{msg: "→ " + tabLabels[tab]}
	}
}

func paletteRefresh() func(m *controlModel) paletteOutcome {
	return func(m *controlModel) paletteOutcome {
		return paletteOutcome{msg: "Refreshing…"}
	}
}

func paletteToggleExecution() func(m *controlModel) paletteOutcome {
	return func(m *controlModel) paletteOutcome {
		if m.remMode.Live {
			mode, err := setRemediationLive(false)
			if err != nil {
				return paletteOutcome{err: err}
			}
			m.remMode = mode
			return paletteOutcome{msg: "OBSERVE mode — dry-run only"}
		}
		m.confirm = confirmEnableLive
		return paletteOutcome{msg: "Confirm LIVE mode below (y/n)"}
	}
}

func paletteCycleRemediationMode() func(m *controlModel) paletteOutcome {
	return func(m *controlModel) paletteOutcome {
		current := currentRemediationMode(m)
		next := nextInCycle(remediationModeCycle, current)
		mode, err := setRemediationMode(next)
		if err != nil {
			return paletteOutcome{err: err}
		}
		m.remMode = mode
		if m.config != nil {
			m.config.Remediation.Mode = mode.Mode
			m.config.Remediation.DryRun = mode.DryRun
		}
		return paletteOutcome{msg: "remediation mode → " + next}
	}
}

func paletteCycleOutputFormat() func(m *controlModel) paletteOutcome {
	return func(m *controlModel) paletteOutcome {
		current := outputFormat
		if current == "" {
			current = "table"
		}
		next := nextInCycle(outputFormatCycle, current)
		outputFormat = next
		if err := setProfileField("", "output", next); err != nil {
			return paletteOutcome{err: err}
		}
		return paletteOutcome{msg: "output format → " + next}
	}
}

func paletteRestartAgent() func(m *controlModel) paletteOutcome {
	return func(m *controlModel) paletteOutcome {
		m.confirm = confirmRestart
		return paletteOutcome{msg: "Confirm restart below (y/n)"}
	}
}

func paletteHealthCheck() func(m *controlModel) paletteOutcome {
	return func(m *controlModel) paletteOutcome {
		h, err := fetchHealth()
		if err != nil {
			return paletteOutcome{err: err}
		}
		return paletteOutcome{msg: "health: " + h["status"]}
	}
}

func paletteQuit() func(m *controlModel) paletteOutcome {
	return func(m *controlModel) paletteOutcome {
		m.paletteQuitApp = true
		return paletteOutcome{msg: "Goodbye"}
	}
}

func paletteConfigPrompt(key, title, placeholder string, apply func(*controlModel, string) (string, error)) func(m *controlModel) paletteOutcome {
	return func(m *controlModel) paletteOutcome {
		cur := ""
		if m.config != nil {
			switch key {
			case "scrape_interval":
				cur = m.config.ScrapeInterval
			case "prometheus":
				cur = m.config.PrometheusAddress
			case "llm_provider":
				cur = m.config.LLMProvider
			case "llm_model":
				cur = m.config.LLMModel
			case "rate_limit":
				cur = strconv.Itoa(m.config.Remediation.RateLimit)
			}
		}
		return paletteOutcome{prompt: &palettePrompt{title: title, placeholder: placeholder, value: cur, apply: apply}}
	}
}

func paletteProfilePrompt(key, title, current string) func(m *controlModel) paletteOutcome {
	return func(m *controlModel) paletteOutcome {
		return paletteOutcome{prompt: &palettePrompt{
			title:       title,
			placeholder: current,
			value:       current,
			apply: func(m *controlModel, v string) (string, error) {
				if err := setProfileField("", key, v); err != nil {
					return "", err
				}
				applyProfileDefaults()
				switch key {
				case "agent-url", "agent_url":
					agentURL = v
				case "agent-namespace", "agent_namespace":
					agentNS = v
				case "output":
					outputFormat = v
				case "timeout", "timeout_seconds":
					if n, err := strconv.Atoi(v); err == nil {
						httpTimeout = n
					}
				}
				return key + "=" + v, nil
			},
		}}
	}
}

func applyScrapeInterval(m *controlModel, v string) (string, error) {
	if m.config == nil {
		return "", fmt.Errorf("no agent config")
	}
	m.config.ScrapeInterval = v
	if err := putAgentConfig(m.config); err != nil {
		return "", err
	}
	return "scrape_interval=" + v, nil
}

func applyPrometheusURL(m *controlModel, v string) (string, error) {
	if m.config == nil {
		return "", fmt.Errorf("no agent config")
	}
	m.config.PrometheusAddress = v
	if err := putAgentConfig(m.config); err != nil {
		return "", err
	}
	return "prometheus=" + v, nil
}

func applyLLMProvider(m *controlModel, v string) (string, error) {
	if m.config == nil {
		return "", fmt.Errorf("no agent config")
	}
	m.config.LLMProvider = v
	if err := putAgentConfig(m.config); err != nil {
		return "", err
	}
	return "llm_provider=" + v, nil
}

func applyLLMModel(m *controlModel, v string) (string, error) {
	if m.config == nil {
		return "", fmt.Errorf("no agent config")
	}
	m.config.LLMModel = v
	if err := putAgentConfig(m.config); err != nil {
		return "", err
	}
	return "llm_model=" + v, nil
}

func applyRateLimit(m *controlModel, v string) (string, error) {
	if m.config == nil {
		return "", fmt.Errorf("no agent config")
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < 0 {
		return "", fmt.Errorf("invalid rate limit")
	}
	m.config.Remediation.RateLimit = n
	if err := putAgentConfig(m.config); err != nil {
		return "", err
	}
	return fmt.Sprintf("rate_limit=%d", n), nil
}

func (m *controlModel) resizePalette() {
	w := m.width - 8
	if w < 40 {
		w = 40
	}
	h := m.height - 8
	if h < 12 {
		h = 12
	}
	m.palette.list.SetWidth(w)
	m.palette.list.SetHeight(h - 2) // room for search bar
	m.palette.search.Width = w
	m.palette.input.Width = w
}

func (m *controlModel) openPalette() tea.Cmd {
	items := m.initPaletteItems()
	m.palette.allItems = items
	m.palette.search.Reset()
	m.palette.search.Focus()
	m.applyPaletteFilter("")
	m.palette.phase = paletteBrowse
	m.palette.input.Reset()
	m.resizePalette()
	return textinput.Blink
}

func (m *controlModel) closePalette() {
	m.palette.phase = paletteClosed
	m.palette.search.Blur()
	m.palette.input.Blur()
}

func (m *controlModel) handlePaletteKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch m.palette.phase {
	case paletteBrowse:
		switch {
		case key.Matches(msg, key.NewBinding(key.WithKeys("esc", "ctrl+p", "ctrl+["))):
			m.closePalette()
			return m, nil
		case key.Matches(msg, key.NewBinding(key.WithKeys("enter"))):
			if len(m.palette.list.VisibleItems()) == 0 {
				return m, nil
			}
			sel, ok := m.palette.list.SelectedItem().(paletteItem)
			if !ok {
				return m, nil
			}
			out := sel.run(m)
			if out.prompt != nil {
				m.palette.phase = paletteInput
				m.palette.input.SetValue(out.prompt.value)
				m.palette.input.Placeholder = out.prompt.placeholder
				m.palette.input.Focus()
				m.palette.search.Blur()
				m.paletteInputTitle = out.prompt.title
				m.paletteInputApply = out.prompt.apply
				return m, textinput.Blink
			}
			m.closePalette()
			if out.err != nil {
				m.err = out.err
			} else {
				m.err = nil
				if out.msg != "" {
					m.statusMsg = out.msg
				}
			}
			if out.msg == "Refreshing…" {
				m.loading = true
				return m, m.refreshAll()
			}
			if m.paletteQuitApp {
				return m, tea.Quit
			}
			if out.msg != "" && out.msg != "Refreshing…" {
				return m, scheduleToastClear()
			}
			return m, nil
		case key.Matches(msg, m.palette.list.KeyMap.CursorUp),
			key.Matches(msg, m.palette.list.KeyMap.CursorDown),
			key.Matches(msg, m.palette.list.KeyMap.PrevPage),
			key.Matches(msg, m.palette.list.KeyMap.NextPage),
			key.Matches(msg, m.palette.list.KeyMap.GoToStart),
			key.Matches(msg, m.palette.list.KeyMap.GoToEnd):
			var cmd tea.Cmd
			m.palette.list, cmd = m.palette.list.Update(msg)
			return m, cmd
		default:
			prev := m.palette.search.Value()
			var cmd tea.Cmd
			m.palette.search, cmd = m.palette.search.Update(msg)
			if m.palette.search.Value() != prev {
				m.applyPaletteFilter(m.palette.search.Value())
			}
			return m, cmd
		}

	case paletteInput:
		switch msg.String() {
		case "esc":
			m.palette.phase = paletteBrowse
			m.palette.input.Blur()
			m.palette.search.Focus()
			return m, textinput.Blink
		case "enter":
			v := strings.TrimSpace(m.palette.input.Value())
			if m.paletteInputApply == nil {
				m.closePalette()
				return m, nil
			}
			msgText, err := m.paletteInputApply(m, v)
			m.closePalette()
			if err != nil {
				m.err = err
			} else {
				m.err = nil
				m.statusMsg = msgText
			}
			m.paletteInputApply = nil
			return m, tea.Batch(m.refreshAll(), scheduleToastClear())
		}
		var cmd tea.Cmd
		m.palette.input, cmd = m.palette.input.Update(msg)
		return m, cmd
	}
	return m, nil
}

func (m controlModel) renderPalette() string {
	w := m.width - 8
	if w < 40 {
		w = 40
	}

	var content string
	switch m.palette.phase {
	case paletteBrowse:
		search := m.palette.search.View()
		listView := m.palette.list.View()
		content = search + "\n" + listView
	case paletteInput:
		title := brandStyle.Render(m.paletteInputTitle)
		input := m.palette.input.View()
		help := mutedStyle.Render("enter save · esc back")
		content = title + "\n\n" + input + "\n" + help
	}

	box := lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(colorPrimary).
		Padding(0, 1).
		Width(w + 2).
		Render(content)

	scrim := lipgloss.NewStyle().
		Width(m.width).
		Height(m.height).
		Background(colorScrim).
		Render("")

	centered := lipgloss.Place(m.width, m.height, lipgloss.Center, lipgloss.Center, box)
	return scrim + "\n" + centered
}
