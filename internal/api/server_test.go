package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

type mockStore struct {
	anomalies []models.AnomalyRecord
	config    *models.AgentConfig
}

func (m *mockStore) ListAnomalies(limit int) ([]models.AnomalyRecord, error) {
	if m.anomalies == nil {
		return []models.AnomalyRecord{}, nil
	}
	if limit > len(m.anomalies) {
		limit = len(m.anomalies)
	}
	return m.anomalies[:limit], nil
}

func (m *mockStore) LoadConfig() (*models.AgentConfig, error) {
	return m.config, nil
}

func (m *mockStore) ListAuditRecords(limit int) ([]models.AuditRecord, error) {
	return []models.AuditRecord{}, nil
}

func (m *mockStore) GetLatestPredictions() ([]models.PredictionResult, error) {
	return []models.PredictionResult{}, nil
}

func setupTestServer() *httptest.Server {
	store := &mockStore{
		anomalies: []models.AnomalyRecord{
			{ID: "1", Entity: "pod-a", Pattern: "OOMKilled", Score: 0.8},
			{ID: "2", Entity: "pod-b", Pattern: "CrashLoopBackOff", Score: 0.6},
		},
		config: &models.AgentConfig{
			ScrapeInterval:    "30s",
			PrometheusAddress: "http://localhost:9090",
		},
	}
	mux := http.NewServeMux()
	s := NewServer(store, ":0")
	s.registerRoutes(mux)
	return httptest.NewServer(withMiddleware(mux, ""))
}

func TestHealthEndpoint(t *testing.T) {
	ts := setupTestServer()
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/health")
	if err != nil {
		t.Fatalf("GET /health: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}

	var body map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["status"] != "ok" {
		t.Fatalf("expected status=ok, got %v", body)
	}
}

func TestHealthContentType(t *testing.T) {
	ts := setupTestServer()
	defer ts.Close()

	resp, _ := http.Get(ts.URL + "/health")
	if ct := resp.Header.Get("Content-Type"); ct != "application/json" {
		t.Fatalf("expected application/json, got %s", ct)
	}
}

func TestStatusEndpoint(t *testing.T) {
	ts := setupTestServer()
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/status")
	if err != nil {
		t.Fatalf("GET /status: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}

	var body map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["uptime"] == nil {
		t.Fatal("expected uptime in status")
	}
}

func TestAnomaliesEndpoint(t *testing.T) {
	ts := setupTestServer()
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/api/v1/anomalies")
	if err != nil {
		t.Fatalf("GET /api/v1/anomalies: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}

	var records []models.AnomalyRecord
	if err := json.NewDecoder(resp.Body).Decode(&records); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(records) == 0 {
		t.Fatal("expected anomalies")
	}
}

func TestAnomaliesLimitParam(t *testing.T) {
	ts := setupTestServer()
	defer ts.Close()

	resp, _ := http.Get(ts.URL + "/api/v1/anomalies?limit=1")
	var records []models.AnomalyRecord
	json.NewDecoder(resp.Body).Decode(&records)
	resp.Body.Close()

	if len(records) != 1 {
		t.Fatalf("expected 1 anomaly with limit=1, got %d", len(records))
	}
}

func TestAnomaliesEmptyStore(t *testing.T) {
	store := &mockStore{anomalies: nil}
	mux := http.NewServeMux()
	s := NewServer(store, ":0")
	s.registerRoutes(mux)
	ts := httptest.NewServer(withMiddleware(mux, ""))
	defer ts.Close()

	resp, _ := http.Get(ts.URL + "/api/v1/anomalies")
	var records []models.AnomalyRecord
	json.NewDecoder(resp.Body).Decode(&records)
	resp.Body.Close()

	if records == nil {
		t.Fatal("expected empty array, not null")
	}
}

func TestConfigEndpoint(t *testing.T) {
	ts := setupTestServer()
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/api/v1/config")
	if err != nil {
		t.Fatalf("GET /api/v1/config: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}

	var cfg models.AgentConfig
	if err := json.NewDecoder(resp.Body).Decode(&cfg); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if cfg.ScrapeInterval != "30s" {
		t.Fatalf("expected scrape_interval 30s, got %s", cfg.ScrapeInterval)
	}
}

func TestConfigNoConfig(t *testing.T) {
	store := &mockStore{config: nil}
	mux := http.NewServeMux()
	s := NewServer(store, ":0")
	s.registerRoutes(mux)
	ts := httptest.NewServer(withMiddleware(mux, ""))
	defer ts.Close()

	resp, _ := http.Get(ts.URL + "/api/v1/config")
	var body map[string]string
	json.NewDecoder(resp.Body).Decode(&body)
	resp.Body.Close()

	if body["message"] != "no config saved" {
		t.Fatalf("expected 'no config saved', got %v", body)
	}
}

func TestPredictionsEndpoint(t *testing.T) {
	ts := setupTestServer()
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/api/v1/predictions")
	if err != nil {
		t.Fatalf("GET /api/v1/predictions: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}

	var predictions []interface{}
	if err := json.NewDecoder(resp.Body).Decode(&predictions); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if predictions == nil {
		t.Fatal("expected empty array, not null")
	}
}

func TestNotFound(t *testing.T) {
	ts := setupTestServer()
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/nonexistent")
	if err != nil {
		t.Fatalf("GET /nonexistent: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", resp.StatusCode)
	}

	// Should return JSON error, not HTML
	ct := resp.Header.Get("Content-Type")
	if ct != "application/json" {
		t.Fatalf("expected JSON error body, got Content-Type: %s", ct)
	}
}

func TestCORSHeaders(t *testing.T) {
	ts := setupTestServer()
	defer ts.Close()

	resp, _ := http.Get(ts.URL + "/health")
	origin := resp.Header.Get("Access-Control-Allow-Origin")
	if origin != "*" {
		t.Fatalf("expected CORS header *, got %s", origin)
	}
}

func TestServerScrapes(t *testing.T) {
	store := &mockStore{}
	mux := http.NewServeMux()
	s := NewServer(store, ":0")
	s.IncrementScrapes()
	s.IncrementScrapes()
	s.IncrementScrapes()

	mux.HandleFunc("GET /status", s.handleStatus)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	resp, _ := http.Get(ts.URL + "/status")
	var body map[string]interface{}
	json.NewDecoder(resp.Body).Decode(&body)
	resp.Body.Close()

	scrapes := body["scrapes"].(float64)
	if scrapes != 3 {
		t.Fatalf("expected 3 scrapes, got %f", scrapes)
	}
}
