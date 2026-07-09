package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

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
	body, _, err := agentGet(context.Background(), "/status")
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
	body, _, err := agentGet(context.Background(), "/health")
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
	body, _, err := agentGet(context.Background(), "/api/v1/config")
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
	body, _, err := agentGet(context.Background(), "/api/v1/predictions")
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
	body, _, err := agentGet(context.Background(), path)
	if err != nil {
		return nil, err
	}
	var records []models.AnomalyRecord
	if err := json.Unmarshal(body, &records); err != nil {
		return nil, fmt.Errorf("parse anomalies: %w", err)
	}
	return records, nil
}

func fetchAuditFiltered(limit int, status, since, id string) ([]models.AuditRecord, error) {
	status = strings.TrimSpace(status)
	since = strings.TrimSpace(since)
	id = strings.TrimSpace(id)

	if id != "" {
		body, _, err := agentGet(context.Background(), "/api/v1/audit/"+url.PathEscape(id))
		if err != nil {
			return nil, err
		}
		var rec models.AuditRecord
		if err := json.Unmarshal(body, &rec); err != nil {
			return nil, fmt.Errorf("parse audit: %w", err)
		}
		return []models.AuditRecord{rec}, nil
	}

	q := url.Values{}
	if status != "" {
		q.Set("status", status)
	}
	if since != "" {
		q.Set("since", since)
	}
	path := "/api/v1/audit" + buildLimitQuery(limit, q)
	body, _, err := agentGet(context.Background(), path)
	if err != nil {
		return nil, err
	}
	var records []models.AuditRecord
	if err := json.Unmarshal(body, &records); err != nil {
		return nil, fmt.Errorf("parse audit: %w", err)
	}
	return records, nil
}

func fetchStats() (models.AgentStats, error) {
	body, _, err := agentGet(context.Background(), "/api/v1/stats")
	if err != nil {
		return models.AgentStats{}, err
	}
	var stats models.AgentStats
	if err := json.Unmarshal(body, &stats); err != nil {
		return models.AgentStats{}, fmt.Errorf("parse stats: %w", err)
	}
	return stats, nil
}

func putAgentConfig(cfg *models.AgentConfig) error {
	_, _, err := agentWrite(context.Background(), "/api/v1/config", cfg)
	return err
}

func fetchHealthScores(namespace string) ([]models.HealthScore, error) {
	path := "/api/v1/health"
	if namespace != "" {
		path += "?namespace=" + url.QueryEscape(namespace)
	}
	body, _, err := agentGet(context.Background(), path)
	if err != nil {
		return nil, err
	}
	var scores []models.HealthScore
	if err := json.Unmarshal(body, &scores); err != nil {
		return nil, fmt.Errorf("parse health scores: %w", err)
	}
	return scores, nil
}

func fetchHealthSummary() (*models.ClusterHealthSummary, error) {
	body, _, err := agentGet(context.Background(), "/api/v1/health/summary")
	if err != nil {
		return nil, err
	}
	var summary models.ClusterHealthSummary
	if err := json.Unmarshal(body, &summary); err != nil {
		return nil, fmt.Errorf("parse health summary: %w", err)
	}
	return &summary, nil
}

func fetchAccuracy() (*models.AccuracySnapshot, error) {
	body, _, err := agentGet(context.Background(), "/api/v1/accuracy")
	if err != nil {
		return nil, err
	}
	var snap models.AccuracySnapshot
	if err := json.Unmarshal(body, &snap); err != nil {
		return nil, fmt.Errorf("parse accuracy: %w", err)
	}
	return &snap, nil
}

func fetchRemediationMode() (models.RemediationModeView, error) {
	body, _, err := agentGet(context.Background(), "/api/v1/remediation/mode")
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
	body, _, err := agentWrite(context.Background(), "/api/v1/remediation/mode", map[string]bool{"live": live})
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
	body, _, err := agentWrite(context.Background(), "/api/v1/remediation/mode", map[string]string{"mode": mode})
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
	body, _, err := agentGet(context.Background(), path)
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
	_, _, err := agentRequest(context.Background(), http.MethodPost, "/api/v1/approvals/"+id+"/approve", nil)
	return err
}

