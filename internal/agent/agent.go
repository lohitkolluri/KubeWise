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
	"github.com/lohitkolluri/KubeWise/internal/agent/notify"
	"github.com/lohitkolluri/KubeWise/internal/agent/outcome"
	"github.com/lohitkolluri/KubeWise/internal/agent/predictor"
	"github.com/lohitkolluri/KubeWise/internal/agent/remediator"
	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/internal/api"
	k8sclient "github.com/lohitkolluri/KubeWise/pkg/k8s"
	"github.com/lohitkolluri/KubeWise/pkg/models"
	nsutil "github.com/lohitkolluri/KubeWise/pkg/namespace"
)

const remediationTimeout = 4 * time.Minute
const maxForecastSeriesPerScrape = 32

const healthComputeInterval = 10 // compute health/accuracy every N scrape cycles

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
	outcomeTracker     *outcome.Tracker
	healthComputer     *outcome.HealthComputer
	accuracyComputer   *outcome.AccuracyComputer
	notifier           *notify.Notifier
	apiServer          *api.Server
	cfg                *models.AgentConfig
	interval           time.Duration
	apiAddr            string
	minScore           float64
	scrapes            atomic.Int64
	healthTick         atomic.Int64
	anomalySeq         uint64
	stopOnce           sync.Once
	stopCh             chan struct{}
	runCtx             context.Context
	runCancel          context.CancelFunc
	k8sCancel          context.CancelFunc
}

// NewAgent creates and wires the complete agent pipeline.
func NewAgent(s *store.Store, cfg *models.AgentConfig, interval time.Duration, llmCfg llm.Config, remCfg remediator.RemediationConfig, forecasterAddr, apiAddr string) (*Agent, error) {
	if cfg == nil {
		cfg = &models.AgentConfig{}
	}
	if apiAddr == "" {
		apiAddr = ":8080"
	}
	if remCfg.Mode == "auto" && remCfg.DryRun {
		log.Printf("agent: warning: remediation mode=auto with dry_run=true — actions will not execute")
	}

	col, err := collector.NewPrometheusCollector(cfg.PrometheusAddress, s, cfg.WatchNamespaces)
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

	llmClient, err := llm.NewClient(llmCfg)
	if err != nil {
		return nil, fmt.Errorf("create llm client: %w", err)
	}

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
	if len(cfg.WatchNamespaces) > 0 {
		remCfg.WatchNamespaces = cfg.WatchNamespaces
	}

	corr := remediator.NewCorrelator(llmClient, exec, s, remCfg)
	notifier := notify.New(cfg.Notifications)
	corr.SetNotifier(notifier)

	gateCfg := gate.DefaultConfig()
	gateCfg.ScrapeInterval = interval
	ag := gate.NewGate(gateCfg)

	apiSrv := api.NewServer(s, apiAddr)
	apiSrv.SetRemediator(corr)

	a := &Agent{
		store:            s,
		collector:        col,
		predictor:        pred,
		forecaster:       fcast,
		correlator:       corr,
		anomalyGate:      ag,
		outcomeTracker:   outcome.NewTracker(s),
		healthComputer:   outcome.NewHealthComputer(s),
		accuracyComputer: outcome.NewAccuracyComputer(s),
		notifier:         notifier,
		apiServer:        apiSrv,
		cfg:              cfg,
		stopCh:           make(chan struct{}),
		interval:         interval,
		apiAddr:          apiAddr,
		minScore:         predCfg.MinScore,
	}
	a.runCtx, a.runCancel = context.WithCancel(context.Background())

	if k8s, kerr := k8sclient.NewInCluster(); kerr == nil {
		cs := k8s.Clientset()
		a.resourcesCollector = collector.NewResourcesCollector(cs, cfg.WatchNamespaces)
		a.eventsCollector = collector.NewEventsCollector(cs, "", cfg.WatchNamespaces)

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

	result := a.anomalyGate.Filter(entity, "k8s_event", score, "event", now)
	if !result.Pass {
		log.Printf("agent: gate dropped k8s event entity=%s event=%s reason=%s",
			entity, record.Reason, result.Reason)
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
		if a.resourcesCollector.WaitForSync() {
			log.Printf("agent: resource informers synced")
		} else {
			log.Printf("agent: warning: resource informers not synced before first scrape")
		}
	}

	ticker := time.NewTicker(a.interval)
	defer ticker.Stop()

	log.Printf("agent: started, scrape interval %s", a.interval)

	for {
		select {
		case <-ticker.C:
			a.runOnceSafe()
		case <-a.stopCh:
			log.Println("agent: stopping")
			return nil
		}
	}
}

// runOnceSafe calls runOnce and recovers from panics to prevent agent death.
func (a *Agent) runOnceSafe() {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("agent: recovered panic in scrape cycle: %v", r)
		}
	}()
	a.runOnce()
}

