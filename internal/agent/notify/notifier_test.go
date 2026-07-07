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
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
