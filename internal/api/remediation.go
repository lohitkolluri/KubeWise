package api

import (
	"context"
	"net/http"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// Remediator handles approval workflow and live/dry-run mode at runtime.
type Remediator interface {
	ListPendingApprovals(limit int) ([]models.AuditRecord, error)
	ApproveRecord(ctx context.Context, id string) error
	RejectRecord(id, reason string) error
	RemediationState() models.RemediationModeView
	SetLiveMode(live bool)
	ApplyRemediationConfig(cfg models.RemediationConfig)
}

func (s *Server) registerRemediationRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/approvals", s.handleApprovalsList)
	mux.HandleFunc("POST /api/v1/approvals/{id}/approve", s.handleApprovalApprove)
	mux.HandleFunc("POST /api/v1/approvals/{id}/reject", s.handleApprovalReject)
	mux.HandleFunc("GET /api/v1/remediation/mode", s.handleRemediationModeGet)
	mux.HandleFunc("PUT /api/v1/remediation/mode", s.handleRemediationModePut)
	mux.HandleFunc("POST /api/v1/remediation/mode", s.handleRemediationModePut)
}

func (s *Server) handleApprovalsList(w http.ResponseWriter, r *http.Request) {
	if s.remediator == nil {
		writeJSON(w, http.StatusOK, []models.AuditRecord{})
		return
	}
	limit, err := parseLimit(r, 20, 100)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid limit")
		return
	}
	records, err := s.remediator.ListPendingApprovals(limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if records == nil {
		records = []models.AuditRecord{}
	}
	writeJSON(w, http.StatusOK, sanitizeAuditRecords(records))
}

func (s *Server) handleApprovalApprove(w http.ResponseWriter, r *http.Request) {
	if s.remediator == nil {
		writeError(w, http.StatusServiceUnavailable, "remediator unavailable")
		return
	}
	id := r.PathValue("id")
	if id == "" {
		writeError(w, http.StatusBadRequest, "missing approval id")
		return
	}
	if err := s.remediator.ApproveRecord(r.Context(), id); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "executed", "id": id})
}

func (s *Server) handleApprovalReject(w http.ResponseWriter, r *http.Request) {
	if s.remediator == nil {
		writeError(w, http.StatusServiceUnavailable, "remediator unavailable")
		return
	}
	id := r.PathValue("id")
	if id == "" {
		writeError(w, http.StatusBadRequest, "missing approval id")
		return
	}
	var body struct {
		Reason string `json:"reason"`
	}
	_ = decodeJSON(r, &body)
	if err := s.remediator.RejectRecord(id, body.Reason); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "rejected", "id": id})
}

func (s *Server) handleRemediationModeGet(w http.ResponseWriter, r *http.Request) {
	if s.remediator != nil {
		writeJSON(w, http.StatusOK, s.remediator.RemediationState())
		return
	}
	cfg, err := s.store.LoadConfig()
	if err != nil || cfg == nil {
		writeJSON(w, http.StatusOK, models.RemediationModeView{Mode: models.RemediationModeDryRun, DryRun: true, Live: false})
		return
	}
	writeJSON(w, http.StatusOK, models.RemediationModeView{
		Mode:   cfg.Remediation.Mode,
		DryRun: cfg.Remediation.DryRun,
		Live:   !cfg.Remediation.DryRun,
	})
}

func (s *Server) handleRemediationModePut(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Mode   string `json:"mode"`
		DryRun *bool  `json:"dry_run"`
		Live   *bool  `json:"live"`
	}
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	cfg, err := s.store.LoadConfig()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if cfg == nil {
		cfg = &models.AgentConfig{}
	}

	live := !cfg.Remediation.DryRun
	if body.Live != nil {
		live = *body.Live
	} else if body.DryRun != nil {
		live = !*body.DryRun
	}

	if body.Mode != "" {
		cfg.Remediation.Mode = body.Mode
	} else if live {
		cfg.Remediation.Mode = models.RemediationModeAuto
	} else {
		cfg.Remediation.Mode = models.RemediationModeDryRun
	}
	cfg.Remediation.DryRun = !live

	if err := s.store.SaveConfig(cfg); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	if s.remediator != nil {
		s.remediator.ApplyRemediationConfig(cfg.Remediation)
		s.remediator.SetLiveMode(live)
	}

	writeJSON(w, http.StatusOK, models.RemediationModeView{
		Mode:   cfg.Remediation.Mode,
		DryRun: cfg.Remediation.DryRun,
		Live:   live,
	})
}
