package api

import (
	"context"
	"fmt"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

type Store interface {
	ListAnomalies(limit int) ([]models.AnomalyRecord, error)
	LoadConfig() (*models.AgentConfig, error)
	ListAuditRecords(limit int) ([]models.AuditRecord, error)
}

type Server struct {
	store    Store
	server   *http.Server
	startAt  time.Time
	scrapes atomic.Int64
}

func NewServer(store Store, addr string) *Server {
	mux := http.NewServeMux()
	s := &Server{
		store:   store,
		startAt: time.Now(),
	}
	s.registerRoutes(mux)
	s.server = &http.Server{
		Addr:    addr,
		Handler: withMiddleware(mux),
	}
	return s
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
