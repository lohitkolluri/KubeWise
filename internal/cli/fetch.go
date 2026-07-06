package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

type agentStatus struct {
	Uptime       string `json:"uptime" yaml:"uptime"`
	StartedAt    string `json:"started_at" yaml:"started_at"`
	Scrapes      int64  `json:"scrapes" yaml:"scrapes"`
	GatePassed   uint64 `json:"gate_passed" yaml:"gate_passed"`
	GateDropped  uint64 `json:"gate_dropped" yaml:"gate_dropped"`
	GateObserved uint64 `json:"gate_observed" yaml:"gate_observed"`
}

func fetchStatus() (agentStatus, error) {
	body, _, err := agentGet("/status")
	if err != nil {
		return agentStatus{}, err
	}
	var st agentStatus
	if err := json.Unmarshal(body, &st); err != nil {
		return agentStatus{}, fmt.Errorf("parse status: %w", err)
	}
	return st, nil
}

func fetchHealth() (map[string]string, error) {
	body, _, err := agentGet("/health")
	if err != nil {
		return nil, err
	}
	var h map[string]string
	if err := json.Unmarshal(body, &h); err != nil {
		return nil, fmt.Errorf("parse health: %w", err)
	}
	return h, nil
}

func fetchAgentConfig() (*models.AgentConfig, error) {
	body, _, err := agentGet("/api/v1/config")
	if err != nil {
		return nil, err
	}
	var msg map[string]string
	if err := json.Unmarshal(body, &msg); err == nil {
		if m, ok := msg["message"]; ok {
			return nil, fmt.Errorf("%s", m)
		}
	}
	var cfg models.AgentConfig
	if err := json.Unmarshal(body, &cfg); err != nil {
		return nil, fmt.Errorf("parse config: %w", err)
	}
	return &cfg, nil
}

func fetchPredictions() ([]models.PredictionResult, error) {
	body, _, err := agentGet("/api/v1/predictions")
	if err != nil {
		return nil, err
	}
	var preds []models.PredictionResult
	if err := json.Unmarshal(body, &preds); err != nil {
		return nil, fmt.Errorf("parse predictions: %w", err)
	}
	return preds, nil
}

func fetchAnomalies(limit int) ([]models.AnomalyRecord, error) {
	path := "/api/v1/anomalies"
	if limit > 0 {
		path = fmt.Sprintf("/api/v1/anomalies?limit=%d", limit)
	}
	body, _, err := agentGet(path)
	if err != nil {
		return nil, err
	}
	var records []models.AnomalyRecord
	if err := json.Unmarshal(body, &records); err != nil {
		return nil, fmt.Errorf("parse anomalies: %w", err)
	}
	return records, nil
}

func fetchAudit(limit int) ([]models.AuditRecord, error) {
	path := "/api/v1/audit"
	if limit > 0 {
		path = fmt.Sprintf("/api/v1/audit?limit=%d", limit)
	}
	body, _, err := agentGet(path)
	if err != nil {
		return nil, err
	}
	var records []models.AuditRecord
	if err := json.Unmarshal(body, &records); err != nil {
		return nil, fmt.Errorf("parse audit: %w", err)
	}
	return records, nil
}

func putAgentConfig(cfg *models.AgentConfig) error {
	_, _, err := agentWrite("/api/v1/config", cfg)
	return err
}

func fetchRemediationMode() (models.RemediationModeView, error) {
	body, _, err := agentGet("/api/v1/remediation/mode")
	if err != nil {
		return models.RemediationModeView{}, err
	}
	var mode models.RemediationModeView
	if err := json.Unmarshal(body, &mode); err != nil {
		return models.RemediationModeView{}, fmt.Errorf("parse remediation mode: %w", err)
	}
	return mode, nil
}

func setRemediationLive(live bool) (models.RemediationModeView, error) {
	body, _, err := agentWrite("/api/v1/remediation/mode", map[string]bool{"live": live})
	if err != nil {
		return models.RemediationModeView{}, err
	}
	var mode models.RemediationModeView
	if err := json.Unmarshal(body, &mode); err != nil {
		return models.RemediationModeView{}, fmt.Errorf("parse remediation mode: %w", err)
	}
	return mode, nil
}

func setRemediationMode(mode string) (models.RemediationModeView, error) {
	body, _, err := agentWrite("/api/v1/remediation/mode", map[string]string{"mode": mode})
	if err != nil {
		return models.RemediationModeView{}, err
	}
	var view models.RemediationModeView
	if err := json.Unmarshal(body, &view); err != nil {
		return models.RemediationModeView{}, fmt.Errorf("parse remediation mode: %w", err)
	}
	return view, nil
}

func fetchApprovals(limit int) ([]models.AuditRecord, error) {
	path := "/api/v1/approvals"
	if limit > 0 {
		path = fmt.Sprintf("/api/v1/approvals?limit=%d", limit)
	}
	body, _, err := agentGet(path)
	if err != nil {
		return nil, err
	}
	var records []models.AuditRecord
	if err := json.Unmarshal(body, &records); err != nil {
		return nil, fmt.Errorf("parse approvals: %w", err)
	}
	return records, nil
}

func approveRemediation(id string) error {
	_, _, err := agentRequest(http.MethodPost, "/api/v1/approvals/"+id+"/approve", nil)
	return err
}

func rejectRemediation(id, reason string) error {
	_, _, err := agentRequest(http.MethodPost, "/api/v1/approvals/"+id+"/reject", map[string]string{"reason": reason})
	return err
}

func buildLimitQuery(limit int, extra url.Values) string {
	q := extra
	if q == nil {
		q = url.Values{}
	}
	if limit > 0 {
		q.Set("limit", fmt.Sprintf("%d", limit))
	}
	if len(q) == 0 {
		return ""
	}
	return "?" + q.Encode()
}
