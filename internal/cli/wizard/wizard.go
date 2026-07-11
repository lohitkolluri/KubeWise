package wizard

import (
	"fmt"
	"strings"

	"charm.land/bubbles/v2/textinput"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
)

// step represents the current wizard step.
type step int

const (
	stepWelcome step = iota
	stepDetect
	stepLLM
	stepObservability
	stepTools
	stepNotifications
	stepRemediate
	stepReview
	stepInstall
	stepDone
)

// State holds all collected configuration across steps.
type State struct {
	ClusterType     string
	OpenRouterKey   string
	OllamaURL       string
	APIToken        string // optional — auto-generated if empty
	EnableLoki      bool
	EnableTempo     bool
	EnableGrafana   bool
	GitHubToken     string
	ArgoCDServer    string
	SlackWebhookURL string
	PagerDutyKey    string
	RemediationMode string // dry-run, auto, off
	WatchNamespaces []string
	DryRun          bool
	Completed       bool
}

// Model is the bubbletea model for the wizard.
type Model struct {
	step   step
	state  State
	width  int
	height int
	cursor int // cursor position within a step

	// Text inputs (lazily initialized)
	apiTokenInput textinput.Model

	detecting bool // true while cluster detection is running
}

// stepNames for rendering step indicators.
var stepNames = []string{
	"Welcome",
	"Detect",
	"LLM",
	"Observability",
	"Tools",
	"Notifications",
	"Remediate",
	"Review",
	"Install",
}

// New creates a wizard model with default settings.
func New() Model {
	ti := textinput.New()
	ti.Prompt = "› "
	ti.Placeholder = "Auto-generated if empty"
	ti.CharLimit = 64
	ti.SetWidth(48)
	s := textinput.DefaultStyles(false)
	s.Focused.Prompt = lipgloss.NewStyle().Foreground(colorAccent).Bold(true)
	s.Focused.Text = lipgloss.NewStyle().Foreground(colorHighlight)
	ti.SetStyles(s)
	return Model{
		step:          stepWelcome,
		state:         State{RemediationMode: "dry-run"},
		apiTokenInput: ti,
	}
}

// State returns the wizard state for inspection or modification.
func (m *Model) State() *State { return &m.state }

// IsComplete returns true when the wizard finished successfully.
func (m Model) IsComplete() bool { return m.state.Completed }

// Init returns the initial command for the bubbletea model.
func (m Model) Init() tea.Cmd { return nil }

// Update handles bubbletea messages and advances the wizard state.
func (m Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		return m, nil
	case tea.KeyPressMsg:
		if msg.String() == "ctrl+c" {
			m.state.Completed = false
			m.step = stepDone
			return m, tea.Quit
		}
	}

	var cmd tea.Cmd
	switch m.step {
	case stepWelcome:
		return m.updateWelcome(msg)
	case stepDetect:
		// Auto-start detection on first entry.
		if !m.detecting && m.state.ClusterType == "" {
			m.detecting = true
			return m, detectClusterCmd()
		}
		return m.updateDetect(msg)
	case stepLLM:
		return m.updateLLM(msg)
	case stepObservability:
		return m.updateObservability(msg)
	case stepTools:
		return m.updateTools(msg)
	case stepNotifications:
		return m.updateNotifications(msg)
	case stepRemediate:
		return m.updateRemediate(msg)
	case stepReview:
		return m.updateReview(msg)
	case stepInstall:
		return m.updateInstall(msg)
	case stepDone:
		return m, tea.Quit
	}
	return m, cmd
}

// View renders the current wizard step as a bubbletea view.
func (m Model) View() tea.View {
	if m.width == 0 {
		m.width = 80
	}

	var body string
	switch m.step {
	case stepWelcome:
		body = m.viewWelcome()
	case stepDetect:
		body = m.viewDetect()
	case stepLLM:
		body = m.viewLLM()
	case stepObservability:
		body = m.viewObservability()
	case stepTools:
		body = m.viewTools()
	case stepNotifications:
		body = m.viewNotifications()
	case stepRemediate:
		body = m.viewRemediate()
	case stepReview:
		body = m.viewReview()
	case stepInstall:
		body = m.viewInstall()
	case stepDone:
		body = m.viewDone()
	}

	progress := m.viewProgress()
	return tea.NewView(appStyle.Render(lipgloss.JoinVertical(lipgloss.Top, progress, body)))
}

