package api

import (
	"fmt"
	"net/http"
	"strconv"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func (s *Server) handleAccuracyLatest(w http.ResponseWriter, _ *http.Request) {
	snap, err := s.store.GetLatestAccuracySnapshot()
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("get accuracy: %v", err))
		return
	}
	if snap == nil {
		snap = &models.AccuracySnapshot{
			ByPredictor:    map[string]models.AccuracyMetrics{},
			ByNamespace:    map[string]models.AccuracyMetrics{},
			ByMetric:       map[string]models.AccuracyMetrics{},
			ByResourceKind: map[string]models.AccuracyMetrics{},
		}
	}
	writeJSON(w, http.StatusOK, snap)
}

func (s *Server) handleAccuracyHistory(w http.ResponseWriter, r *http.Request) {
	limitStr := r.URL.Query().Get("limit")
	limit := 24
	if limitStr != "" {
		if v, err := strconv.Atoi(limitStr); err == nil && v > 0 && v <= 168 {
			limit = v
		}
	}
	history, err := s.store.GetAccuracyHistory(limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("get accuracy history: %v", err))
		return
	}
	if history == nil {
		history = []models.AccuracySnapshot{}
	}
	writeJSON(w, http.StatusOK, history)
}
