package notify

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func TestNew_Disabled(t *testing.T) {
	if n := New(models.NotificationsConfig{}); n != nil {
		t.Fatal("expected nil when disabled")
	}
}

func TestNotifyPrediction_Slack(t *testing.T) {
	var mu sync.Mutex
	var payload map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		_ = json.Unmarshal(body, &payload)
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	n := New(models.NotificationsConfig{
		Enabled:         true,
		SlackWebhookURL: srv.URL,
		OnPrediction:    true,
		MinScore:        0.5,
	})
	n.NotifyPrediction(context.Background(), models.PredictionResult{
		Type:       "pattern",
		Entity:     "demo",
		Namespace:  "demo",
		MetricName: "oom_predicted",
		Score:      0.9,
		Confidence: 0.85,
		ETASeconds: 120,
		Timestamp:  time.Now(),
	})

	mu.Lock()
	defer mu.Unlock()
	if payload == nil {
		t.Fatal("expected slack payload")
	}
}

func TestNotifyRemediation_Pending(t *testing.T) {
	var called bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		called = true
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	n := New(models.NotificationsConfig{
		Enabled:       true,
		WebhookURL:    srv.URL,
		OnApproval:    true,
		OnRemediation: true,
	})
	n.NotifyRemediation(context.Background(), models.AuditRecord{
		ID:       "audit-1",
		Status:   models.AuditPending,
		RiskTier: models.RiskTier3,
		Plan: models.RemediationPlan{
			Diagnosis: models.Diagnosis{RootCause: "OOM", Confidence: 0.9},
			Action:    models.Action{Type: "patch_resources", Namespace: "demo", Target: "api"},
		},
		CreatedAt: time.Now(),
	})
	if !called {
		t.Fatal("expected webhook call for pending approval")
	}
}

func TestNotifyRemediation_DryRunSkippedWhenDisabled(t *testing.T) {
	called := false
	srv := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, _ *http.Request) {
		called = true
	}))
	defer srv.Close()

	n := New(models.NotificationsConfig{
		Enabled:       true,
		WebhookURL:    srv.URL,
		OnRemediation: false,
	})
	n.NotifyRemediation(context.Background(), models.AuditRecord{
		Status: models.AuditDryRun,
		Plan:   models.RemediationPlan{Action: models.Action{Type: "noop", Namespace: "demo", Target: "x"}},
	})
	if called {
		t.Fatal("expected no notification when on_remediation=false")
	}
}

func TestNew_PagerDutyOnly(t *testing.T) {
	n := New(models.NotificationsConfig{
		Enabled:             true,
		PagerDutyRoutingKey: "pd-key-123",
	})
	if n == nil {
		t.Fatal("expected notifier with PagerDuty routing key")
	}
}

func TestNew_AlertmanagerOnly(t *testing.T) {
	n := New(models.NotificationsConfig{
		Enabled:         true,
		AlertmanagerURL: "http://alertmanager:9093/api/v1/alerts",
	})
	if n == nil {
		t.Fatal("expected notifier with Alertmanager URL")
	}
}

func TestNew_NilWhenAllChannelsEmpty(t *testing.T) {
	n := New(models.NotificationsConfig{
		Enabled: true,
	})
	if n != nil {
		t.Fatal("expected nil when no channel is configured")
	}
}

func TestPagerDuty_PayloadTrigger(t *testing.T) {
	n := New(models.NotificationsConfig{Enabled: true, PagerDutyRoutingKey: "test-key-123"})
	if n == nil {
		t.Fatal("expected notifier")
	}
	ev := Event{
		Type:      "prediction",
		Title:     "KubeWise prediction: cpu_throttle",
		Message:   "demo/demo-pod — pattern confidence 88%",
		Severity:  "high",
		Entity:    "demo/demo-pod",
		Namespace: "demo",
		Details:   map[string]interface{}{"metric": "cpu_throttle"},
		Timestamp: time.Now().UTC(),
	}
	p := n.buildPagerDutyPayload("test-key-123", ev)

	if p.RoutingKey != "test-key-123" {
		t.Fatalf("expected routing_key test-key-123, got %s", p.RoutingKey)
	}
	if p.EventAction != "trigger" {
		t.Fatalf("expected trigger action, got %s", p.EventAction)
	}
	if p.Payload.Summary != "[high] KubeWise prediction: cpu_throttle" {
		t.Fatalf("unexpected summary: %s", p.Payload.Summary)
	}
	if p.Payload.Source != "demo/demo-pod" {
		t.Fatalf("unexpected source: %s", p.Payload.Source)
	}
	if p.Payload.Severity != "error" {
		t.Fatalf("expected severity error (high->error), got %s", p.Payload.Severity)
	}
	if p.Payload.Component != "demo" {
		t.Fatalf("unexpected component: %s", p.Payload.Component)
	}
}