// Stop signals the agent loop to stop.
func (a *Agent) Stop() {
	a.stopOnce.Do(func() {
		if a.runCancel != nil {
			a.runCancel()
		}
		if a.k8sCancel != nil {
			a.k8sCancel()
		}
		if a.forecaster != nil {
			if err := a.forecaster.Close(); err != nil {
				log.Printf("agent: forecaster close error: %v", err)
			}
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
	failing, unhealthy, pods := a.resourcesCollector.Snapshot()
	for _, p := range failing {
		snap.FailingPods = append(snap.FailingPods, models.FormatEntity(p.Namespace, p.Name))
	}
	snap.UnhealthyNodes = append(snap.UnhealthyNodes, unhealthy...)
	for _, p := range pods {
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
	ns := p.Namespace
	if ns == "" {
		ns, _ = models.ParseEntity(entity)
	}
	if !nsutil.InScope(ns, a.cfg.WatchNamespaces) {
		return
	}

	if p.Score < a.minScore {
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
	select {
	case <-a.runCtx.Done():
		return
	default:
	}

	scrapeCtx, scrapeCancel := context.WithTimeout(a.runCtx, a.interval)
	defer scrapeCancel()

	a.scrapes.Add(1)
	scrapeNum := a.scrapes.Load()

	metrics, err := a.collector.CollectMetrics(scrapeCtx)
	if len(metrics) == 0 {
		if err != nil {
			log.Printf("agent[%d]: collect failed: %v", scrapeNum, err)
		} else {
			log.Printf("agent[%d]: no metrics collected, skipping scrape", scrapeNum)
		}
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

	patternCount := 0
	var allPredictions []models.PredictionResult
	allPredictions = append(allPredictions, predictions...)

	events, err := a.store.ListAnomalies(20)
	if err != nil {
		log.Printf("agent[%d]: list anomalies error: %v — skipping pattern pass", scrapeNum, err)
	} else {
		enrichedMetrics := a.predictor.PreparePatternMetrics(predMetrics)
		resources := a.buildResourceSnapshot()
		patternResults := a.predictor.RunPatterns(enrichedMetrics, events, resources)
		patternCount = len(patternResults)
		allPredictions = append(allPredictions, patternResults...)
		for _, pr := range patternResults {
			a.persistPrediction(pr, "pattern", now, scrapeNum)
		}
		if a.outcomeTracker != nil {
			a.outcomeTracker.TrackPatternPredictions(patternResults, now)
		}
		if a.notifier != nil {
			notifyCtx, notifyCancel := context.WithTimeout(a.runCtx, 5*time.Second)
			for _, pr := range patternResults {
				a.notifier.NotifyPrediction(notifyCtx, pr)
			}
			notifyCancel()
		}
	}
	// Only save predictions when there are any — this prevents overwriting
	// active predictions with an empty list when the current cycle's metrics
	// temporarily recede below the anomaly threshold. Previously generated
	// predictions remain visible until replaced by a new non-empty set or
	// explicitly resolved.
	if len(allPredictions) > 0 {
		if err := a.store.SaveLatestPredictions(allPredictions); err != nil {
			log.Printf("agent[%d]: save predictions: %v", scrapeNum, err)
		}
	} else if len(predictions) == 0 && patternCount == 0 {
		// No new predictions this cycle — keep existing ones from previous cycles.
		log.Printf("agent[%d]: no new predictions — preserving existing stored predictions", scrapeNum)
	}

	gateStats := a.anomalyGate.StatsSnapshot()
	a.apiServer.SetGateStats(gateStats)
	log.Printf("agent[%d]: predictions=%d patterns=%d gate_passed=%d gate_dropped=%d",
		scrapeNum, len(predictions), patternCount, gateStats.Passed, gateStats.Dropped)

	if a.outcomeTracker != nil {
		a.outcomeTracker.VerifyPending(a.buildResourceSnapshot(), now)
	}

	if a.forecaster != nil {
		fcastCtx, fcastCancel := context.WithTimeout(a.runCtx, 30*time.Second)
		a.runForecast(fcastCtx, scrapeNum)
		fcastCancel()
	}

	select {
	case <-a.runCtx.Done():
		return
	default:
	}

	remCtx, remCancel := context.WithTimeout(a.runCtx, remediationTimeout)
	defer remCancel()
	if err := a.correlator.RunOnce(remCtx); err != nil {
		log.Printf("agent[%d]: remediation error: %v", scrapeNum, err)
	}

	tick := a.healthTick.Add(1)
	if tick%healthComputeInterval == 0 {
		if a.healthComputer != nil {
			if _, err := a.healthComputer.ComputeAll(); err != nil {
				log.Printf("agent[%d]: health compute error: %v", scrapeNum, err)
			}
			if a.accuracyComputer != nil {
				if _, err := a.accuracyComputer.ComputeSnapshot(7 * 24 * time.Hour); err != nil {
					log.Printf("agent[%d]: accuracy compute error: %v", scrapeNum, err)
				}
			}
		}
	}
}

func (a *Agent) runForecast(ctx context.Context, scrapeNum int64) {
	// Instant PromQL returns cross-sectional snapshots — forecast per stored time series instead.
	names, err := a.store.ListMetricNames()
	if err != nil {
		log.Printf("agent[%d]: forecast list metrics: %v", scrapeNum, err)
		return
	}
	forecasted := 0
	for _, name := range names {
		if forecasted >= maxForecastSeriesPerScrape {
			break
		}
		keys, err := a.store.ListMetricSeries(name)
		if err != nil {
			continue
		}
		for _, key := range keys {
			if forecasted >= maxForecastSeriesPerScrape {
				break
			}
			metricName, labels, err := store.ParseSeriesKey(key)
			if err != nil {
				continue
			}
			pts, err := a.store.GetMetricSeries(metricName, labels, 20)
			if err != nil || len(pts) < 10 {
				continue
			}
			values := make([]float64, len(pts))
			timestamps := make([]float64, len(pts))
			for i, p := range pts {
				values[i] = p.Value
				timestamps[i] = float64(p.TS.Unix())
			}
			resp, err := a.forecaster.Forecast(ctx, &forecaster.ForecastRequest{
				MetricName:      metricName,
				Values:          values,
				Timestamps:      timestamps,
				Labels:          labels,
				Horizon:         12,
				IntervalSeconds: a.interval.Seconds(),
			})
			if err != nil {
				if !benignForecastError(err.Error()) {
					log.Printf("agent[%d]: forecast error for %s: %v", scrapeNum, metricName, err)
				}
				continue
			}
			if resp.Status == "ok" && len(resp.Points) > 0 {
				forecasted++
				last := resp.Points[len(resp.Points)-1]
				log.Printf("agent[%d]: forecast %s -> %d points (last: %.2f [%.2f, %.2f])",
					scrapeNum, metricName, len(resp.Points), last.Value, last.LowerBound, last.UpperBound)
			} else if resp.Status != "ok" && !benignForecastError(resp.ErrorMessage) {
				log.Printf("agent[%d]: forecast error for %s: %s", scrapeNum, metricName, resp.ErrorMessage)
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

// benignForecastError reports whether a forecaster failure is expected for short/young series.
func benignForecastError(msg string) bool {
	msg = strings.ToLower(msg)
	return strings.Contains(msg, "seasonal") ||
		strings.Contains(msg, "need >=") ||
		strings.Contains(msg, "two full seasonal")
}
