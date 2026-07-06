package api

import (
	"fmt"
	"net/http"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func (s *Server) registerRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("GET /status", s.handleStatus)
	mux.HandleFunc("GET /api/v1/predictions", s.handlePredictions)
	mux.HandleFunc("GET /api/v1/anomalies", s.handleAnomalies)
	mux.HandleFunc("GET /api/v1/config", s.handleConfigGet)
	mux.HandleFunc("PUT /api/v1/config", s.handleConfigPut)
	mux.HandleFunc("POST /api/v1/config", s.handleConfigPut)
	s.registerRemediationRoutes(mux)
	mux.HandleFunc("GET /api/v1/remediations", s.handleRemediations)
	mux.HandleFunc("GET /api/v1/audit", s.handleAudit)
	mux.HandleFunc("GET /", s.handleNotFound)
}

func (s *Server) handleNotFound(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"name": "kubewise-agent", "version": "0.2.0"})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
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

func (s *Server) handlePredictions(w http.ResponseWriter, r *http.Request) {
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
	if err := decodeJSON(r, &cfg); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := s.store.SaveConfig(&cfg); err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("save config: %v", err))
		return
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
	}
	return out
}

func (s *Server) handleAudit(w http.ResponseWriter, r *http.Request) {
	limit, err := parseLimit(r, 20, 100)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid limit: must be 1-100")
		return
	}

	records, err := s.store.ListAuditRecords(limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("list audit records: %v", err))
		return
	}
	if records == nil {
		records = []models.AuditRecord{}
	}
	writeJSON(w, http.StatusOK, sanitizeAuditRecords(records))
}
