package api

import (
	"context"
	"fmt"
	"net/http"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

type Store interface {
	ListAnomalies(limit int) ([]models.AnomalyRecord, error)
	LoadConfig() (*models.AgentConfig, error)
}

type Server struct {
	store    Store
	server   *http.Server
	startAt  time.Time
	scrapes  int64
}

func NewServer(store Store, addr string) *Server {
	mux := http.NewServeMux()
	s := &Server{
		store:   store,
		startAt: time.Now(),
	}
	s.registerRoutes(mux)
	return &Server{
		store:   store,
		startAt: time.Now(),
		server: &http.Server{
			Addr:    addr,
			Handler: withMiddleware(mux),
		},
	}
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
	s.scrapes++
}

func (s *Server) uptime() time.Duration {
	return time.Since(s.startAt)
}
