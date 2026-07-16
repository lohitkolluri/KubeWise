// Package api provides the HTTP API server for the KubeWise agent.
package api

import (
	"context"
	"errors"
	"io"
	"net/http"
	"os"
	"sync/atomic"
	"time"

	"golang.org/x/time/rate"

	"github.com/lohitkolluri/KubeWise/internal/agent/gate"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// Store is the data access interface the API server depends on.
type Store interface {
	ListAnomalies(limit int) ([]models.AnomalyRecord, error)
	LoadConfig() (*models.AgentConfig, error)
	SaveConfig(cfg *models.AgentConfig) error
	ListAuditRecords(limit int) ([]models.AuditRecord, error)
	ListAuditRecordsSince(since time.Time, limit int) ([]models.AuditRecord, error)
	ListAuditRecordsByStatus(status models.AuditStatus, limit int) ([]models.AuditRecord, error)
	GetAuditRecord(id string) (*models.AuditRecord, error)
	GetLatestPredictions() ([]models.PredictionResult, error)
	ComputeAgentStats() (models.AgentStats, error)
	Ping() error

	// Health scores.
	GetLatestHealthScores() ([]models.HealthScore, error)
	GetHealthScoresByNamespace(ns string) ([]models.HealthScore, error)
	GetHealthScoreHistory(entity, namespace string, limit int) ([]models.HealthScore, error)
	ComputeClusterSummary() (*models.ClusterHealthSummary, error)

	// Accuracy snapshots.
	GetLatestAccuracySnapshot() (*models.AccuracySnapshot, error)
	GetAccuracyHistory(limit int) ([]models.AccuracySnapshot, error)

	Backup(w io.Writer) error
}

// Server serves the KubeWise HTTP API with middleware for auth, CORS, and rate limiting.
type Server struct {
	store      Store
	remediator Remediator
	server     *http.Server
	startAt    time.Time
	scrapes    atomic.Int64
	gateStats  atomic.Value // gate.Stats
	apiToken   string
}

// NewServer creates an HTTP API server backed by the given store.
func NewServer(store Store, addr string) (*Server, error) {
	mux := http.NewServeMux()
	apiToken := os.Getenv("KUBEWISE_API_TOKEN")
	corsOrigin := os.Getenv("KUBEWISE_CORS_ORIGIN")
	if corsOrigin == "" {
		corsOrigin = defaultCORSOrigin
	}
	allowUnauth := os.Getenv("KUBEWISE_ALLOW_UNAUTH") == "true"
	requireToken := os.Getenv("KUBEWISE_REQUIRE_API_TOKEN") == "true" || !allowUnauth
	if apiToken == "" && requireToken {
		return nil, errors.New("api: KUBEWISE_API_TOKEN required but not set (set KUBEWISE_ALLOW_UNAUTH=true for local dev)")
	}

	s := &Server{
		store:    store,
		startAt:  time.Now(),
		apiToken: apiToken,
	}
	s.gateStats.Store(gate.Stats{})
	s.registerRoutes(mux)
	s.server = &http.Server{
		Addr: addr,
		Handler: withMiddleware(mux, middlewareConfig{
			apiToken:      apiToken,
			corsOrigin:    corsOrigin,
			requireToken:  requireToken,
			limiter:       rate.NewLimiter(rate.Limit(60/60.0), 10),
			publicLimiter: rate.NewLimiter(rate.Limit(5/60.0), 5),
		}),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	return s, nil
}

// SetRemediator wires the live remediation controller (approvals, mode toggle).
func (s *Server) SetRemediator(r Remediator) {
	s.remediator = r
}

// SetGateStats stores the latest anomaly gate statistics for the /stats endpoint.
func (s *Server) SetGateStats(stats gate.Stats) {
	s.gateStats.Store(stats)
}

// Serve starts the HTTP listener and blocks until the server stops.
func (s *Server) Serve() error {
	if s.server == nil {
		return errors.New("server not initialized")
	}
	return s.server.ListenAndServe()
}

// Shutdown gracefully stops the HTTP server with a context deadline.
func (s *Server) Shutdown(ctx context.Context) error {
	if s.server == nil {
		return nil
	}
	return s.server.Shutdown(ctx)
}

// IncrementScrapes records a metrics scrape for the /stats endpoint.
func (s *Server) IncrementScrapes() {
	s.scrapes.Add(1)
	scrapesTotal.Inc()
}

func (s *Server) uptime() time.Duration {
	return time.Since(s.startAt)
}

func (s *Server) gateStatsSnapshot() gate.Stats {
	if v := s.gateStats.Load(); v != nil {
		if stats, ok := v.(gate.Stats); ok {
			return stats
		}
	}
	return gate.Stats{}
}
