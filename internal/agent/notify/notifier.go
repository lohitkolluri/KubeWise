package notify

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// Notifier posts structured events to Slack or generic webhooks (Robusta/Botkube pattern).
type Notifier struct {
	cfg        models.NotificationsConfig
	httpClient *http.Client
}

// New creates a notifier from agent config. Returns nil when notifications are disabled
// and no output channel is configured.
func New(cfg models.NotificationsConfig) *Notifier {
	if !cfg.Enabled {
		return nil
	}
	if strings.TrimSpace(cfg.SlackWebhookURL) == "" &&
		strings.TrimSpace(cfg.WebhookURL) == "" &&
		strings.TrimSpace(cfg.PagerDutyRoutingKey) == "" &&
		strings.TrimSpace(cfg.AlertmanagerURL) == "" {
		return nil
	}
	return &Notifier{
		cfg: cfg,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// Event is a notification payload.
type Event struct {
	Type      string                 `json:"type"`
	Title     string                 `json:"title"`
	Message   string                 `json:"message"`
	Severity  string                 `json:"severity,omitempty"`
	Entity    string                 `json:"entity,omitempty"`
	Namespace string                 `json:"namespace,omitempty"`
	Details   map[string]interface{} `json:"details,omitempty"`
	Timestamp time.Time              `json:"timestamp"`
}

// NotifyPrediction sends a high-confidence prediction alert.
func (n *Notifier) NotifyPrediction(ctx context.Context, pred models.PredictionResult) {
	if n == nil || !n.cfg.OnPrediction {
		return
	}
	minScore := n.cfg.MinScore
	if minScore <= 0 {
		minScore = 0.7
	}
	if pred.Score < minScore && pred.Confidence < minScore {
		return
	}
	entity := pred.Entity
	if pred.Namespace != "" {
		entity = models.FormatEntity(pred.Namespace, pred.Entity)
	}
	eta := ""
	if pred.ETASeconds > 0 {
		eta = fmt.Sprintf(" (~%.0fs to failure)", pred.ETASeconds)
	}
	n.send(ctx, Event{
		Type:      "prediction",
		Title:     fmt.Sprintf("KubeWise prediction: %s", pred.MetricName),
		Message:   fmt.Sprintf("%s — %s confidence %.0f%%%s", entity, pred.Type, pred.Confidence*100, eta),
		Severity:  severityFromScore(pred.Score),
		Entity:    entity,
		Namespace: pred.Namespace,
		Details: map[string]interface{}{
			"metric":      pred.MetricName,
			"action":      pred.Action,
			"score":       pred.Score,
			"confidence":  pred.Confidence,
			"eta_seconds": pred.ETASeconds,
		},
		Timestamp: time.Now().UTC(),
	})
}

// NotifyRemediation sends audit/remediation lifecycle updates.
func (n *Notifier) NotifyRemediation(ctx context.Context, record models.AuditRecord) {
	if n == nil {
		return
	}
	switch record.Status {
	case models.AuditPending:
		if !n.cfg.OnApproval {
			return
		}
	case models.AuditDryRun, models.AuditVerified, models.AuditExecuted, models.AuditFailed, models.AuditVerifyFailed, models.AuditEscalated:
		if !n.cfg.OnRemediation {
			return
		}
	default:
		return
	}

	action := record.Plan.Action
	title := fmt.Sprintf("Remediation %s: %s", record.Status, action.Type)
	msg := fmt.Sprintf("%s/%s — %s (tier %s, confidence %.0f%%)",
		action.Namespace, action.Target, record.Plan.Diagnosis.RootCause,
		record.RiskTier, record.Plan.Diagnosis.Confidence*100)
	if record.Status == models.AuditDryRun {
		msg = "[dry-run] " + msg
	}
	if record.Reason != "" {
		msg += " — " + record.Reason
	}

	n.send(ctx, Event{
		Type:      "remediation",
		Title:     title,
		Message:   msg,
		Severity:  remediationSeverity(record),
		Entity:    models.FormatEntity(action.Namespace, action.Target),
		Namespace: action.Namespace,
		Details: map[string]interface{}{
			"status":     record.Status,
			"risk_tier":  record.RiskTier,
			"audit_id":   record.ID,
			"root_cause": record.Plan.Diagnosis.RootCause,
			"evidence":   record.Plan.Diagnosis.Evidence,
		},
		Timestamp: time.Now().UTC(),
	})
}

func (n *Notifier) send(ctx context.Context, ev Event) {
	if strings.TrimSpace(n.cfg.SlackWebhookURL) != "" {
		_ = n.postSlack(ctx, n.cfg.SlackWebhookURL, ev)
	}
	if strings.TrimSpace(n.cfg.WebhookURL) != "" && n.cfg.WebhookURL != n.cfg.SlackWebhookURL {
		_ = n.postJSON(ctx, n.cfg.WebhookURL, ev)
	}
	if strings.TrimSpace(n.cfg.PagerDutyRoutingKey) != "" {
		_ = n.sendPagerDuty(ctx, n.cfg.PagerDutyRoutingKey, ev)
	}
	if strings.TrimSpace(n.cfg.AlertmanagerURL) != "" {
		_ = n.sendAlertmanager(ctx, n.cfg.AlertmanagerURL, ev)
	}
}

func (n *Notifier) postSlack(ctx context.Context, url string, ev Event) error {
	color := "#36a64f"
	emoji := ":white_check_mark:"
	switch ev.Severity {
	case "critical", "high":
		color = "#e01e5a"
		emoji = ":red_circle:"
	case "warning", "medium":
		color = "#ecb22e"
		emoji = ":warning:"
	}

	blocks := slackBlocks(ev, emoji)

	payload := map[string]interface{}{
		"attachments": []map[string]interface{}{
			{
				"color":  color,
				"title":  ev.Title,
				"text":   ev.Message,
				"footer": "KubeWise",
				"ts":     ev.Timestamp.Unix(),
				"fields": slackFields(ev),
			},
		},
		"blocks": blocks,
	}
	return n.postJSON(ctx, url, payload)
}

// slackBlocks returns Slack Block Kit blocks for richer notifications.
func slackBlocks(ev Event, emoji string) []map[string]interface{} {
	blocks := []map[string]interface{}{
		{
			"type": "header",
			"text": map[string]interface{}{
				"type": "plain_text",
				"text": fmt.Sprintf("%s %s", emoji, ev.Title),
			},
		},
		{
			"type": "section",
			"text": map[string]interface{}{
				"type": "mrkdwn",
				"text": ev.Message,
			},
		},
	}

	fields := []map[string]interface{}{}
	if ev.Namespace != "" {
		fields = append(fields, map[string]interface{}{"type": "mrkdwn", "text": fmt.Sprintf("*Namespace:*\n%s", ev.Namespace)})
	}
	if ev.Entity != "" {
		fields = append(fields, map[string]interface{}{"type": "mrkdwn", "text": fmt.Sprintf("*Entity:*\n%s", ev.Entity)})
	}
	fields = append(fields, map[string]interface{}{"type": "mrkdwn", "text": fmt.Sprintf("*Type:*\n%s", ev.Type)})
	fields = append(fields, map[string]interface{}{"type": "mrkdwn", "text": fmt.Sprintf("*Severity:*\n%s", ev.Severity)})

	if len(fields) > 0 {
		blocks = append(blocks, map[string]interface{}{
			"type":   "section",
			"fields": fields,
		})
	}

	blocks = append(blocks, map[string]interface{}{
		"type": "context",
		"elements": []map[string]interface{}{
			{
				"type": "mrkdwn",
				"text": fmt.Sprintf("<!date^%d^KubeWise • {date_short} {time_secs}|KubeWise>", ev.Timestamp.Unix()),
			},
		},
	})

	return blocks
}

func slackFields(ev Event) []map[string]string {
	fields := []map[string]string{}
	if ev.Namespace != "" {
		fields = append(fields, map[string]string{"title": "Namespace", "value": ev.Namespace, "short": "true"})
	}
	if ev.Entity != "" {
		fields = append(fields, map[string]string{"title": "Entity", "value": ev.Entity, "short": "true"})
	}
	fields = append(fields, map[string]string{"title": "Type", "value": ev.Type, "short": "true"})
	return fields
}

func (n *Notifier) postJSON(ctx context.Context, url string, payload interface{}) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "KubeWise/1.0")

	resp, err := n.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode >= 400 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("webhook HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	return nil
}

func severityFromScore(score float64) string {
	switch {
	case score >= 0.9:
		return "critical"
	case score >= 0.75:
		return "high"
	case score >= 0.5:
		return "warning"
	default:
		return "info"
	}
}

func remediationSeverity(record models.AuditRecord) string {
	switch record.Status {
	case models.AuditFailed, models.AuditVerifyFailed:
		return "critical"
	case models.AuditPending:
		return "warning"
	case models.AuditDryRun:
		return "info"
	default:
		return "high"
	}
}
