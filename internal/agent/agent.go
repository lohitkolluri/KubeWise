package agent

import (
	"context"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/collector"
	"github.com/lohitkolluri/KubeWise/internal/agent/forecaster"
	"github.com/lohitkolluri/KubeWise/internal/agent/llm"
	"github.com/lohitkolluri/KubeWise/internal/agent/predictor"
	"github.com/lohitkolluri/KubeWise/internal/agent/remediator"
	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/internal/api"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// Agent is the main orchestration loop: collect → predict → correlate → remediate.
type Agent struct {
	store      *store.Store
	collector  *collector.PrometheusCollector
	predictor  *predictor.Predictor
	forecaster *forecaster.Client
	correlator *remediator.Correlator
	apiServer  *api.Server
	cfg        *models.AgentConfig
	interval   time.Duration
	scrapes    int64
	mu         sync.Mutex
	stopCh     chan struct{}
}

// NewAgent creates and wires the complete agent pipeline.
// forecasterAddr is the gRPC address of the Python forecasting sidecar; empty means skip.
func NewAgent(s *store.Store, promAddr string, interval time.Duration, llmKey, llmModel string, remCfg remediator.RemediationConfig, forecasterAddr string) (*Agent, error) {
	// Collector
	col, err := collector.NewPrometheusCollector(promAddr, s)
	if err != nil {
		return nil, fmt.Errorf("create collector: %w", err)
	}

	// Optional Tier-2 forecasting sidecar
	var fcast *forecaster.Client
	if forecasterAddr != "" {
		fcast, err = forecaster.NewClient(forecasterAddr, 30*time.Second)
		if err != nil {
			log.Printf("agent: forecaster sidecar unavailable (%s): %v", forecasterAddr, err)
			fcast = nil
		}
	}

	// Predictor
	pred := predictor.NewPredictor(predictor.DefaultScorerConfig())
	pred.AddPattern(&predictor.CrashLoopPattern{})
	pred.AddPattern(&predictor.OOMPattern{})
	pred.AddPattern(&predictor.DegradationPattern{})

	// LLM client
	llmClient := llm.NewClient(llmKey, llmModel)

	// K8s executor
	exec, err := remediator.NewK8sExecutor(remCfg.DryRun)
	if err != nil {
		log.Printf("agent: k8s executor unavailable (not in-cluster?): %v", err)
		exec = nil
	}

	// Correlator
	corr := remediator.NewCorrelator(llmClient, exec, s, remCfg)

	// API server
	apiAddr := ":8080"
	apiSrv := api.NewServer(s, apiAddr)

	return &Agent{
		store:      s,
		collector:  col,
		predictor:  pred,
		forecaster: fcast,
		correlator: corr,
		apiServer:  apiSrv,
		stopCh:     make(chan struct{}),
		interval:   interval,
	}, nil
}

// Run starts the agent loop and API server. Blocks until Stop is called.
func (a *Agent) Run() error {
	// Start API server
	go func() {
		log.Printf("agent: API server listening on :8080")
		if err := a.apiServer.Serve(); err != nil {
			log.Printf("agent: api server error: %v", err)
		}
	}()

	// Main collection loop
	ticker := time.NewTicker(a.interval)
	defer ticker.Stop()

	log.Printf("agent: started, scrape interval %s", a.interval)

	for {
		select {
		case <-ticker.C:
			a.runOnce()
		case <-a.stopCh:
			log.Println("agent: stopping")
			return nil
		}
	}
}

// Stop signals the agent loop to stop.
func (a *Agent) Stop() {
	if a.forecaster != nil {
		a.forecaster.Close()
	}
	close(a.stopCh)
}

// runOnce executes a single collection → prediction → remediation cycle.
func (a *Agent) runOnce() {
	ctx, cancel := context.WithTimeout(context.Background(), a.interval)
	defer cancel()

	a.mu.Lock()
	a.scrapes++
	scrapeNum := a.scrapes
	a.mu.Unlock()

	// 1. Collect metrics
	metrics, err := a.collector.CollectMetrics(ctx)
	if err != nil {
		log.Printf("agent[%d]: collect error: %v", scrapeNum, err)
		return
	}
	log.Printf("agent[%d]: collected %d metrics", scrapeNum, len(metrics))

	// Update API server scrape count
	a.apiServer.IncrementScrapes()

	// 2. Predict anomalies (statistical — Tier 1)
	predictions, err := a.predictor.Run(toPredictorMetrics(metrics))
	if err != nil {
		log.Printf("agent[%d]: predict error: %v", scrapeNum, err)
	}

	// 3. Save high-confidence predictions as anomalies
	now := time.Now()
	for _, p := range predictions {
		if p.Score >= 0.3 {
			anomalyID := fmt.Sprintf("anomaly-%d", now.UnixNano())
			anomaly := &models.AnomalyRecord{
				ID:         anomalyID,
				Entity:     p.Entity,
				Namespace:  p.Namespace,
				MetricName: p.Type,
				Score:      p.Score,
				Pattern:    p.Type,
				Status:     "detected",
				DetectedAt: &now,
			}
			if err := a.store.SaveAnomaly(anomaly); err != nil {
				log.Printf("agent[%d]: save anomaly: %v", scrapeNum, err)
			}
		}
	}

	// 4. Run pattern matchers
	events, _ := a.store.ListAnomalies(10)
	resources := predictor.ResourceSnapshot{}
	patternResults := a.predictor.RunPatterns(toPredictorMetrics(metrics), events, resources)
	for _, pr := range patternResults {
		if pr.Score >= 0.3 && pr.Namespace != "" {
			anomalyID := fmt.Sprintf("pattern-%d", now.UnixNano())
			anomaly := &models.AnomalyRecord{
				ID:         anomalyID,
				Entity:     pr.Entity,
				Namespace:  pr.Namespace,
				Score:      pr.Score,
				Pattern:    pr.Type,
				Status:     "detected",
				DetectedAt: &now,
			}
			if err := a.store.SaveAnomaly(anomaly); err != nil {
				log.Printf("agent[%d]: save pattern anomaly: %v", scrapeNum, err)
			}
		}
	}

	log.Printf("agent[%d]: predictions=%d patterns=%d", scrapeNum, len(predictions), len(patternResults))

	// 2b. Run Tier-2 forecasting sidecar (non-blocking, best-effort)
	if a.forecaster != nil && len(metrics) > 0 {
		a.runForecast(ctx, metrics, scrapeNum)
	}

	// 5. Run remediation correlator
	if err := a.correlator.RunOnce(ctx); err != nil {
		log.Printf("agent[%d]: remediation error: %v", scrapeNum, err)
	}
}

// runForecast calls the Tier-2 forecasting sidecar for the first metric batch.
// Best-effort: errors are logged and do not affect the scrape loop.
func (a *Agent) runForecast(ctx context.Context, metrics []collector.MetricResult, scrapeNum int64) {
	// Forecast the first metric with enough data points.
	for _, m := range metrics {
		if len(m.Values) >= 10 {
			values := make([]float64, 0, len(m.Values))
			timestamps := make([]float64, 0, len(m.Values))
			for _, p := range m.Values {
				values = append(values, p.Value)
				timestamps = append(timestamps, float64(p.Timestamp.Unix()))
			}

			resp, err := a.forecaster.Forecast(ctx, &forecaster.ForecastRequest{
				MetricName:      m.Name,
				Values:          values,
				Timestamps:      timestamps,
				Horizon:         12,
				IntervalSeconds: a.interval.Seconds(),
			})
			if err != nil {
				log.Printf("agent[%d]: forecast error for %s: %v", scrapeNum, m.Name, err)
				break
			}
			if resp.Status == "ok" {
				log.Printf("agent[%d]: forecast %s -> %d points (last: %.2f [%.2f, %.2f])",
					scrapeNum, m.Name, len(resp.Points),
					resp.Points[len(resp.Points)-1].Value,
					resp.Points[len(resp.Points)-1].LowerBound,
					resp.Points[len(resp.Points)-1].UpperBound,
				)
			} else {
				log.Printf("agent[%d]: forecast error for %s: %s", scrapeNum, m.Name, resp.ErrorMessage)
			}
			break // one forecast per cycle for now
		}
	}
}

// toPredictorMetrics converts collector MetricResults to predictor MetricResults.
func toPredictorMetrics(metrics []collector.MetricResult) []predictor.MetricResult {
	result := make([]predictor.MetricResult, 0, len(metrics))
	for _, m := range metrics {
		values := make([]predictor.MetricPoint, 0, len(m.Values))
		for _, v := range m.Values {
			values = append(values, predictor.MetricPoint{
				Timestamp: v.Timestamp,
				Value:     v.Value,
				Labels:    v.Labels,
			})
		}
		result = append(result, predictor.MetricResult{
			Name:   m.Name,
			Values: values,
		})
	}
	return result
}