// ── Progress bar ──

func (m Model) viewProgress() string {
	var b strings.Builder
	b.WriteString(titleStyle.Render(" KubeWise Setup "))
	b.WriteString("\n\n")

	for i, name := range stepNames {
		s := step(i)
		switch {
		case s < m.step:
			b.WriteString(progressDoneStyle.String())
			b.WriteString(" " + mutedStyle(name))
		case s == m.step:
			b.WriteString(progressCurrentStyle.String())
			b.WriteString(" " + activeStepStyle.Render(name))
		default:
			b.WriteString(progressPendingStyle.String())
			b.WriteString(" " + mutedStyle(name))
		}
		if i < len(stepNames)-1 {
			b.WriteString("  ")
		}
	}
	b.WriteString("\n")
	return b.String()
}

func mutedStyle(s string) string { return lipgloss.NewStyle().Foreground(colorMuted).Render(s) }

// ── Step: Welcome ──

func (m Model) updateWelcome(msg tea.Msg) (tea.Model, tea.Cmd) {
	if msg, ok := msg.(tea.KeyPressMsg); ok && msg.String() == "enter" {
		m.step = stepDetect
	}
	return m, nil
}

func (m Model) viewWelcome() string {
	return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top,
		titleStyle.Render("🚀 Welcome to KubeWise"),
		"",
		infoStyle.Render("This wizard will help you install and configure KubeWise"),
		infoStyle.Render("in your Kubernetes cluster."),
		"",
		infoStyle.Render("You'll configure:"),
		"  • LLM provider (OpenRouter or Ollama)",
		"  • Observability stack (Loki, Tempo, Grafana)",
		"  • Tool integrations (GitHub, ArgoCD)",
		"  • Notifications (Slack, PagerDuty)",
		"  • Remediation mode and policies",
		"",
		buttonStyle.Render("Press Enter to begin ▶"),
	))
}

// ── Step: Detect ──

func (m Model) updateDetect(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case detectResult:
		m.detecting = false
		if msg.Error != nil {
			m.state.ClusterType = "error: " + msg.Error.Error()
		} else {
			m.state.ClusterType = msg.ClusterType
		}
		return m, nil
	case tea.KeyPressMsg:
		if msg.String() == "enter" {
			m.step = stepLLM
		}
	}
	return m, nil
}

func (m Model) viewDetect() string {
	lines := []string{
		stepStyle.Render("Step 2/9: Environment Detection"),
		"",
	}
	if m.detecting {
		lines = append(lines,
			spinnerStyle.Render("⏳ Detecting Kubernetes environment..."),
			"",
			mutedStyle("Running kubectl probes..."),
		)
	} else if m.state.ClusterType != "" {
		isErr := strings.HasPrefix(m.state.ClusterType, "error:")
		lines = append(lines,
			inputValueStyle.Render("Cluster:"),
			"  "+m.state.ClusterType,
			"",
		)
		if isErr {
			lines = append(lines,
				warnStyle.Render("⚠ Unable to fully detect environment"),
				"",
				helpStyle.Render("Make sure kubectl is connected to a cluster."),
			)
		}
		lines = append(lines,
			buttonStyle.Render("Press Enter to continue ▶"),
		)
	}
	return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top, lines...))
}

// ── Step: LLM ──

func (m Model) updateLLM(msg tea.Msg) (tea.Model, tea.Cmd) {
	// Enter saves the token and advances.
	if msg, ok := msg.(tea.KeyPressMsg); ok && msg.String() == "enter" {
		m.state.APIToken = m.apiTokenInput.Value()
		m.step = stepObservability
		return m, nil
	}
	// All other key events go to the text input.
	var cmd tea.Cmd
	m.apiTokenInput, cmd = m.apiTokenInput.Update(msg)
	return m, cmd
}

