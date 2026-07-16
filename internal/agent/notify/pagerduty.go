package notify

import (
	"context"
	"fmt"
	"time"
)

// PagerDuty API endpoint.
const pagerDutyEventsURL = "https://events.pagerduty.com/v2/enqueue"

// pagerDutyPayload is the request body for the PagerDuty Events API v2.
type pagerDutyPayload struct {
	RoutingKey  string           `json:"routing_key"`
	EventAction string           `json:"event_action"`
	DedupKey    string           `json:"dedup_key,omitempty"`
	Payload     pagerDutyEvent   `json:"payload"`
	Client      string           `json:"client,omitempty"`
	ClientURL   string           `json:"client_url,omitempty"`
	Links       []pagerDutyLink  `json:"links,omitempty"`
	Images      []pagerDutyImage `json:"images,omitempty"`
}

type pagerDutyEvent struct {
	Summary       string                 `json:"summary"`
	Source        string                 `json:"source"`
	Severity      string                 `json:"severity"`
	Timestamp     string                 `json:"timestamp,omitempty"`
	Component     string                 `json:"component,omitempty"`
	Group         string                 `json:"group,omitempty"`
	Class         string                 `json:"class,omitempty"`
	CustomDetails map[string]interface{} `json:"custom_details,omitempty"`
}

type pagerDutyLink struct {
	HRef string `json:"href"`
	Text string `json:"text"`
}

type pagerDutyImage struct {
	Src  string `json:"src"`
	Alt  string `json:"alt"`
	Href string `json:"href,omitempty"`
}

// pagerDutySeverity maps KubeWise severity to PagerDuty severity.
func pagerDutySeverity(s string) string {
	switch s {
	case "critical":
		return "critical"
	case "high":
		return "error"
	case "warning", "medium":
		return "warning"
	default:
		return "info"
	}
}

// buildPagerDutyPayload converts a KubeWise Event into a PagerDuty v2 payload.
func (n *Notifier) buildPagerDutyPayload(routingKey string, ev Event) pagerDutyPayload {
	action := "trigger"
	// Resolve events for completed remediations.
	if ev.Type == "remediation" {
		if sev := ev.Severity; sev == "info" || sev == "" {
			action = "resolve"
		}
	}

	dedupKey := ev.Entity
	if dedupKey == "" {
		dedupKey = ev.Type + "/" + ev.Title
	}

	return pagerDutyPayload{
		RoutingKey:  routingKey,
		EventAction: action,
		DedupKey:    dedupKey,
		Payload: pagerDutyEvent{
			Summary:   fmt.Sprintf("[%s] %s", ev.Severity, ev.Title),
			Source:    ev.Entity,
			Severity:  pagerDutySeverity(ev.Severity),
			Timestamp: ev.Timestamp.Format(time.RFC3339),
			Component: ev.Namespace,
			Group:     ev.Type,
			Class:     "kubewise",
			CustomDetails: map[string]interface{}{
				"message":     ev.Message,
				"entity":      ev.Entity,
				"namespace":   ev.Namespace,
				"severity":    ev.Severity,
				"type":        ev.Type,
				"title":       ev.Title,
				"details":     ev.Details,
				"kubewise_id": dedupKey,
			},
		},
		Client:    "KubeWise",
		ClientURL: "",
		Links: []pagerDutyLink{
			{HRef: "", Text: "KubeWise Event"},
		},
	}
}

// sendPagerDuty sends an event to the PagerDuty Events API v2.
func (n *Notifier) sendPagerDuty(ctx context.Context, routingKey string, ev Event) error {
	payload := n.buildPagerDutyPayload(routingKey, ev)
	if err := n.postJSON(ctx, pagerDutyEventsURL, payload); err != nil {
		return fmt.Errorf("pagerduty: %w", err)
	}
	return nil
}

// sendAlertmanager sends an event as an Alertmanager API v4 alert.
func (n *Notifier) sendAlertmanager(ctx context.Context, url string, ev Event) error {
	status := "firing"
	// Resolve events for info-severity or completed remediations.
	if ev.Severity == "info" || (ev.Type == "remediation" && ev.Severity == "") {
		status = "resolved"
	}

	labels := map[string]string{
		"alertname": "KubeWise_" + ev.Type,
		"severity":  ev.Severity,
		"entity":    ev.Entity,
		"namespace": ev.Namespace,
		"source":    "kubewise",
	}
	annotations := map[string]string{
		"summary":  ev.Title,
		"message":  ev.Message,
		"type":     ev.Type,
		"kubewise": "true",
	}

	payload := map[string]interface{}{
		"labels":       labels,
		"annotations":  annotations,
		"startsAt":     ev.Timestamp.Format(time.RFC3339),
		"status":       status,
		"generatorURL": "",
	}

	// Alertmanager API v4 accepts an array of alerts.
	body := []map[string]interface{}{payload}
	return n.postJSON(ctx, url, body)
}
