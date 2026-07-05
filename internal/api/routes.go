package api

import (
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func (s *Server) registerRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("GET /status", s.handleStatus)
	mux.HandleFunc("GET /api/v1/predictions", s.handlePredictions)
	mux.HandleFunc("GET /api/v1/anomalies", s.handleAnomalies)
	mux.HandleFunc("GET /api/v1/config", s.handleConfig)
	mux.HandleFunc("GET /api/v1/remediations", s.handleRemediations)
	mux.HandleFunc("GET /api/v1/audit", s.handleAudit)
	mux.HandleFunc("GET /", s.handleNotFound)
}

func (s *Server) handleNotFound(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"name": "kubewise-agent", "version": "0.1.0"})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	resp := map[string]interface{}{
		"uptime":     s.uptime().String(),
		"started_at": s.startAt.UTC().Format(time.RFC3339),
		"scrapes":    s.scrapes,
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handlePredictions(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, []interface{}{})
}

func (s *Server) handleAnomalies(w http.ResponseWriter, r *http.Request) {
	limit := 20
	if l := r.URL.Query().Get("limit"); l != "" {
		if parsed, err := strconv.Atoi(l); err == nil && parsed > 0 && parsed <= 100 {
			limit = parsed
		}
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

func (s *Server) handleConfig(w http.ResponseWriter, r *http.Request) {
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

func (s *Server) handleRemediations(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"message": "remediation endpoint (use /api/v1/audit for history)"})
}

func (s *Server) handleAudit(w http.ResponseWriter, r *http.Request) {
	limit := 20
	if l := r.URL.Query().Get("limit"); l != "" {
		if parsed, err := strconv.Atoi(l); err == nil && parsed > 0 && parsed <= 100 {
			limit = parsed
		}
	}

	records, err := s.store.ListAuditRecords(limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("list audit records: %v", err))
		return
	}
	if records == nil {
		records = []models.AuditRecord{}
	}
	writeJSON(w, http.StatusOK, records)
}
