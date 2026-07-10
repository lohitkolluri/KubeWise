package cli

import (
	"fmt"
	"sort"
	"strings"

	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

var (
	detailSubheadStyle  = lipgloss.NewStyle().Foreground(colorMuted).Bold(true)
	detailStepNumStyle  = lipgloss.NewStyle().Foreground(colorAccent).Bold(true)
	detailStepTypeStyle = lipgloss.NewStyle().Foreground(colorHighlight).Bold(true)
	detailChipStyle     = lipgloss.NewStyle().Foreground(colorInfo)
)

func buildAuditDetailBody(b *strings.Builder, width int, r models.AuditRecord, isApproval bool) {
	plan := r.Plan
	writePlanDiagnosis(b, width, plan.Diagnosis)
	writePlanAction(b, width, plan.Action, len(plan.Steps) == 0)
	writePlanSteps(b, width, plan.Steps)
	writePlanRisk(b, width, plan.Risk)
	writePlanVerification(b, width, plan.Verification)

	if plan.Investigation.Summary != "" {
		writeDetailSectionProse(b, width, "Investigation", plan.Investigation.Summary)
	}

	if !isApproval {
		if r.Prompt != "" {
			writeDetailSectionProse(b, width, "LLM Prompt", r.Prompt)
		}
		if r.LLMResponse != "" {
			writeDetailSectionProse(b, width, "LLM Response", r.LLMResponse)
		}
		if r.K8sResult != "" {
			writeDetailSectionProse(b, width, "K8s Result", r.K8sResult)
		}
		if r.VerificationNote != "" {
			writeDetailSectionProse(b, width, "Verification Note", r.VerificationNote)
		}
	}
}

func writePlanDiagnosis(b *strings.Builder, width int, d models.Diagnosis) {
	writeDetailSection(b, "Diagnosis")
	if strings.TrimSpace(d.RootCause) != "" {
		writeDetailKV(b, width, "Root cause:", d.RootCause)
	}
	if strings.TrimSpace(d.Severity) != "" {
		writeDetailKVStyled(b, width, "Severity:", severityDetailStyle(d.Severity), humanizeSeverity(d.Severity))
	}
	if d.Confidence > 0 {
		writeDetailKVStyled(b, width, "Confidence:", scoreStyle(d.Confidence), fmt.Sprintf("%.0f%%", d.Confidence*100))
	}
	writeDetailBulletBlock(b, width, "Evidence", d.Evidence)
}

func writePlanAction(b *strings.Builder, width int, act models.Action, showParams bool) {
	if act.Type == "" && act.Target == "" {
		return
	}
	writeDetailSection(b, "Action")
	if act.Type != "" {
		writeDetailKVStyled(b, width, "Type:", detailStepTypeStyle, humanizeActionType(act.Type))
	}
	if act.Namespace != "" || act.Target != "" {
		writeDetailKV(b, width, "Target:", formatWorkloadTarget(act.Namespace, act.Target))
	}
	if strings.TrimSpace(act.Rationale) != "" {
		writeDetailKV(b, width, "Why:", act.Rationale)
	}
	if showParams && len(act.Parameters) > 0 {
		writeDetailKV(b, width, "Changes:", formatParamChips(act.Parameters))
	}
}

func writePlanSteps(b *strings.Builder, width int, steps []models.RunbookStep) {
	if len(steps) == 0 {
		return
	}
	writeDetailSection(b, "Runbook")
	sorted := append([]models.RunbookStep(nil), steps...)
	sort.Slice(sorted, func(i, j int) bool {
		if sorted[i].Order == sorted[j].Order {
			return i < j
		}
		return sorted[i].Order < sorted[j].Order
	})
	for _, s := range sorted {
		writeRunbookStep(b, width, s)
	}
}

func writeRunbookStep(b *strings.Builder, width int, s models.RunbookStep) {
	order := s.Order
	if order <= 0 {
		order = 1
	}
	head := fmt.Sprintf("%s %s", detailStepNumStyle.Render(fmt.Sprintf("%d.", order)), detailStepTypeStyle.Render(humanizeActionType(s.Type)))
	if s.WaitSeconds > 0 && strings.EqualFold(s.Type, "wait") {
		head = fmt.Sprintf("%s %s", detailStepNumStyle.Render(fmt.Sprintf("%d.", order)), detailStepTypeStyle.Render(fmt.Sprintf("Wait %ds", s.WaitSeconds)))
	}
	b.WriteString(head)
	b.WriteByte('\n')

	if s.Namespace != "" || s.Target != "" {
		writeDetailIndentedKV(b, width, "Target", formatWorkloadTarget(s.Namespace, s.Target))
	}
	if strings.TrimSpace(s.Rationale) != "" {
		writeDetailIndentedKV(b, width, "Why", s.Rationale)
	}
	if len(s.Parameters) > 0 {
		writeDetailIndentedKV(b, width, "Set", formatParamChips(s.Parameters))
	}
	if s.WaitSeconds > 0 && !strings.EqualFold(s.Type, "wait") {
		writeDetailIndentedKV(b, width, "Wait", fmt.Sprintf("%ds before next step", s.WaitSeconds))
	}
	b.WriteByte('\n')
}

func writePlanRisk(b *strings.Builder, width int, risk models.Risk) {
	writeDetailSection(b, "Risk")
	if risk.BlastRadius != "" {
		writeDetailKVStyled(b, width, "Blast radius:", riskTierStyle(risk.BlastRadius), humanizeBlastRadius(risk.BlastRadius))
	}
	b.WriteString(formatDetailKVRendered(width, "Reversible:", severitySymbol(risk.Reversible)))
	b.WriteByte('\n')
	if risk.EstimatedTimeToResolve != "" {
		writeDetailKV(b, width, "Est. resolve:", risk.EstimatedTimeToResolve)
	}
}

func writePlanVerification(b *strings.Builder, width int, plan models.VerificationPlan) {
	if len(plan.Checks) == 0 && plan.WaitSeconds <= 0 {
		return
	}
	writeDetailSection(b, "Verification")
	for _, c := range plan.Checks {
		check := humanizeCheckType(c.Type)
		target := formatWorkloadTarget(c.Namespace, c.Target)
		writeDetailBullet(b, width, fmt.Sprintf("%s on %s", check, target))
	}
	if plan.WaitSeconds > 0 {
		writeDetailBullet(b, width, fmt.Sprintf("wait %ds after changes before checking", plan.WaitSeconds))
	}
}

func writeDetailBulletBlock(b *strings.Builder, width int, title string, items []string) {
	var cleaned []string
	for _, item := range items {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		cleaned = append(cleaned, item)
	}
	if len(cleaned) == 0 {
		return
	}
	b.WriteString(detailLabelStyle.Render(title + ":"))
	b.WriteByte('\n')
	for _, item := range cleaned {
		writeDetailBullet(b, width, item)
	}
}

func writeDetailBullet(b *strings.Builder, width int, text string) {
	text = strings.TrimSpace(text)
	if text == "" {
		return
	}
	bullet := "  " + mutedStyle.Render("•") + " "
	bulletW := lipgloss.Width(bullet)
	avail := width - bulletW
	if avail < 12 {
		avail = 12
	}
	wrapped := ansi.Wrap(subtleStyle.Render(text), avail, "")
	for i, line := range strings.Split(wrapped, "\n") {
		if i == 0 {
			b.WriteString(bullet + line)
		} else {
			b.WriteString(strings.Repeat(" ", bulletW) + line)
		}
		b.WriteByte('\n')
	}
}

func writeDetailIndentedKV(b *strings.Builder, width int, label, value string) {
	indent := "  "
	labelText := indent + detailSubheadStyle.Render(label+":")
	labelW := lipgloss.Width(labelText)
	gutter := strings.Repeat(" ", labelW+1)
	avail := width - labelW - 1
	if avail < 12 {
		avail = 12
	}
	rendered := formatDetailKVRendered(avail, "", detailValueStyle.Render(value))
	for i, line := range strings.Split(rendered, "\n") {
		if i == 0 {
			b.WriteString(lipgloss.JoinHorizontal(lipgloss.Top, labelText, line))
		} else {
			b.WriteString(gutter + line)
		}
		b.WriteByte('\n')
	}
}

func formatParamChips(params map[string]string) string {
	if len(params) == 0 {
		return ""
	}
	keys := sortedParamKeys(params)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, detailChipStyle.Render(humanizeParamKey(k))+"="+detailValueStyle.Render(params[k]))
	}
	return strings.Join(parts, mutedStyle.Render("  "))
}

