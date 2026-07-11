package api

import (
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/featureflags"
	"github.com/lohitkolluri/KubeWise/internal/version"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func (s *Server) registerRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("GET /readyz", s.handleReadyz)
	mux.HandleFunc("GET /status", s.handleStatus)
	mux.HandleFunc("POST /api/v1/auth", s.handleAuth)
	mux.HandleFunc("GET /api/v1/predictions", s.handlePredictions)
	mux.HandleFunc("GET /api/v1/anomalies", s.handleAnomalies)
	mux.HandleFunc("GET /api/v1/config", s.handleConfigGet)
	mux.HandleFunc("PUT /api/v1/config", s.handleConfigPut)
	mux.HandleFunc("POST /api/v1/config", s.handleConfigPut)
	s.registerRemediationRoutes(mux)
	mux.HandleFunc("GET /api/v1/remediations", s.handleRemediations)
	mux.HandleFunc("GET /api/v1/audit", s.handleAudit)
	mux.HandleFunc("GET /api/v1/audit/{id}", s.handleAuditGet)
	mux.HandleFunc("GET /api/v1/stats", s.handleStats)
	mux.HandleFunc("GET /", s.handleRoot)
	flags := featureflags.Load()
	if flags.Any() {
		mux.HandleFunc("GET /api/v1/pipeline/stats", s.handlePipelineStats)
		mux.HandleFunc("GET /api/v1/cache/stats", s.handleCacheStats)
	}
	mux.HandleFunc("GET /api/v1/health", s.handleHealthScores)
	mux.HandleFunc("GET /api/v1/health/history", s.handleHealthScoreHistory)
	mux.HandleFunc("GET /api/v1/health/summary", s.handleClusterHealthSummary)
	mux.HandleFunc("GET /api/v1/accuracy", s.handleAccuracyLatest)
	mux.HandleFunc("GET /api/v1/accuracy/history", s.handleAccuracyHistory)
	mux.HandleFunc("GET /api/v1/admin/backup", s.handleBackup)
	s.registerMetrics(mux)
}

func (s *Server) handleRoot(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{
		"name":    version.AgentName,
		"version": version.Version,
	})
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleReadyz(w http.ResponseWriter, _ *http.Request) {
	if err := s.store.Ping(); err != nil {
		writeError(w, http.StatusServiceUnavailable, "store unavailable")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
}

func (s *Server) handleStatus(w http.ResponseWriter, _ *http.Request) {
	stats := s.gateStatsSnapshot()
	resp := map[string]interface{}{
		"uptime":        s.uptime().String(),
		"started_at":    s.startAt.UTC().Format(time.RFC3339),
		"scrapes":       s.scrapes.Load(),
		"gate_passed":   stats.Passed,
		"gate_dropped":  stats.Dropped,
		"gate_observed": stats.Observed,
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handlePredictions(w http.ResponseWriter, _ *http.Request) {
	preds, err := s.store.GetLatestPredictions()
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("get predictions: %v", err))
		return
	}
	if preds == nil {
		preds = []models.PredictionResult{}
	}
	writeJSON(w, http.StatusOK, preds)
}

func (s *Server) handleAnomalies(w http.ResponseWriter, r *http.Request) {
	limit, err := parseLimit(r, 20, 100)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid limit: must be 1-100")
		return
	}

	records, err := s.store.ListAnomalies(limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("list anomalies: %v", err))
		return
	}
	if records == nil {
		records = []models.AnomalyRecord{}
	}
	writeJSON(w, http.StatusOK, records)
}

func (s *Server) handleConfigGet(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	cfg, err := s.store.LoadConfig()
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("load config: %v", err))
		return
	}
	if cfg == nil {
		writeJSON(w, http.StatusOK, map[string]string{"message": "no config saved"})
		return
	}
	writeJSON(w, http.StatusOK, cfg)
}

func (s *Server) handleConfigPut(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut && r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var cfg models.AgentConfig
	if err := decodeJSON(w, r, &cfg); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := s.store.SaveConfig(&cfg); err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("save config: %v", err))
		return
	}
	if s.remediator != nil {
		s.remediator.SetObservabilityURLs(cfg.LokiURL, cfg.TempoURL)
	}
	writeJSON(w, http.StatusOK, cfg)
}

func (s *Server) handleRemediations(w http.ResponseWriter, r *http.Request) {
	limit, err := parseLimit(r, 20, 100)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid limit: must be 1-100")
		return
	}
	records, err := s.store.ListAuditRecords(limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("list audit: %v", err))
		return
	}
	if records == nil {
		records = []models.AuditRecord{}
	}
	writeJSON(w, http.StatusOK, sanitizeAuditRecords(records))
}

func sanitizeAuditRecords(records []models.AuditRecord) []models.AuditRecord {
	out := make([]models.AuditRecord, len(records))
	for i, r := range records {
		out[i] = r
		out[i].Prompt = ""
		out[i].LLMResponse = ""
		out[i].Reason = redactSensitive(out[i].Reason)
		out[i].Error = redactSensitive(out[i].Error)
		out[i].K8sResult = redactSensitive(out[i].K8sResult)
		out[i].Plan = redactPlan(out[i].Plan)
	}
	return out
}

