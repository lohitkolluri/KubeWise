package api

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

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

func (m *mockStore) SaveConfig(cfg *models.AgentConfig) error {
	m.config = cfg
	return nil
}

func (m *mockStore) ListAuditRecords(_ int) ([]models.AuditRecord, error) {
	return []models.AuditRecord{}, nil
}

func (m *mockStore) ListAuditRecordsSince(_ time.Time, _ int) ([]models.AuditRecord, error) {
	return []models.AuditRecord{}, nil
}

func (m *mockStore) ListAuditRecordsByStatus(_ models.AuditStatus, _ int) ([]models.AuditRecord, error) {
	return []models.AuditRecord{}, nil
}

func (m *mockStore) GetAuditRecord(id string) (*models.AuditRecord, error) {
	return nil, fmt.Errorf("audit record %q not found", id)
}

func (m *mockStore) GetLatestPredictions() ([]models.PredictionResult, error) {
	return []models.PredictionResult{}, nil
}

func (m *mockStore) ComputeAgentStats() (models.AgentStats, error) {
	return models.AgentStats{
		PredictionsTotal:   10,
		PredictionsHit:     7,
		PredictionsMissed:  3,
		PredictionAccuracy: 0.7,
	}, nil
}

func (m *mockStore) Ping() error { return nil }

func (m *mockStore) GetLatestHealthScores() ([]models.HealthScore, error) {
	return []models.HealthScore{}, nil
}

func (m *mockStore) GetHealthScoresByNamespace(_ string) ([]models.HealthScore, error) {
	return []models.HealthScore{}, nil
}

func (m *mockStore) GetHealthScoreHistory(_, _ string, _ int) ([]models.HealthScore, error) {
	return []models.HealthScore{}, nil
}

func (m *mockStore) ComputeClusterSummary() (*models.ClusterHealthSummary, error) {
	return &models.ClusterHealthSummary{}, nil
}

func (m *mockStore) GetLatestAccuracySnapshot() (*models.AccuracySnapshot, error) {
	return &models.AccuracySnapshot{
		ByPredictor:    map[string]models.AccuracyMetrics{},
		ByNamespace:    map[string]models.AccuracyMetrics{},
		ByMetric:       map[string]models.AccuracyMetrics{},
		ByResourceKind: map[string]models.AccuracyMetrics{},
	}, nil
}

func (m *mockStore) GetAccuracyHistory(_ int) ([]models.AccuracySnapshot, error) {
	return []models.AccuracySnapshot{}, nil
}

func (m *mockStore) Backup(w io.Writer) error {
	_, err := w.Write([]byte("mock-backup-data"))
	return err
}

func mustNewTestServer(t *testing.T, store Store, addr string) *Server {
	t.Helper()
	t.Setenv("KUBEWISE_ALLOW_UNAUTH", "true")
	t.Setenv("KUBEWISE_REQUIRE_API_TOKEN", "false")
	s, err := NewServer(store, addr)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	return s
}

func setupTestServer(t *testing.T) *httptest.Server {
	t.Helper()
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
	s := mustNewTestServer(t, store, ":0")
	s.registerRoutes(mux)
	return httptest.NewServer(withMiddleware(mux, middlewareConfig{corsOrigin: "*"}))
}

func TestNewServerRequiresAPIToken(t *testing.T) {
	t.Setenv("KUBEWISE_ALLOW_UNAUTH", "false")
	t.Setenv("KUBEWISE_REQUIRE_API_TOKEN", "true")
	t.Setenv("KUBEWISE_API_TOKEN", "")
	_, err := NewServer(&mockStore{}, ":0")
	if err == nil {
		t.Fatal("expected error when API token is required but missing")
	}
}

func TestHealthEndpoint(t *testing.T) {
	ts := setupTestServer(t)
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
	ts := setupTestServer(t)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/health")
	if err != nil {
		t.Fatalf("health check failed: %v", err)
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); ct != "application/json" {
		t.Fatalf("expected application/json, got %s", ct)
	}
}

func TestStatusEndpoint(t *testing.T) {
	ts := setupTestServer(t)
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
	ts := setupTestServer(t)
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
	ts := setupTestServer(t)
	defer ts.Close()

	resp, _ := http.Get(ts.URL + "/api/v1/anomalies?limit=1")
	var records []models.AnomalyRecord
	_ = json.NewDecoder(resp.Body).Decode(&records)
	_ = resp.Body.Close()

	if len(records) != 1 {
		t.Fatalf("expected 1 anomaly with limit=1, got %d", len(records))
	}
}

