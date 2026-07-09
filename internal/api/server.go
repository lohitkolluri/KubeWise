package api

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"sync/atomic"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/gate"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

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
}

type Server struct {
	store        Store
	remediator   Remediator
	server       *http.Server
	startAt      time.Time
	scrapes      atomic.Int64
	gateStats    atomic.Value // gate.Stats
	apiToken     string
	corsOrigin   string
	requireToken bool
}

func NewServer(store Store, addr string) *Server {
	mux := http.NewServeMux()
	apiToken := os.Getenv("KUBEWISE_API_TOKEN")
	corsOrigin := os.Getenv("KUBEWISE_CORS_ORIGIN")
	if corsOrigin == "" {
		corsOrigin = defaultCORSOrigin
	}
	allowUnauth := os.Getenv("KUBEWISE_ALLOW_UNAUTH") == "true"
	requireToken := os.Getenv("KUBEWISE_REQUIRE_API_TOKEN") == "true" || !allowUnauth

	s := &Server{
		store:        store,
		startAt:      time.Now(),
		apiToken:     apiToken,
		corsOrigin:   corsOrigin,
		requireToken: requireToken,
	}
	if apiToken == "" && requireToken {
		log.Printf("api: ERROR: KUBEWISE_API_TOKEN is not set — refusing unauthenticated API (set KUBEWISE_ALLOW_UNAUTH=true for dev)")
	}
	s.gateStats.Store(gate.Stats{})
	s.registerRoutes(mux)
	s.server = &http.Server{
		Addr:              addr,
		Handler:           withMiddleware(mux, middlewareConfig{apiToken: apiToken, corsOrigin: corsOrigin, requireToken: requireToken}),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	return s
}

// SetRemediator wires the live remediation controller (approvals, mode toggle).
func (s *Server) SetRemediator(r Remediator) {
	s.remediator = r
}

func (s *Server) SetGateStats(stats gate.Stats) {
	s.gateStats.Store(stats)
}

func (s *Server) Serve() error {
	if s.server == nil {
		return fmt.Errorf("server not initialized")
	}
	return s.server.ListenAndServe()
}

func (s *Server) Shutdown(ctx context.Context) error {
	if s.server == nil {
		return nil
	}
	return s.server.Shutdown(ctx)
}

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
