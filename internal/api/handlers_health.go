package api

import (
	"fmt"
	"net/http"
	"strconv"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func (s *Server) handleHealthScores(w http.ResponseWriter, r *http.Request) {
	ns := r.URL.Query().Get("namespace")
	if ns != "" {
		scores, err := s.store.GetHealthScoresByNamespace(ns)
		if err != nil {
			writeError(w, http.StatusInternalServerError, fmt.Sprintf("get health scores: %v", err))
			return
		}
		writeJSON(w, http.StatusOK, scores)
		return
	}
	scores, err := s.store.GetLatestHealthScores()
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("get health scores: %v", err))
		return
	}
	if scores == nil {
		scores = []models.HealthScore{}
	}
	writeJSON(w, http.StatusOK, scores)
}

func (s *Server) handleHealthScoreHistory(w http.ResponseWriter, r *http.Request) {
	entity := r.URL.Query().Get("entity")
	namespace := r.URL.Query().Get("namespace")
	limitStr := r.URL.Query().Get("limit")
	limit := 50
	if limitStr != "" {
		if v, err := strconv.Atoi(limitStr); err == nil && v > 0 && v <= 200 {
			limit = v
		}
	}
	if entity == "" {
		writeError(w, http.StatusBadRequest, "missing entity query parameter")
		return
	}
	history, err := s.store.GetHealthScoreHistory(entity, namespace, limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("get health history: %v", err))
		return
	}
	if history == nil {
		history = []models.HealthScore{}
	}
	writeJSON(w, http.StatusOK, history)
}

func (s *Server) handleClusterHealthSummary(w http.ResponseWriter, r *http.Request) {
	summary, err := s.store.ComputeClusterSummary()
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("compute cluster summary: %v", err))
		return
	}
	writeJSON(w, http.StatusOK, summary)
}