func TestPagerDuty_RemediationResolve(t *testing.T) {
	n := New(models.NotificationsConfig{Enabled: true, PagerDutyRoutingKey: "pd-key"})
	if n == nil {
		t.Fatal("expected notifier")
	}
	ev := Event{
		Type:      "remediation",
		Title:     "Remediation verified: scale",
		Message:   "prod/svc — fixed (tier T2, confidence 95%)",
		Severity:  "high",
		Entity:    "prod/svc",
		Namespace: "prod",
		Timestamp: time.Now().UTC(),
	}
	p := n.buildPagerDutyPayload("pd-key", ev)

	if p.EventAction != "trigger" {
		t.Fatalf("expected trigger (severity=high), got %s", p.EventAction)
	}
}

func TestPagerDuty_InfoSeverityResolves(t *testing.T) {
	n := New(models.NotificationsConfig{Enabled: true, PagerDutyRoutingKey: "pd-key"})
	if n == nil {
		t.Fatal("expected notifier")
	}
	ev := Event{
		Type:      "remediation",
		Title:     "Remediation dry-run: scale",
		Message:   "[dry-run] prod/svc — fix",
		Severity:  "info",
		Entity:    "prod/svc",
		Namespace: "prod",
		Timestamp: time.Now().UTC(),
	}
	p := n.buildPagerDutyPayload("pd-key", ev)

	if p.EventAction != "resolve" {
		t.Fatalf("expected resolve for info severity, got %s", p.EventAction)
	}
}

func TestAlertmanager_Send(t *testing.T) {
	var captured []map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &captured)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	n := New(models.NotificationsConfig{
		Enabled:         true,
		AlertmanagerURL: srv.URL,
		OnPrediction:    true,
		MinScore:        0.5,
	})
	n.NotifyPrediction(context.Background(), models.PredictionResult{
		Type:       "anomaly",
		Entity:     "web-1",
		Namespace:  "default",
		MetricName: "memory_leak",
		Score:      0.95,
		Confidence: 0.9,
		Timestamp:  time.Now(),
	})

	if captured == nil {
		t.Fatal("expected Alertmanager payload")
	}
	if len(captured) == 0 {
		t.Fatal("expected at least one alert in array")
	}
	labels, ok := captured[0]["labels"].(map[string]interface{})
	if !ok {
		t.Fatal("expected labels in alert")
	}
	if labels["alertname"] != "KubeWise_prediction" {
		t.Fatalf("unexpected alertname: %v", labels["alertname"])
	}
}

func TestSlack_BlockKitBlocks(t *testing.T) {
	var payload map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &payload)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	n := New(models.NotificationsConfig{
		Enabled:         true,
		SlackWebhookURL: srv.URL,
		OnPrediction:    true,
		MinScore:        0.5,
	})
	n.NotifyPrediction(context.Background(), models.PredictionResult{
		Type:       "pattern",
		Entity:     "pod-1",
		Namespace:  "demo",
		MetricName: "oom",
		Score:      0.85,
		Confidence: 0.8,
		Timestamp:  time.Now(),
	})

	if payload == nil {
		t.Fatal("expected Slack payload")
	}
	blocks, ok := payload["blocks"].([]interface{})
	if !ok || len(blocks) == 0 {
		t.Fatal("expected Slack Block Kit blocks")
	}
	header, ok := blocks[0].(map[string]interface{})
	if !ok || header["type"] != "header" {
		t.Fatal("expected first block to be a header")
	}
}