func (m Model) viewLLM() string {
	maskedKey := m.state.OpenRouterKey
	if len(maskedKey) > 8 {
		maskedKey = maskedKey[:4] + "···" + maskedKey[len(maskedKey)-4:]
	} else if maskedKey != "" {
		maskedKey = "··set··"
	}
	ollama := m.state.OllamaURL
	if ollama == "" {
		ollama = "http://localhost:11434 (default)"
	}

	orStatus := mutedStyle("not set (observe-only mode)")
	if m.state.OpenRouterKey != "" {
		orStatus = successStyle.Render("✓ configured")
	}

	return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top,
		stepStyle.Render("Step 3/9: LLM Provider & Security"),
		"",
		lipgloss.JoinHorizontal(lipgloss.Top,
			lipgloss.NewStyle().Width(22).Foreground(colorMuted).Render("OpenRouter API key"),
			inputValueStyle.Render(maskedKey),
			infoStyle.Render(" "+orStatus),
		),
		mutedStyle("  Set via OPENROUTER_API_KEY env var"),
		"",
		lipgloss.JoinHorizontal(lipgloss.Top,
			lipgloss.NewStyle().Width(22).Foreground(colorMuted).Render("Ollama URL"),
			inputValueStyle.Render(ollama),
		),
		"",
		lipgloss.NewStyle().Foreground(colorMuted).Render("API Token")+mutedStyle("  (for secure agent API access)"),
		m.apiTokenInput.View(),
		mutedStyle("  Leave empty to auto-generate"),
		"",
		buttonStyle.Render("Press Enter to continue ▶"),
	))
}

// ── Step: Observability ──

func (m Model) updateObservability(msg tea.Msg) (tea.Model, tea.Cmd) {
	if msg, ok := msg.(tea.KeyPressMsg); ok {
		switch msg.String() {
		case "enter":
			m.step = stepTools
		case "up":
			if m.cursor > 0 {
				m.cursor--
			}
		case "down":
			if m.cursor < 2 {
				m.cursor++
			}
		case "space":
			switch m.cursor {
			case 0:
				m.state.EnableLoki = !m.state.EnableLoki
			case 1:
				m.state.EnableTempo = !m.state.EnableTempo
			case 2:
				m.state.EnableGrafana = !m.state.EnableGrafana
			}
		}
	}
	return m, nil
}

func (m Model) viewObservability() string {
	toggles := []struct {
		label string
		on    bool
	}{
		{"Loki (logs)", m.state.EnableLoki},
		{"Tempo (traces)", m.state.EnableTempo},
		{"Grafana (dashboards)", m.state.EnableGrafana},
	}

	lines := make([]string, 0, 8)
	lines = append(lines, stepStyle.Render("Step 4/9: Observability Stack"))
	lines = append(lines, "")
	for i, t := range toggles {
		cursor := "  "
		if m.cursor == i {
			cursor = "▸"
		}
		check := "☐"
		if t.on {
			check = "☑"
		}
		lines = append(lines, fmt.Sprintf("%s %s %s", cursor, check, t.label))
	}
	lines = append(lines, "")
	lines = append(lines, buttonStyle.Render("Press Enter to continue ▶"))
	lines = append(lines, helpStyle.Render("Space to toggle, ↑/↓ to navigate"))

	return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top, lines...))
}

// ── Step: Tools ──

func (m Model) updateTools(msg tea.Msg) (tea.Model, tea.Cmd) {
	if msg, ok := msg.(tea.KeyPressMsg); ok && msg.String() == "enter" {
		m.step = stepNotifications
	}
	return m, nil
}

