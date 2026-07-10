package cli

import (
	"strings"

	"charm.land/bubbles/v2/key"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"charm.land/lipgloss/v2/compat"
)

var (
	confirmBtnActive = lipgloss.NewStyle().
				Foreground(colorHighlight).
				Background(colorPrimary).
				Bold(true).
				Padding(0, 2).
				MarginRight(1)
	confirmBtnInactive = lipgloss.NewStyle().
				Foreground(colorSubtle).
				Background(compat.AdaptiveColor{Light: lipgloss.Color("#E5E7EB"), Dark: lipgloss.Color("#374151")}).
				Padding(0, 2).
				MarginRight(1)
)

func (m controlModel) confirmOpen() bool {
	return m.confirm != confirmNone
}

func (m *controlModel) openConfirmForm(kind confirmKind, targetID string) tea.Cmd {
	m.confirm = kind
	m.confirmTargetID = targetID
	m.confirmAffirm = false
	return nil
}

func (m *controlModel) cancelConfirm() (tea.Model, tea.Cmd) {
	m.confirm = confirmNone
	m.confirmTargetID = ""
	m.confirmAffirm = false
	m.notify("Cancelled", toastWarn)
	return m, nil
}

func confirmCopy(kind confirmKind, targetID string) (title, description, affirmative, negative string) {
	switch kind {
	case confirmEnableLive:
		title = "Enable LIVE remediation?"
		description = "T1/T2 actions will execute automatically.\nT3 actions still require your approval."
		affirmative = "Enable"
		negative = "Cancel"
	case confirmApproveRemediation:
		title = "Approve remediation?"
		description = "ID: " + targetID + "\nThis action will be executed against the cluster."
		affirmative = "Approve"
		negative = "Cancel"
	default:
		title = "Restart agent deployment?"
		description = "Rolling restart " + agentNS + "/" + agentSvc + "."
		affirmative = "Confirm"
		negative = "Cancel"
	}
	return title, description, affirmative, negative
}

func (m controlModel) renderConfirmOverlay() string {
	title, description, affirmative, negative := confirmCopy(m.confirm, m.confirmTargetID)

	var body strings.Builder
	body.WriteString(confirmTitleStyle.Render(title))
	body.WriteString("\n\n")
	for _, line := range strings.Split(description, "\n") {
		body.WriteString(subtleStyle.Render(line))
		body.WriteString("\n")
	}
	body.WriteString("\n")

	var affirmBtn, negBtn string
	if m.confirmAffirm {
		affirmBtn = confirmBtnActive.Render(affirmative)
		negBtn = confirmBtnInactive.Render(negative)
	} else {
		affirmBtn = confirmBtnInactive.Render(affirmative)
		negBtn = confirmBtnActive.Render(negative)
	}
	body.WriteString(lipgloss.JoinHorizontal(lipgloss.Left, affirmBtn, negBtn))
	body.WriteString("\n\n")
	body.WriteString(mutedStyle.Render("←/→ toggle   enter submit   y confirm   n cancel   esc dismiss"))

	boxW := min(58, m.width-8)
	dialog := confirmBoxStyle.Width(boxW).Render(strings.TrimRight(body.String(), "\n"))
	return lipgloss.Place(m.width, m.height, lipgloss.Center, lipgloss.Center, dialog)
}

func (m *controlModel) handleConfirmKey(msg tea.KeyPressMsg) (tea.Model, tea.Cmd) {
	toggle := key.NewBinding(key.WithKeys("left", "right", "h", "l"))
	submit := key.NewBinding(key.WithKeys("enter"))

	switch {
	case key.Matches(msg, m.keys.Cancel):
		return m.cancelConfirm()
	case key.Matches(msg, m.keys.Confirm):
		return m.executePendingConfirm()
	case key.Matches(msg, toggle):
		m.confirmAffirm = !m.confirmAffirm
		return m, nil
	case key.Matches(msg, submit):
		if m.confirmAffirm {
			return m.executePendingConfirm()
		}
		return m.cancelConfirm()
	}
	return m, nil
}