func sortedParamKeys(params map[string]string) []string {
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func formatWorkloadTarget(namespace, target string) string {
	target = strings.TrimSpace(target)
	namespace = strings.TrimSpace(namespace)
	switch {
	case namespace != "" && target != "":
		return namespace + "/" + target
	case target != "":
		return target
	default:
		return namespace
	}
}

func humanizeActionType(action string) string {
	switch strings.ToLower(strings.TrimSpace(action)) {
	case "patch_resources":
		return "Patch resources"
	case "restart_pod":
		return "Restart pod"
	case "delete_pod":
		return "Delete pod"
	case "scale_replicas":
		return "Scale replicas"
	case "rollback_deployment":
		return "Rollback deployment"
	case "view_logs":
		return "View logs"
	case "escalate":
		return "Escalate"
	case "noop":
		return "No-op"
	case "wait":
		return "Wait"
	default:
		return strings.ReplaceAll(action, "_", " ")
	}
}

func humanizeCheckType(check string) string {
	switch strings.ToLower(strings.TrimSpace(check)) {
	case "pod_ready":
		return "Pod ready"
	case "no_crashloop":
		return "No crash loop"
	case "deployment_available":
		return "Deployment available"
	default:
		return strings.ReplaceAll(check, "_", " ")
	}
}

func humanizeBlastRadius(radius string) string {
	switch strings.ToLower(strings.TrimSpace(radius)) {
	case "single_pod":
		return "Single pod"
	case "multiple_pods":
		return "Multiple pods"
	case "service":
		return "Service"
	case "cluster":
		return "Cluster"
	default:
		return strings.ReplaceAll(radius, "_", " ")
	}
}

func humanizeSeverity(severity string) string {
	s := strings.TrimSpace(severity)
	if s == "" {
		return s
	}
	return strings.ToUpper(s[:1]) + strings.ToLower(s[1:])
}

func humanizeParamKey(key string) string {
	key = strings.TrimSpace(key)
	if key == "" {
		return key
	}
	return strings.ReplaceAll(key, "_", " ")
}

func severityDetailStyle(severity string) lipgloss.Style {
	switch strings.ToLower(strings.TrimSpace(severity)) {
	case "critical":
		return errStyle
	case "high":
		return listRowCriticalStyle
	case "warning", "medium":
		return warnStyle
	case "low", "info":
		return infoStyle
	default:
		return mutedStyle
	}
}
