package api

import (
	"net/http"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var scrapesTotal = prometheus.NewCounter(prometheus.CounterOpts{
	Namespace: "kubewise",
	Name:      "scrapes_total",
	Help:      "Total metric scrape cycles completed by the agent.",
})

func init() {
	prometheus.MustRegister(scrapesTotal)
}

func (s *Server) registerMetrics(mux *http.ServeMux) {
	mux.Handle("GET /metrics", promhttp.Handler())
}