func redactPlan(p models.RemediationPlan) models.RemediationPlan {
	p.Action.Parameters = redactParams(p.Action.Parameters)
	for i := range p.Steps {
		p.Steps[i].Parameters = redactParams(p.Steps[i].Parameters)
	}
	return p
}

func redactParams(in map[string]string) map[string]string {
	if len(in) == 0 {
		return in
	}
	out := make(map[string]string, len(in))
	for k, v := range in {
		lk := strings.ToLower(k)
		if strings.Contains(lk, "token") || strings.Contains(lk, "key") || strings.Contains(lk, "secret") || strings.Contains(lk, "password") {
			out[k] = "***"
			continue
		}
		out[k] = redactSensitive(v)
	}
	return out
}

func (s *Server) handleAudit(w http.ResponseWriter, r *http.Request) {
	limit, err := parseLimit(r, 20, 100)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid limit: must be 1-100")
		return
	}

	// Optional filters:
	// - status=pending|executed|rejected|failed|dry-run|verified|verify_failed|escalated
	// - since=RFC3339 timestamp
	status := strings.TrimSpace(r.URL.Query().Get("status"))
	since := strings.TrimSpace(r.URL.Query().Get("since"))

	var records []models.AuditRecord
	switch {
	case status != "" && since != "":
		// Prefer "since" semantics; filter status in-memory for now.
		ts, err := time.Parse(time.RFC3339, since)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid since: must be RFC3339")
			return
		}
		all, err := s.store.ListAuditRecordsSince(ts, limit)
		if err != nil {
			writeError(w, http.StatusInternalServerError, fmt.Sprintf("list audit records: %v", err))
			return
		}
		for _, rec := range all {
			if strings.EqualFold(string(rec.Status), status) {
				records = append(records, rec)
			}
		}
	case status != "":
		records, err = s.store.ListAuditRecordsByStatus(models.AuditStatus(status), limit)
	case since != "":
		ts, parseErr := time.Parse(time.RFC3339, since)
		if parseErr != nil {
			writeError(w, http.StatusBadRequest, "invalid since: must be RFC3339")
			return
		}
		records, err = s.store.ListAuditRecordsSince(ts, limit)
	default:
		records, err = s.store.ListAuditRecords(limit)
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("list audit records: %v", err))
		return
	}
	if records == nil {
		records = []models.AuditRecord{}
	}
	writeJSON(w, http.StatusOK, sanitizeAuditRecords(records))
}

func (s *Server) handleAuditGet(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		writeError(w, http.StatusBadRequest, "missing audit id")
		return
	}
	rec, err := s.store.GetAuditRecord(id)
	if err != nil {
		writeError(w, http.StatusNotFound, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, sanitizeAuditRecords([]models.AuditRecord{*rec})[0])
}

func (s *Server) handlePipelineStats(w http.ResponseWriter, _ *http.Request) {
	flags := featureflags.Load()
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"features_enabled": map[string]bool{
			"rule_engine":     flags.RuleEngine,
			"llm_router":      flags.LLMRouter,
			"context_builder": flags.ContextBuilder,
			"events_v2":       flags.EventsV2,
			"tool_plugins":    flags.ToolPlugins,
			"observability":   flags.Observability,
		},
		"pipeline_active": flags.Any(),
	})
}

func (s *Server) handleCacheStats(w http.ResponseWriter, _ *http.Request) {
	flags := featureflags.Load()
	resp := map[string]interface{}{
		"cache_type":   "semantic",
		"cache_active": flags.SemanticCache,
	}
	if flags.SemanticCache {
		resp["hits"] = 0
		resp["misses"] = 0
		resp["size"] = 0
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleBackup(w http.ResponseWriter, r *http.Request) {
	if !isLocalRequest(r) && s.apiToken == "" {
		writeError(w, http.StatusForbidden, "backup requires authentication or local access")
		return
	}

	pr, pw := io.Pipe()
	defer func() { _ = pr.Close() }()

	go func() {
		if err := s.store.Backup(pw); err != nil {
			pw.CloseWithError(fmt.Errorf("backup: %w", err))
			return
		}
		if err := pw.Close(); err != nil {
			slog.Error("api: backup pipe close error", "error", err)
		}
	}()

	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Disposition", fmt.Sprintf(`attachment; filename="kubewise-backup-%s.db"`, time.Now().Format("20060102T150405")))
	if _, err := io.Copy(w, pr); err != nil {
		slog.Error("api: backup stream error", "error", err)
	}
}

func isLocalRequest(r *http.Request) bool {
	host := r.RemoteAddr
	if idx := strings.LastIndex(host, ":"); idx >= 0 {
		host = host[:idx]
	}
	return host == "127.0.0.1" || host == "::1" || host == "localhost"
}