func (m Model) viewTools() string {
	gh := m.state.GitHubToken
	if gh != "" {
		gh = "✓ configured"
	} else {
		gh = "not set"
	}
	argo := m.state.ArgoCDServer
	if argo == "" {
		argo = "not set"
	}

	return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top,
		stepStyle.Render("Step 5/9: Tool Integrations"),
		"",
		fmt.Sprintf("  GitHub token: %s", inputValueStyle.Render(gh)),
		fmt.Sprintf("  ArgoCD server: %s", inputValueStyle.Render(argo)),
		"",
		infoStyle.Render("Set GITHUB_TOKEN env var and/or configure ArgoCD after install."),
		"",
		buttonStyle.Render("Press Enter to continue ▶"),
	))
}

// ── Step: Notifications ──

func (m Model) updateNotifications(msg tea.Msg) (tea.Model, tea.Cmd) {
	if msg, ok := msg.(tea.KeyPressMsg); ok && msg.String() == "enter" {
		m.step = stepRemediate
	}
	return m, nil
}

func (m Model) viewNotifications() string {
	slack := m.state.SlackWebhookURL
	if slack != "" {
		slack = "✓ configured"
	} else {
		slack = "not set"
	}
	pd := m.state.PagerDutyKey
	if pd != "" {
		pd = "✓ configured"
	} else {
		pd = "not set"
	}

	return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top,
		stepStyle.Render("Step 6/9: Notifications"),
		"",
		fmt.Sprintf("  Slack webhook: %s", inputValueStyle.Render(slack)),
		fmt.Sprintf("  PagerDuty key: %s", inputValueStyle.Render(pd)),
		"",
		infoStyle.Render("Configure after install via config file or environment variables."),
		"",
		buttonStyle.Render("Press Enter to continue ▶"),
	))
}

// ── Step: Remediate ──

func (m Model) updateRemediate(msg tea.Msg) (tea.Model, tea.Cmd) {
	if msg, ok := msg.(tea.KeyPressMsg); ok {
		switch msg.String() {
		case "enter":
			m.step = stepReview
		case "up":
			if m.cursor > 0 {
				m.cursor--
			}
		case "down":
			if m.cursor < 2 {
				m.cursor++
			}
		case "space":
			modes := []string{"off", "dry-run", "auto"}
			if m.cursor < len(modes) {
				m.state.RemediationMode = modes[m.cursor]
			}
		}
	}
	return m, nil
}

func (m Model) viewRemediate() string {
	modes := []struct {
		label string
		desc  string
	}{
		{"off", "Observe only — no automatic actions"},
		{"dry-run", "Plan but don't execute (safe default)"},
		{"auto", "Automatic remediation for T1/T2 risks"},
	}

	lines := make([]string, 0, 7)
	lines = append(lines, stepStyle.Render("Step 7/9: Remediation Mode"))
	lines = append(lines, "")
	for i, mode := range modes {
		cursor := "  "
		if m.cursor == i {
			cursor = "▸"
		}
		selected := "○"
		if m.state.RemediationMode == []string{"off", "dry-run", "auto"}[i] {
			selected = "●"
		}
		lines = append(lines, fmt.Sprintf("%s %s %s — %s", cursor, selected, mode.label, mutedStyle(mode.desc)))
	}
	lines = append(lines, "")
	lines = append(lines, buttonStyle.Render("Press Enter to continue ▶"))

	return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top, lines...))
}

// ── Step: Review ──

func (m Model) updateReview(msg tea.Msg) (tea.Model, tea.Cmd) {
	if msg, ok := msg.(tea.KeyPressMsg); ok {
		switch msg.String() {
		case "enter":
			m.step = stepInstall
		case "esc", "backspace":
			if m.step > stepWelcome {
				m.step--
			}
		}
	}
	return m, nil
}