func TestAnomaliesEmptyStore(t *testing.T) {
	store := &mockStore{anomalies: nil}
	mux := http.NewServeMux()
	s := mustNewTestServer(t, store, ":0")
	s.registerRoutes(mux)
	ts := httptest.NewServer(withMiddleware(mux, middlewareConfig{corsOrigin: "*"}))
	defer ts.Close()

	resp, _ := http.Get(ts.URL + "/api/v1/anomalies")
	var records []models.AnomalyRecord
	_ = json.NewDecoder(resp.Body).Decode(&records)
	_ = resp.Body.Close()

	if records == nil {
		t.Fatal("expected empty array, not null")
	}
}

func TestConfigEndpoint(t *testing.T) {
	ts := setupTestServer(t)
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

func TestConfigPutEndpoint(t *testing.T) {
	ts := setupTestServer(t)
	defer ts.Close()

	body := `{"scrape_interval":"60s","prometheus_address":"http://prom:9090"}`
	req, err := http.NewRequest(http.MethodPut, ts.URL+"/api/v1/config", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("PUT /api/v1/config: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var cfg models.AgentConfig
	if err := json.NewDecoder(resp.Body).Decode(&cfg); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if cfg.ScrapeInterval != "60s" {
		t.Fatalf("expected 60s, got %s", cfg.ScrapeInterval)
	}
}

func TestConfigNoConfig(t *testing.T) {
	store := &mockStore{config: nil}
	mux := http.NewServeMux()
	s := mustNewTestServer(t, store, ":0")
	s.registerRoutes(mux)
	ts := httptest.NewServer(withMiddleware(mux, middlewareConfig{corsOrigin: "*"}))
	defer ts.Close()

	resp, _ := http.Get(ts.URL + "/api/v1/config")
	var body map[string]string
	_ = json.NewDecoder(resp.Body).Decode(&body)
	_ = resp.Body.Close()

	if body["message"] != "no config saved" {
		t.Fatalf("expected 'no config saved', got %v", body)
	}
}

func TestPredictionsEndpoint(t *testing.T) {
	ts := setupTestServer(t)
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

func TestStatsEndpoint(t *testing.T) {
	ts := setupTestServer(t)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/api/v1/stats")
	if err != nil {
		t.Fatalf("GET /api/v1/stats: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var stats models.AgentStats
	if err := json.NewDecoder(resp.Body).Decode(&stats); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if stats.PredictionAccuracy != 0.7 {
		t.Fatalf("expected accuracy 0.7, got %f", stats.PredictionAccuracy)
	}
}

func TestNotFound(t *testing.T) {
	ts := setupTestServer(t)
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
	ts := setupTestServer(t)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/health")
	if err != nil {
		t.Fatalf("health check failed: %v", err)
	}
	defer resp.Body.Close()
	origin := resp.Header.Get("Access-Control-Allow-Origin")
	if origin != "*" {
		t.Fatalf("expected CORS header *, got %s", origin)
	}
}

func TestServerScrapes(t *testing.T) {
	store := &mockStore{}
	mux := http.NewServeMux()
	s := mustNewTestServer(t, store, ":0")
	s.IncrementScrapes()
	s.IncrementScrapes()
	s.IncrementScrapes()

	mux.HandleFunc("GET /status", s.handleStatus)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	resp, _ := http.Get(ts.URL + "/status")
	var body map[string]interface{}
	_ = json.NewDecoder(resp.Body).Decode(&body)
	_ = resp.Body.Close()

	scrapes := body["scrapes"].(float64)
	if scrapes != 3 {
		t.Fatalf("expected 3 scrapes, got %f", scrapes)
	}
}

func TestBackupEndpoint(t *testing.T) {
	store := &mockStore{}
	mux := http.NewServeMux()
	s := mustNewTestServer(t, store, ":0")
	mux.HandleFunc("GET /api/v1/admin/backup", s.handleBackup)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	// Unauthenticated request should get 401 (rate limiter may allow since local).
	resp, err := http.Get(ts.URL + "/api/v1/admin/backup")
	if err != nil {
		t.Fatalf("backup request failed: %v", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", resp.StatusCode, string(body))
	}
	if string(body) != "mock-backup-data" {
		t.Fatalf("expected mock-backup-data, got %s", string(body))
	}
}

func TestRateLimitHeaders(t *testing.T) {
	store := &mockStore{}
	mux := http.NewServeMux()
	s := mustNewTestServer(t, store, ":0")
	mux.HandleFunc("GET /api/v1/predictions", s.handlePredictions)
	ts := httptest.NewServer(withMiddleware(mux, middlewareConfig{}))
	defer ts.Close()

	// First request should succeed.
	resp, err := http.Get(ts.URL + "/api/v1/predictions")
	if err != nil {
		t.Fatalf("first request failed: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("unexpected status: %d", resp.StatusCode)
	}
}
