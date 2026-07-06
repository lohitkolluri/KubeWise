package agent

import (
	"context"
	"fmt"
	"log"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/collector"
	"github.com/lohitkolluri/KubeWise/internal/agent/forecaster"
	"github.com/lohitkolluri/KubeWise/internal/agent/gate"
	"github.com/lohitkolluri/KubeWise/internal/agent/llm"
	"github.com/lohitkolluri/KubeWise/internal/agent/predictor"
	"github.com/lohitkolluri/KubeWise/internal/agent/remediator"
	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/internal/api"
	k8sclient "github.com/lohitkolluri/KubeWise/pkg/k8s"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

const remediationTimeout = 2 * time.Minute

// Agent is the main orchestration loop: collect → detect → gate → store → correlate → remediate.
type Agent struct {
	store              *store.Store
	collector          *collector.PrometheusCollector
	resourcesCollector *collector.ResourcesCollector
	eventsCollector    *collector.EventsCollector
	predictor          *predictor.Predictor
	forecaster         *forecaster.Client
	correlator         *remediator.Correlator
	anomalyGate        *gate.AnomalyGate
	apiServer          *api.Server
	cfg                *models.AgentConfig
	interval           time.Duration
	apiAddr            string
	scrapes            int64
	anomalySeq         uint64
	mu                 sync.Mutex
	stopOnce           sync.Once
	stopCh             chan struct{}
	k8sCancel          context.CancelFunc
}

// NewAgent creates and wires the complete agent pipeline.
func NewAgent(s *store.Store, cfg *models.AgentConfig, interval time.Duration, llmKey, llmModel string, remCfg remediator.RemediationConfig, forecasterAddr, apiAddr string) (*Agent, error) {
	if cfg == nil {
		cfg = &models.AgentConfig{}
	}
	if apiAddr == "" {
		apiAddr = ":8080"
	}
	if remCfg.Mode == "auto" && remCfg.DryRun {
		log.Printf("agent: warning: remediation mode=auto with dry_run=true — actions will not execute")
	}

	col, err := collector.NewPrometheusCollector(cfg.PrometheusAddress, s)
	if err != nil {
		return nil, fmt.Errorf("create collector: %w", err)
	}

	var fcast *forecaster.Client
	if forecasterAddr != "" {
		fcast, err = forecaster.NewClient(forecasterAddr, 30*time.Second)
		if err != nil {
			log.Printf("agent: forecaster sidecar unavailable (%s): %v", forecasterAddr, err)
			fcast = nil
		}
	}

	predCfg := predictor.DefaultScorerConfig()
	pred := predictor.NewPredictor(predCfg)
	predictor.SetScrapeInterval(interval)
	pred.LoadPatternHistory(s, predictor.MaxPatternHistory)
	pred.AddPattern(&predictor.CrashLoopPattern{})
	pred.AddPattern(&predictor.OOMPattern{})
	pred.AddPattern(&predictor.DegradationPattern{})

	llmClient := llm.NewClient(llmKey, llmModel)

	exec, err := remediator.NewK8sExecutor(remCfg.DryRun)
	if err != nil {
		log.Printf("agent: k8s executor unavailable (not in-cluster?): %v", err)
		exec = nil
	}

	if cfg.Remediation.MinConfidence > 0 {
		remCfg.MinConfidence = cfg.Remediation.MinConfidence
	}
	if cfg.Remediation.RateLimit > 0 {
		remCfg.RateLimit = cfg.Remediation.RateLimit
	}

	corr := remediator.NewCorrelator(llmClient, exec, s, remCfg)

	gateCfg := gate.DefaultConfig()
	gateCfg.ScrapeInterval = interval
	ag := gate.NewGate(gateCfg)

	apiSrv := api.NewServer(s, apiAddr)

	a := &Agent{
		store:       s,
		collector:   col,
		predictor:   pred,
		forecaster:  fcast,
		correlator:  corr,
		anomalyGate: ag,
		apiServer:   apiSrv,
		cfg:         cfg,
		stopCh:      make(chan struct{}),
		interval:    interval,
		apiAddr:     apiAddr,
	}

	if k8s, kerr := k8sclient.NewInCluster(); kerr == nil {
		cs := k8s.Clientset()
		a.resourcesCollector = collector.NewResourcesCollector(cs)
		a.eventsCollector = collector.NewEventsCollector(cs, "")

		k8sCtx, k8sCancel := context.WithCancel(context.Background())
		a.k8sCancel = k8sCancel
		go a.resourcesCollector.Run(k8sCtx)
		go a.watchK8sEvents(k8sCtx)
		log.Printf("agent: k8s collectors started")
	} else {
		log.Printf("agent: k8s collectors unavailable: %v", kerr)
	}

	return a, nil
}

func (a *Agent) watchK8sEvents(ctx context.Context) {
	if a.eventsCollector == nil {
		return
	}
	ch := a.eventsCollector.WatchEvents(ctx)
	for record := range ch {
		a.persistEventAnomaly(record)
	}
}

func (a *Agent) persistEventAnomaly(record collector.EventRecord) {
	now := time.Now()
	name := record.InvolvedObject
	if idx := strings.LastIndex(name, "/"); idx >= 0 {
		name = name[idx+1:]
	}
	entity := models.FormatEntity(record.Namespace, name)
	score := 1.0

	result := a.anomalyGate.Filter(entity, "k8s_event", score, "pattern", now)
	if !result.Pass {
		log.Printf("agent: gate dropped k8s event entity=%s reason=%s", entity, result.Reason)
		return
	}

	anomaly := &models.AnomalyRecord{
		ID:         a.nextAnomalyID("event"),
		Entity:     entity,
		Namespace:  record.Namespace,
		MetricName: "k8s_event",
		Score:      score,
		Pattern:    record.Reason,
		Status:     models.AnomalyStatusDetected,
		DetectedAt: &now,
	}
	if _, err := a.store.UpsertOpenAnomaly(anomaly); err != nil {
		log.Printf("agent: save event anomaly: %v", err)
	}
}

// Run starts the agent loop and API server. Blocks until Stop is called.
func (a *Agent) Run() error {
	go func() {
		log.Printf("agent: API server listening on %s", a.apiAddr)
		if err := a.apiServer.Serve(); err != nil {
			log.Printf("agent: api server error: %v", err)
		}
	}()

	if a.resourcesCollector != nil {
		go func() {
			if a.resourcesCollector.WaitForSync() {
				log.Printf("agent: resource informers synced")
			}
		}()
	}

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
	a.stopOnce.Do(func() {
		if a.k8sCancel != nil {
			a.k8sCancel()
		}
		if a.forecaster != nil {
			a.forecaster.Close()
		}
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := a.apiServer.Shutdown(shutdownCtx); err != nil {
			log.Printf("agent: api shutdown error: %v", err)
		}
		close(a.stopCh)
	})
}

func (a *Agent) nextAnomalyID(prefix string) string {
	seq := atomic.AddUint64(&a.anomalySeq, 1)
	return fmt.Sprintf("%s-%d-%d", prefix, time.Now().UnixNano(), seq)
}

func (a *Agent) buildResourceSnapshot() predictor.ResourceSnapshot {
	snap := predictor.ResourceSnapshot{}
	if a.resourcesCollector == nil || !a.resourcesCollector.HasSynced() {
		return snap
	}
	for _, p := range a.resourcesCollector.GetFailingPods() {
		snap.FailingPods = append(snap.FailingPods, models.FormatEntity(p.Namespace, p.Name))
	}
	for _, n := range a.resourcesCollector.GetUnhealthyNodes() {
		snap.UnhealthyNodes = append(snap.UnhealthyNodes, n.Name)
	}
	for _, p := range a.resourcesCollector.GetPodResources() {
		snap.PodResources = append(snap.PodResources, predictor.PodResource{
			Name:      p.Name,
			Namespace: p.Namespace,
			MemLimit:  p.MemLimitBytes,
		})
	}
	return snap
}

func (a *Agent) persistPrediction(p models.PredictionResult, prefix string, now time.Time, scrapeNum int64) {
	entity := models.FormatEntity(p.Namespace, p.Entity)
	if p.Namespace == "" {
		entity = p.Entity
	}

	if p.Score < 0.3 {
		a.anomalyGate.ObserveScore(entity, p.MetricName, p.Score, now)
		return
	}

	result := a.anomalyGate.Filter(entity, p.MetricName, p.Score, p.Type, now)
	if !result.Pass {
		log.Printf("agent[%d]: gate dropped %s anomaly entity=%s score=%.2f reason=%s",
			scrapeNum, p.Type, entity, p.Score, result.Reason)
		return
	}

	pattern := p.Type
	if p.Type == "pattern" {
		pattern = p.MetricName
	}

	anomaly := &models.AnomalyRecord{
		ID:         a.nextAnomalyID(prefix),
		Entity:     entity,
		Namespace:  p.Namespace,
		MetricName: p.MetricName,
		Score:      p.Score,
		Pattern:    pattern,
		Status:     models.AnomalyStatusDetected,
		DetectedAt: &now,
	}
	if _, err := a.store.UpsertOpenAnomaly(anomaly); err != nil {
		log.Printf("agent[%d]: save anomaly: %v", scrapeNum, err)
	}
}

func (a *Agent) runOnce() {
	scrapeCtx, scrapeCancel := context.WithTimeout(context.Background(), a.interval)
	defer scrapeCancel()

	a.mu.Lock()
	a.scrapes++
	scrapeNum := a.scrapes
	a.mu.Unlock()

	metrics, err := a.collector.CollectMetrics(scrapeCtx)
	if err != nil && len(metrics) == 0 {
		log.Printf("agent[%d]: collect error: %v", scrapeNum, err)
		return
	}
	if err != nil {
		log.Printf("agent[%d]: partial collect error: %v", scrapeNum, err)
	}
	log.Printf("agent[%d]: collected %d metrics", scrapeNum, len(metrics))

	a.apiServer.IncrementScrapes()
	a.anomalyGate.PruneStale(time.Now(), 24*time.Hour)

	predMetrics := toPredictorMetrics(metrics)

	predictions, err := a.predictor.Run(predMetrics)
	if err != nil {
		log.Printf("agent[%d]: predict error: %v", scrapeNum, err)
	}

	now := time.Now()
	for _, p := range predictions {
		a.persistPrediction(p, "anomaly", now, scrapeNum)
	}

	events, err := a.store.ListAnomalies(20)
	if err != nil {
		log.Printf("agent[%d]: list anomalies error: %v", scrapeNum, err)
	}

	enrichedMetrics := a.predictor.PreparePatternMetrics(predMetrics)
	resources := a.buildResourceSnapshot()
	patternResults := a.predictor.RunPatterns(enrichedMetrics, events, resources)
	for _, pr := range patternResults {
		a.persistPrediction(pr, "pattern", now, scrapeNum)
	}

	log.Printf("agent[%d]: predictions=%d patterns=%d", scrapeNum, len(predictions), len(patternResults))

	if a.forecaster != nil && len(metrics) > 0 {
		a.runForecast(scrapeCtx, metrics, scrapeNum)
	}

	remCtx, remCancel := context.WithTimeout(context.Background(), remediationTimeout)
	defer remCancel()
	if err := a.correlator.RunOnce(remCtx); err != nil {
		log.Printf("agent[%d]: remediation error: %v", scrapeNum, err)
	}
}

func (a *Agent) runForecast(ctx context.Context, metrics []collector.MetricResult, scrapeNum int64) {
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
				continue
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
		}
	}
}

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
