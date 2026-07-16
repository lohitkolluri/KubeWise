package store_test

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/llm"
	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func TestOpenClose(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.db")
	s, err := store.Open(path)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	if err := s.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
}

func TestMetricsRingBuffer(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.db")
	s, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	now := time.Now()
	for i := 0; i < 5; i++ {
		if err := s.AppendMetric("cpu_usage", float64(i), now.Add(time.Duration(i)*time.Second)); err != nil {
			t.Fatalf("AppendMetric: %v", err)
		}
	}

	points, err := s.GetMetrics("cpu_usage", 3)
	if err != nil {
		t.Fatalf("GetMetrics: %v", err)
	}
	if len(points) != 3 {
		t.Fatalf("expected 3 points, got %d", len(points))
	}
	if points[0].Value != 2 || points[2].Value != 4 {
		t.Fatalf("unexpected values: %+v", points)
	}
}

func TestAnomalyCRUD(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.db")
	s, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	now := time.Now()
	r := &models.AnomalyRecord{
		ID:         "test-1",
		Entity:     "pod/nginx-abc123",
		Namespace:  "default",
		MetricName: "memory_usage",
		Score:      0.92,
		Pattern:    "OOMRisk",
		DetectedAt: &now,
		Status:     "active",
	}

	if err := s.SaveAnomaly(r); err != nil {
		t.Fatalf("SaveAnomaly: %v", err)
	}

	got, err := s.GetAnomaly("test-1")
	if err != nil {
		t.Fatalf("GetAnomaly: %v", err)
	}
	if got == nil {
		t.Fatal("GetAnomaly returned nil")
	}
	if got.Score != 0.92 || got.Pattern != "OOMRisk" {
		t.Fatalf("unexpected anomaly: %+v", got)
	}

	// Update
	r.Status = "remediated"
	if err := s.UpdateAnomaly(r); err != nil {
		t.Fatalf("UpdateAnomaly: %v", err)
	}

	list, err := s.ListAnomalies(10)
	if err != nil {
		t.Fatalf("ListAnomalies: %v", err)
	}
	if len(list) != 1 {
		t.Fatalf("expected 1 anomaly, got %d", len(list))
	}
	if list[0].Status != "remediated" {
		t.Fatalf("expected remediated status, got %s", list[0].Status)
	}
}

func TestConfigSaveLoad(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.db")
	s, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	// Load when empty — should return nil, not error
	cfg, err := s.LoadConfig()
	if err != nil {
		t.Fatalf("LoadConfig on empty: %v", err)
	}
	if cfg != nil {
		t.Fatalf("expected nil config, got %+v", cfg)
	}

	saved := &models.AgentConfig{
		ScrapeInterval:    "30s",
		PrometheusAddress: "http://prometheus:9090",
		LLMProvider:       "openrouter",
		LLMModel:          llm.DefaultModel,
		Remediation: models.RemediationConfig{
			Mode:      "semi",
			DryRun:    true,
			RateLimit: 5,
		},
	}
	if err := s.SaveConfig(saved); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}

	loaded, err := s.LoadConfig()
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if loaded.ScrapeInterval != "30s" || loaded.LLMModel != llm.DefaultModel {
		t.Fatalf("unexpected config: %+v", loaded)
	}
}

func TestTrimOlderThan(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.db")
	s, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	now := time.Now()
	for i := 0; i < 10; i++ {
		if err := s.AppendMetric("test_metric", float64(i), now.Add(-time.Duration(10-i)*time.Second)); err != nil {
			t.Fatalf("AppendMetric: %v", err)
		}
	}

	// Trim everything older than 5 seconds
	if err := s.TrimOlderThan(5 * time.Second); err != nil {
		t.Fatalf("TrimOlderThan: %v", err)
	}

	points, err := s.GetMetrics("test_metric", 100)
	if err != nil {
		t.Fatalf("GetMetrics: %v", err)
	}
	if len(points) == 0 {
		t.Fatal("expected remaining points after trim")
	}
	if len(points) >= 10 {
		t.Fatalf("expected fewer points after trim, got %d", len(points))
	}
}
