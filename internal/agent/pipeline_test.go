package agent

import (
	"testing"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/gate"
	"github.com/lohitkolluri/KubeWise/internal/agent/predictor"
)

// TestPipeline_StatisticalSpikeBlockedBeforeLLM verifies that a single moderate
// statistical spike is detected by the predictor but blocked by the gate before
// it would be persisted and sent to the LLM correlator.
func TestPipeline_StatisticalSpikeBlockedBeforeLLM(t *testing.T) {
	pred := predictor.NewPredictor(predictor.DefaultScorerConfig())
	ag := gate.NewGate(gate.Config{
		ScrapeInterval: 30 * time.Second,
		SustainCount:   3,
		Threshold:      0.3,
	})

	now := time.Now()
	entity := "web-1"
	metric := "pod_cpu_usage"

	// Warmup with stable values.
	for i := 0; i < 12; i++ {
		_, _ = pred.Run([]predictor.MetricResult{{
			Name: metric,
			Values: []predictor.MetricPoint{{
				Value:     50.0,
				Timestamp: now.Add(time.Duration(i) * 30 * time.Second),
				Labels:    map[string]string{"pod": entity},
			}},
		}})
	}

	// Moderate elevation — predictor may flag it, gate should block without sustainment.
	results, err := pred.Run([]predictor.MetricResult{{
		Name: metric,
		Values: []predictor.MetricPoint{{
			Value:     65.0,
			Timestamp: now.Add(12 * 30 * time.Second),
			Labels:    map[string]string{"pod": entity},
		}},
	}})
	if err != nil {
		t.Fatalf("predict error: %v", err)
	}

	if len(results) == 0 {
		t.Skip("predictor did not flag spike in this run; gate test not applicable")
	}

	persisted := 0
	for _, r := range results {
		if r.Score < 0.3 {
			continue
		}
		pass, reason := ag.ShouldPersist(r.Entity, r.MetricName, r.Score, r.Type, now.Add(12*30*time.Second))
		if pass {
			persisted++
			t.Logf("unexpected pass: entity=%s score=%.2f reason=%s", r.Entity, r.Score, reason)
		}
	}

	if persisted > 0 {
		t.Fatalf("expected 0 anomalies to pass gate on single spike, got %d", persisted)
	}
}

// TestPipeline_SustainedAnomalyReachesLLM verifies that sustained elevation
// passes the gate and would be persisted for LLM analysis.
func TestPipeline_SustainedAnomalyReachesLLM(t *testing.T) {
	ag := gate.NewGate(gate.Config{
		ScrapeInterval: 30 * time.Second,
		SustainCount:   3,
		Threshold:      0.3,
	})

	now := time.Now()
	entity := "db-1"
	score := 0.4

	for i := 0; i < 2; i++ {
		pass, _ := ag.ShouldPersist(entity, "cpu", score, "statistical", now.Add(time.Duration(i)*30*time.Second))
		if pass {
			t.Fatalf("expected no pass before sustainment at scrape %d", i+1)
		}
	}

	pass, reason := ag.ShouldPersist(entity, "cpu", score, "statistical", now.Add(2*30*time.Second))
	if !pass {
		t.Fatalf("expected pass on 3rd sustained scrape, got reason=%s", reason)
	}
	if reason != "sustainment" {
		t.Fatalf("expected sustainment, got %s", reason)
	}
}

// TestPipeline_PatternBypassReachesLLM verifies predictive pattern matches
// bypass sustainment and would reach the correlator immediately.
func TestPipeline_PatternBypassReachesLLM(t *testing.T) {
	pred := predictor.NewPredictor(predictor.DefaultScorerConfig())
	pred.AddPattern(&predictor.OOMPattern{})
	ag := gate.NewGate(gate.DefaultConfig())

	now := time.Now()
	metrics := []predictor.MetricResult{{
		Name: "pod_memory_usage",
		Values: []predictor.MetricPoint{
			{Value: 0.6 * (1 << 30), Timestamp: now, Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
			{Value: 0.75 * (1 << 30), Timestamp: now.Add(30 * time.Second), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
			{Value: 0.9 * (1 << 30), Timestamp: now.Add(60 * time.Second), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
		},
	}}
	resources := predictor.ResourceSnapshot{
		PodResources: []predictor.PodResource{{Name: "web-1", Namespace: "default", MemLimit: 1 << 30}},
	}

	results := pred.RunPatterns(metrics, nil, resources)
	if len(results) == 0 {
		t.Fatal("expected predictive OOM pattern result")
	}

	pass, reason := ag.ShouldPersist(results[0].Entity, results[0].MetricName, results[0].Score, results[0].Type, now)
	if !pass {
		t.Fatalf("expected pattern to bypass gate, got reason=%s", reason)
	}
	if reason != "pattern_bypass" {
		t.Fatalf("expected pattern_bypass, got %s", reason)
	}
}

// TestPipeline_NoisyGaussianMostlyBlocked estimates how many statistical
// detections from Gaussian noise survive the gate across multiple scrapes.
func TestPipeline_NoisyGaussianMostlyBlocked(t *testing.T) {
	pred := predictor.NewPredictor(predictor.DefaultScorerConfig())
	ag := gate.NewGate(gate.Config{
		ScrapeInterval: 30 * time.Second,
		SustainCount:   3,
		Threshold:      0.3,
	})

	now := time.Now()
	entity := "steady-pod"
	detected := 0
	persisted := 0

	for i := 0; i < 50; i++ {
		// Small noise around 50 — should mostly stay below gate thresholds.
		val := 50.0 + float64((i%7)-3)*0.5
		ts := now.Add(time.Duration(i) * 30 * time.Second)
		results, _ := pred.Run([]predictor.MetricResult{{
			Name: "pod_cpu_usage",
			Values: []predictor.MetricPoint{{
				Value:     val,
				Timestamp: ts,
				Labels:    map[string]string{"pod": entity},
			}},
		}})
		for _, r := range results {
			if r.Score < 0.3 {
				continue
			}
			detected++
			pass, _ := ag.ShouldPersist(r.Entity, r.MetricName, r.Score, r.Type, ts)
			if pass {
				persisted++
			}
		}
	}

	t.Logf("noisy steady: detected=%d persisted=%d (persisted should be << detected)", detected, persisted)
	if detected > 0 && persisted > detected/2 {
		t.Fatalf("gate let too many noisy detections through: %d/%d", persisted, detected)
	}
}