func rejectRemediation(id, reason string) error {
	_, _, err := agentRequest(context.Background(), http.MethodPost, "/api/v1/approvals/"+id+"/reject", map[string]string{"reason": reason})
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

// ── Parallel refresh ──

type fetchResult struct {
	status     agentStatus
	healthOK   bool
	err        error
	preds      []models.PredictionResult
	anomalies  []models.AnomalyRecord
	audits     []models.AuditRecord
	config     *models.AgentConfig
	remMode    models.RemediationModeView
	pending    []models.AuditRecord
	logs       string
	lastUpdate time.Time

	// Health & Accuracy
	healthScores []models.HealthScore
	healthSum    *models.ClusterHealthSummary
	accSnap      *models.AccuracySnapshot

	// Per-tab errors
	predErr   error
	anomErr   error
	auditErr  error
	configErr error
	remErr    error
	pendErr   error
	logsErr   error
	healthErr error
	accErr    error
}

// Context-aware variants used by fetchAll() so cancellation applies to in-flight HTTP calls.
func fetchStatusCtx(ctx context.Context) (agentStatus, error) {
	body, _, err := agentGet(ctx, "/status")
	if err != nil {
		return agentStatus{}, err
	}
	var st agentStatus
	if err := json.Unmarshal(body, &st); err != nil {
		return agentStatus{}, fmt.Errorf("parse status: %w", err)
	}
	return st, nil
}

func fetchHealthCtx(ctx context.Context) (map[string]string, error) {
	body, _, err := agentGet(ctx, "/health")
	if err != nil {
		return nil, err
	}
	var h map[string]string
	if err := json.Unmarshal(body, &h); err != nil {
		return nil, fmt.Errorf("parse health: %w", err)
	}
	return h, nil
}

func fetchPredictionsCtx(ctx context.Context) ([]models.PredictionResult, error) {
	body, _, err := agentGet(ctx, "/api/v1/predictions")
	if err != nil {
		return nil, err
	}
	var preds []models.PredictionResult
	if err := json.Unmarshal(body, &preds); err != nil {
		return nil, fmt.Errorf("parse predictions: %w", err)
	}
	return preds, nil
}

func fetchAnomaliesCtx(ctx context.Context, limit int) ([]models.AnomalyRecord, error) {
	path := "/api/v1/anomalies"
	if limit > 0 {
		path = fmt.Sprintf("/api/v1/anomalies?limit=%d", limit)
	}
	body, _, err := agentGet(ctx, path)
	if err != nil {
		return nil, err
	}
	var records []models.AnomalyRecord
	if err := json.Unmarshal(body, &records); err != nil {
		return nil, fmt.Errorf("parse anomalies: %w", err)
	}
	return records, nil
}

func fetchAuditFilteredCtx(ctx context.Context, limit int, status, since, id string) ([]models.AuditRecord, error) {
	status = strings.TrimSpace(status)
	since = strings.TrimSpace(since)
	id = strings.TrimSpace(id)

	if id != "" {
		body, _, err := agentGet(ctx, "/api/v1/audit/"+url.PathEscape(id))
		if err != nil {
			return nil, err
		}
		var rec models.AuditRecord
		if err := json.Unmarshal(body, &rec); err != nil {
			return nil, fmt.Errorf("parse audit: %w", err)
		}
		return []models.AuditRecord{rec}, nil
	}

	q := url.Values{}
	if status != "" {
		q.Set("status", status)
	}
	if since != "" {
		q.Set("since", since)
	}
	path := "/api/v1/audit" + buildLimitQuery(limit, q)
	body, _, err := agentGet(ctx, path)
	if err != nil {
		return nil, err
	}
	var records []models.AuditRecord
	if err := json.Unmarshal(body, &records); err != nil {
		return nil, fmt.Errorf("parse audit: %w", err)
	}
	return records, nil
}

func fetchAgentConfigCtx(ctx context.Context) (*models.AgentConfig, error) {
	body, _, err := agentGet(ctx, "/api/v1/config")
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

func fetchRemediationModeCtx(ctx context.Context) (models.RemediationModeView, error) {
	body, _, err := agentGet(ctx, "/api/v1/remediation/mode")
	if err != nil {
		return models.RemediationModeView{}, err
	}
	var mode models.RemediationModeView
	if err := json.Unmarshal(body, &mode); err != nil {
		return models.RemediationModeView{}, fmt.Errorf("parse remediation mode: %w", err)
	}
	return mode, nil
}

func fetchApprovalsCtx(ctx context.Context, limit int) ([]models.AuditRecord, error) {
	path := "/api/v1/approvals"
	if limit > 0 {
		path = fmt.Sprintf("/api/v1/approvals?limit=%d", limit)
	}
	body, _, err := agentGet(ctx, path)
	if err != nil {
		return nil, err
	}
	var records []models.AuditRecord
	if err := json.Unmarshal(body, &records); err != nil {
		return nil, fmt.Errorf("parse approvals: %w", err)
	}
	return records, nil
}

func fetchHealthScoresCtx(ctx context.Context, namespace string) ([]models.HealthScore, error) {
	path := "/api/v1/health"
	if namespace != "" {
		path += "?namespace=" + url.QueryEscape(namespace)
	}
	body, _, err := agentGet(ctx, path)
	if err != nil {
		return nil, err
	}
	var scores []models.HealthScore
	if err := json.Unmarshal(body, &scores); err != nil {
		return nil, fmt.Errorf("parse health scores: %w", err)
	}
	return scores, nil
}

func fetchHealthSummaryCtx(ctx context.Context) (*models.ClusterHealthSummary, error) {
	body, _, err := agentGet(ctx, "/api/v1/health/summary")
	if err != nil {
		return nil, err
	}
	var summary models.ClusterHealthSummary
	if err := json.Unmarshal(body, &summary); err != nil {
		return nil, fmt.Errorf("parse health summary: %w", err)
	}
	return &summary, nil
}

func fetchAccuracyCtx(ctx context.Context) (*models.AccuracySnapshot, error) {
	body, _, err := agentGet(ctx, "/api/v1/accuracy")
	if err != nil {
		return nil, err
	}
	var snap models.AccuracySnapshot
	if err := json.Unmarshal(body, &snap); err != nil {
		return nil, fmt.Errorf("parse accuracy: %w", err)
	}
	return &snap, nil
}

func fetchAll(auditStatus, auditSince string) fetchResult {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	var (
		wg     sync.WaitGroup
		result fetchResult
		mu     sync.Mutex
	)

	setErr := func(field *error, err error) {
		mu.Lock()
		*field = err
		mu.Unlock()
	}

	// Launch all fetches in parallel
	wg.Add(1)
	go func() {
		defer wg.Done()
		st, err := fetchStatusCtx(ctx)
		mu.Lock()
		result.status = st
		result.err = err
		mu.Unlock()
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		_, err := fetchHealthCtx(ctx)
		mu.Lock()
		result.healthOK = err == nil
		if err != nil {
			result.err = err
		}
		mu.Unlock()
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		select {
		case <-ctx.Done():
			setErr(&result.predErr, ctx.Err())
			return
		default:
		}
		preds, err := fetchPredictionsCtx(ctx)
		mu.Lock()
		result.preds = preds
		result.predErr = err
		mu.Unlock()
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		select {
		case <-ctx.Done():
			setErr(&result.anomErr, ctx.Err())
			return
		default:
		}
		anomalies, err := fetchAnomaliesCtx(ctx, 30)
		mu.Lock()
		result.anomalies = anomalies
		result.anomErr = err
		mu.Unlock()
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		select {
		case <-ctx.Done():
			setErr(&result.auditErr, ctx.Err())
			return
		default:
		}
		audits, err := fetchAuditFilteredCtx(ctx, 25, auditStatus, auditSince, "")
		mu.Lock()
		result.audits = audits
		result.auditErr = err
		mu.Unlock()
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		select {
		case <-ctx.Done():
			setErr(&result.configErr, ctx.Err())
			return
		default:
		}
		cfg, err := fetchAgentConfigCtx(ctx)
		if err != nil && strings.Contains(err.Error(), "no config") {
			cfg = nil
			err = nil
		}
		mu.Lock()
		result.config = cfg
		result.configErr = err
		mu.Unlock()
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		select {
		case <-ctx.Done():
			setErr(&result.remErr, ctx.Err())
			return
		default:
		}
		mode, err := fetchRemediationModeCtx(ctx)
		mu.Lock()
		result.remMode = mode
		result.remErr = err
		mu.Unlock()
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		select {
		case <-ctx.Done():
			setErr(&result.pendErr, ctx.Err())
			return
		default:
		}
		pending, err := fetchApprovalsCtx(ctx, 30)
		mu.Lock()
		result.pending = pending
		result.pendErr = err
		mu.Unlock()
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		select {
		case <-ctx.Done():
			setErr(&result.logsErr, ctx.Err())
			return
		default:
		}
		logs, err := fetchAgentLogs(80)
		mu.Lock()
		result.logs = logs
		result.logsErr = err
		mu.Unlock()
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		select {
		case <-ctx.Done():
			setErr(&result.healthErr, ctx.Err())
			return
		default:
		}
		scores, err := fetchHealthScoresCtx(ctx, "")
		mu.Lock()
		result.healthScores = scores
		result.healthErr = err
		mu.Unlock()
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		select {
		case <-ctx.Done():
			setErr(&result.healthErr, ctx.Err())
			return
		default:
		}
		sum, err := fetchHealthSummaryCtx(ctx)
		mu.Lock()
		result.healthSum = sum
		if err != nil {
			result.healthErr = err
		}
		mu.Unlock()
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		select {
		case <-ctx.Done():
			setErr(&result.accErr, ctx.Err())
			return
		default:
		}
		snap, err := fetchAccuracyCtx(ctx)
		mu.Lock()
		result.accSnap = snap
		if err != nil {
			result.accErr = err
		}
		mu.Unlock()
	}()

	wg.Wait()
	result.lastUpdate = time.Now()
	return result
}
