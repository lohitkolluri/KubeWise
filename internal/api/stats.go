package api

import (
	"fmt"
	"net/http"
)

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	stats, err := s.store.ComputeAgentStats()
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("compute stats: %v", err))
		return
	}
	writeJSON(w, http.StatusOK, stats)
}
