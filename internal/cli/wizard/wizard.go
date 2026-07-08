package wizard

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
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

// WizardState holds all collected configuration across steps.
type WizardState struct {
	ClusterType     string
	OpenRouterKey   string
	OllamaURL       string
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
	state  WizardState
	width  int
	height int
	cursor int // cursor position within a step
	err    error
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

func New() Model {
	return Model{
		step:  stepWelcome,
		state: WizardState{RemediationMode: "dry-run"},
	}
}

// State returns the wizard state for inspection or modification.
func (m *Model) State() *WizardState { return &m.state }

// IsComplete returns true when the wizard finished successfully.
func (m Model) IsComplete() bool { return m.state.Completed }

func (m Model) Init() tea.Cmd { return nil }

func (m Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		return m, nil
	case tea.KeyMsg:
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

func (m Model) View() string {
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
	return appStyle.Render(lipgloss.JoinVertical(lipgloss.Top, progress, body))
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
	if msg, ok := msg.(tea.KeyMsg); ok {
		switch msg.String() {
		case "enter":
			m.step = stepDetect
		}
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
	if msg, ok := msg.(tea.KeyMsg); ok {
		switch msg.String() {
		case "enter":
			m.step = stepLLM
		}
	}
	return m, nil
}

func (m Model) viewDetect() string {
	cluster := m.state.ClusterType
	if cluster == "" {
		cluster = "detecting..."
	}
	return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top,
		stepStyle.Render("Step 2/9: Environment Detection"),
		"",
		infoStyle.Render("Detecting your Kubernetes environment..."),
		"",
		fmt.Sprintf("Cluster type: %s", cluster),
		"",
		buttonStyle.Render("Press Enter to continue ▶"),
		helpStyle.Render("Auto-detection will be implemented in a later sub-phase."),
	))
}

// ── Step: LLM ──

func (m Model) updateLLM(msg tea.Msg) (tea.Model, tea.Cmd) {
	if msg, ok := msg.(tea.KeyMsg); ok {
		switch msg.String() {
		case "enter":
			m.step = stepObservability
		case "up":
			if m.cursor > 0 {
				m.cursor--
			}
		case "down":
			if m.cursor < 1 {
				m.cursor++
			}
		case " ":
			// Toggle on/off for Ollama option.
		}
	}
	return m, nil
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

	cursor := " "
	if m.cursor == 0 {
		cursor = "▸"
	}

	return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top,
		stepStyle.Render("Step 3/9: LLM Provider"),
		"",
		fmt.Sprintf("%s OpenRouter API key: %s", cursor, inputValueStyle.Render(maskedKey)),
		infoStyle.Render("  Set via OPENROUTER_API_KEY env var or enter below"),
		"",
		fmt.Sprintf("  Ollama URL: %s", inputValueStyle.Render(ollama)),
		"",
		successStyle.Render("✓ OpenRouter configured") + "  " + mutedStyle("(no key = observe-only mode)"),
		"",
		buttonStyle.Render("Press Enter to continue ▶"),
	))
}

// ── Step: Observability ──

func (m Model) updateObservability(msg tea.Msg) (tea.Model, tea.Cmd) {
	if msg, ok := msg.(tea.KeyMsg); ok {
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
		case " ":
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

	var lines []string
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
	if msg, ok := msg.(tea.KeyMsg); ok {
		switch msg.String() {
		case "enter":
			m.step = stepNotifications
		}
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
	if msg, ok := msg.(tea.KeyMsg); ok {
		switch msg.String() {
		case "enter":
			m.step = stepRemediate
		}
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
	if msg, ok := msg.(tea.KeyMsg); ok {
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
		case " ":
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

	var lines []string
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
	if msg, ok := msg.(tea.KeyMsg); ok {
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
	return boxStyle.Render(lipgloss.JoinVertical(lipgloss.Top,
		stepStyle.Render("Step 8/9: Review Configuration"),
		"",
		summarySectionStyle.Render("LLM Provider"),
		fmt.Sprintf("  %s OpenRouter: %s", summaryKeyStyle.Render("Key:"), m.summaryBool(m.state.OpenRouterKey != "")),
		fmt.Sprintf("  %s %s", summaryKeyStyle.Render("Ollama:"), m.state.OllamaURL),
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
		"",
		successStyle.Render("✓ Press Enter to install")+"  "+
			mutedStyle("Esc: go back to edit"),
	))
}

func (m Model) summaryBool(v bool) string {
	if v {
		return successStyle.Render("✓ enabled")
	}
	return mutedStyle("—")
}

// ── Step: Install ──

func (m Model) updateInstall(msg tea.Msg) (tea.Model, tea.Cmd) {
	if msg, ok := msg.(tea.KeyMsg); ok {
		switch msg.String() {
		case "enter":
			m.step = stepDone
			m.state.Completed = true
			return m, tea.Quit
		}
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