func (m Model) viewReview() string {
	// Collect and show validation warnings
	warnings := m.state.Validate()
	warnLines := make([]string, 0, len(warnings))
	for _, w := range warnings {
		warnLines = append(warnLines, warnStyle.Render("⚠ "+w))
	}

	var maskedToken string
	if len(m.state.APIToken) > 8 {
		maskedToken = m.state.APIToken[:4] + "···" + m.state.APIToken[len(m.state.APIToken)-4:]
	} else if m.state.APIToken != "" {
		maskedToken = "··set··" //nolint:gosec // display mask, not a credential
	}

	lines := []string{
		stepStyle.Render("Step 8/9: Review Configuration"),
		"",
		summarySectionStyle.Render("LLM Provider & Security"),
		fmt.Sprintf("  %s %s", summaryKeyStyle.Render("OpenRouter:"), m.summaryBool(m.state.OpenRouterKey != "")),
		fmt.Sprintf("  %s %s", summaryKeyStyle.Render("Ollama:"), m.state.OllamaURL),
		fmt.Sprintf("  %s %s", summaryKeyStyle.Render("API Token:"), inputValueStyle.Render(maskedToken)),
		"",
		summarySectionStyle.Render("Observability"),
		fmt.Sprintf("  %s %s", summaryKeyStyle.Render("Loki:"), m.summaryBool(m.state.EnableLoki)),
		fmt.Sprintf("  %s %s", summaryKeyStyle.Render("Tempo:"), m.summaryBool(m.state.EnableTempo)),
		fmt.Sprintf("  %s %s", summaryKeyStyle.Render("Grafana:"), m.summaryBool(m.state.EnableGrafana)),
		"",
		summarySectionStyle.Render("Tools & Notifications"),
		fmt.Sprintf("  %s %s", summaryKeyStyle.Render("GitHub:"), m.summaryBool(m.state.GitHubToken != "")),
		fmt.Sprintf("  %s %s", summaryKeyStyle.Render("ArgoCD:"), m.summaryBool(m.state.ArgoCDServer != "")),
		fmt.Sprintf("  %s %s", summaryKeyStyle.Render("Slack:"), m.summaryBool(m.state.SlackWebhookURL != "")),
		fmt.Sprintf("  %s %s", summaryKeyStyle.Render("PagerDuty:"), m.summaryBool(m.state.PagerDutyKey != "")),
		"",
		summarySectionStyle.Render("Remediation"),
		fmt.Sprintf("  %s %s", summaryKeyStyle.Render("Mode:"), m.state.RemediationMode),
	}
	if len(warnings) > 0 {
		lines = append(lines, "",
			warnStyle.Render("⚠ Warnings"))
		for _, wl := range warnLines {
			lines = append(lines, mutedStyle("  "+wl))
		}
	}
	lines = append(lines, "",
		successStyle.Render("✓ Press Enter to install")+"  "+
			mutedStyle("Esc: go back to edit"))

	return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top, lines...))
}

func (m Model) summaryBool(v bool) string {
	if v {
		return successStyle.Render("✓ enabled")
	}
	return mutedStyle("—")
}

// ── Step: Install ──

func (m Model) updateInstall(msg tea.Msg) (tea.Model, tea.Cmd) {
	if msg, ok := msg.(tea.KeyPressMsg); ok && msg.String() == "enter" {
		m.step = stepDone
		m.state.Completed = true
		return m, tea.Quit
	}
	return m, nil
}

func (m Model) viewInstall() string {
	return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top,
		stepStyle.Render("Step 9/9: Install"),
		"",
		infoStyle.Render("Installing KubeWise with your configuration..."),
		"",
		spinnerStyle.Render("⏳ Installing Helm chart..."),
		"",
		infoStyle.Render("This will be implemented in the executor sub-phase."),
		"",
		buttonStyle.Render("Press Enter to finish ▶"),
	))
}

// ── Step: Done ──

func (m Model) viewDone() string {
	if m.state.Completed {
		return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top,
			successStyle.Render("✓ KubeWise installation complete!"),
			"",
			infoStyle.Render("Connect to the agent:"),
			"  kwctl up",
			"  kwctl ui",
			"",
			buttonStyle.Render("Press Enter to exit"),
		))
	}
	return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top,
		errStyle.Render("✗ Installation cancelled."),
		"",
		buttonStyle.Render("Press Enter to exit"),
	))
}
