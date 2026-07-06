package api

import (
	"context"
	"fmt"
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
	ListAuditRecords(limit int) ([]models.AuditRecord, error)
	GetLatestPredictions() ([]models.PredictionResult, error)
}

type Server struct {
	store      Store
	server     *http.Server
	startAt    time.Time
	scrapes    atomic.Int64
	gateStats  atomic.Value // gate.Stats
	apiToken   string
}

func NewServer(store Store, addr string) *Server {
	mux := http.NewServeMux()
	s := &Server{
		store:    store,
		startAt:  time.Now(),
		apiToken: os.Getenv("KUBEWISE_API_TOKEN"),
	}
	s.gateStats.Store(gate.Stats{})
	s.registerRoutes(mux)
	s.server = &http.Server{
		Addr:              addr,
		Handler:           withMiddleware(mux, s.apiToken),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	return s
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
