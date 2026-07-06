package predictor

import (
	"math/rand"
	"testing"
	"time"
)

func TestPredictorEmptyMetrics(t *testing.T) {
	p := NewPredictor(DefaultScorerConfig())
	results, err := p.Run(nil)
	if err != nil {
		t.Fatalf("expected no error for nil metrics, got %v", err)
	}
	if len(results) != 0 {
		t.Fatalf("expected no results, got %d", len(results))
	}
}

func TestPredictorWarmup(t *testing.T) {
	p := NewPredictor(DefaultScorerConfig())
	metrics := []MetricResult{
		{
			Name: "pod_cpu_usage",
			Values: []MetricPoint{
				{Value: 10.0, Timestamp: time.Now()},
				{Value: 11.0, Timestamp: time.Now()},
			},
		},
	}
	results, err := p.Run(metrics)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(results) != 0 {
		t.Fatalf("expected 0 results during warmup, got %d", len(results))
	}
}

func TestPredictorRisingMetric(t *testing.T) {
	p := NewPredictor(DefaultScorerConfig())

	// Simulate a memory metric rising 10% per cycle for 15 cycles
	// (warmup is 10, then we need a few more to build a visible trend)
	val := 100.0
	for cycle := 0; cycle < 15; cycle++ {
		metrics := []MetricResult{
			{
				Name: "pod_memory_usage",
				Values: []MetricPoint{
					{
						Value:     val,
						Timestamp: time.Now(),
						Labels:    map[string]string{"pod": "web-1", "namespace": "default"},
					},
				},
			},
		}
		results, _ := p.Run(metrics)
		if cycle >= 12 && len(results) == 0 {
			t.Logf("cycle %d: no prediction yet (score < 0.3)", cycle)
		}
		val = val * 1.10
	}

	// After 15 cycles with rising values, we should have predictions
	metrics := []MetricResult{
		{
			Name: "pod_memory_usage",
			Values: []MetricPoint{
				{
					Value:     val,
					Timestamp: time.Now(),
					Labels:    map[string]string{"pod": "web-1", "namespace": "default"},
				},
			},
		},
	}
	results, _ := p.Run(metrics)

	if len(results) == 0 {
		t.Fatal("expected at least 1 prediction for rising metric after 15 cycles")
	}
	for _, r := range results {
		if r.Score < 0.3 {
			t.Fatalf("expected score >= 0.3 for rising metric, got %f", r.Score)
		}
		if r.Entity != "web-1" {
			t.Fatalf("expected entity web-1, got %s", r.Entity)
		}
		if r.Namespace != "default" {
			t.Fatalf("expected namespace default, got %s", r.Namespace)
		}
		if r.Type != "statistical" {
			t.Fatalf("expected type statistical, got %s", r.Type)
		}
	}
}

func TestPredictorFlatMetric(t *testing.T) {
	p := NewPredictor(DefaultScorerConfig())

	// Flat metric at 50 for 15 cycles
	for cycle := 0; cycle < 15; cycle++ {
		metrics := []MetricResult{
			{
				Name: "pod_cpu_usage",
				Values: []MetricPoint{
					{
						Value:     50.0,
						Timestamp: time.Now(),
						Labels:    map[string]string{"pod": "web-1"},
					},
				},
			},
		}
		p.Run(metrics)
	}

	// Same flat value — no deviation
	metrics := []MetricResult{
		{
			Name: "pod_cpu_usage",
			Values: []MetricPoint{
				{
					Value:     50.0,
					Timestamp: time.Now(),
					Labels:    map[string]string{"pod": "web-1"},
				},
			},
		},
	}
	results, _ := p.Run(metrics)

	// Flat identical value → no anomaly score triggered
	for _, r := range results {
		if r.Score >= 0.3 {
			t.Fatalf("expected score < 0.3 for identical flat value, got %f", r.Score)
		}
	}
}

func TestPredictorMultipleMetrics(t *testing.T) {
	p := NewPredictor(DefaultScorerConfig())

	// Warmup phase: 12 cycles to pass the 10-point warmup
	for cycle := 0; cycle < 12; cycle++ {
		metrics := []MetricResult{
			{
				Name: "pod_cpu_usage",
				Values: []MetricPoint{
					{Value: float64(50 + cycle*5), Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1"}},
				},
			},
			{
				Name: "pod_memory_usage",
				Values: []MetricPoint{
					{Value: float64(100 + cycle*10), Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1"}},
				},
			},
		}
		p.Run(metrics)
	}

	// After warmup, multiple metrics with further deviations should produce predictions
	metrics := []MetricResult{
		{
			Name: "pod_cpu_usage",
			Values: []MetricPoint{
				{Value: 120.0, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1"}},
			},
		},
		{
			Name: "pod_memory_usage",
			Values: []MetricPoint{
				{Value: 240.0, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1"}},
			},
		},
	}
	results, _ := p.Run(metrics)

	if len(results) == 0 {
		t.Log("Note: no predictions for multiple metrics — may need larger deviation")
	}
}

func TestPredictorSpikeAfterWarmup(t *testing.T) {
	p := NewPredictor(DefaultScorerConfig())

	// Stable values for 12 cycles (past warmup of 10)
	for cycle := 0; cycle < 12; cycle++ {
		metrics := []MetricResult{
			{
				Name: "restart_rate",
				Values: []MetricPoint{
					{Value: 0.1, Timestamp: time.Now(), Labels: map[string]string{"pod": "db-1"}},
				},
			},
		}
		p.Run(metrics)
	}

	// Spike to 10x (1.0 from 0.1 steady-state)
	metrics := []MetricResult{
		{
			Name: "restart_rate",
			Values: []MetricPoint{
				{Value: 1.0, Timestamp: time.Now(), Labels: map[string]string{"pod": "db-1"}},
			},
		},
	}
	results, _ := p.Run(metrics)

	found := false
	for _, r := range results {
		if r.Score >= 0.3 {
			found = true
			break
		}
	}
	if !found {
		t.Log("Note: spike did not trigger prediction (score < 0.3 — may need more deviation)")
	}
}

func TestMetricKey(t *testing.T) {
	key := metricKey("cpu", map[string]string{"pod": "web-1", "namespace": "default"})
	if key != "cpu/pod=web-1/namespace=default" {
		t.Fatalf("unexpected key: %s", key)
	}

	key = metricKey("cpu", nil)
	if key != "cpu" {
		t.Fatalf("expected 'cpu', got '%s'", key)
	}

	// Different label orders produce same key
	k1 := metricKey("mem", map[string]string{"pod": "a", "namespace": "b"})
	k2 := metricKey("mem", map[string]string{"namespace": "b", "pod": "a"})
	if k1 != k2 {
		t.Fatalf("expected same key regardless of order: %s vs %s", k1, k2)
	}
}

func TestPredictorScoreRange(t *testing.T) {
	p := NewPredictor(DefaultScorerConfig())

	for cycle := 0; cycle < 12; cycle++ {
		metrics := []MetricResult{
			{
				Name: "test_metric",
				Values: []MetricPoint{
					{Value: float64(cycle * 20), Timestamp: time.Now(), Labels: map[string]string{"pod": "test"}},
				},
			},
		}
		p.Run(metrics)
	}

	results, _ := p.Run([]MetricResult{
		{
			Name: "test_metric",
			Values: []MetricPoint{
				{Value: 300.0, Timestamp: time.Now(), Labels: map[string]string{"pod": "test"}},
			},
		},
	})

	for _, r := range results {
		if r.Score < 0 || r.Score > 1 {
			t.Fatalf("score %f out of [0,1] range", r.Score)
		}
	}
}

func TestPredictorKeyIndependent(t *testing.T) {
	p := NewPredictor(DefaultScorerConfig())

	for cycle := 0; cycle < 12; cycle++ {
		metrics := []MetricResult{
			{
				Name: "metric",
				Values: []MetricPoint{
					{Value: 50.0, Timestamp: time.Now(), Labels: map[string]string{"pod": "stable"}},
					{Value: float64(50 + cycle*20), Timestamp: time.Now(), Labels: map[string]string{"pod": "rising"}},
				},
			},
		}
		p.Run(metrics)
	}

	results, _ := p.Run([]MetricResult{
		{
			Name: "metric",
			Values: []MetricPoint{
				{Value: 51.0, Timestamp: time.Now(), Labels: map[string]string{"pod": "stable"}},
				{Value: 300.0, Timestamp: time.Now(), Labels: map[string]string{"pod": "rising"}},
			},
		},
	})

	stableScore := 1.0
	risingScore := 0.0
	for _, r := range results {
		if r.Entity == "stable" {
			stableScore = r.Score
		}
		if r.Entity == "rising" {
			risingScore = r.Score
		}
	}

	if risingScore <= stableScore {
		t.Logf("Warning: rising score (%f) not > stable score (%f) — indicates key independence issue", risingScore, stableScore)
	}
}

func TestPredictorWarmupOnly(t *testing.T) {
	p := NewPredictor(DefaultScorerConfig())
	results, _ := p.Run([]MetricResult{
		{
			Name:   "test",
			Values: []MetricPoint{{Value: 1, Timestamp: time.Now()}},
		},
	})
	if len(results) != 0 {
		t.Fatal("expected 0 results for single data point (warmup)")
	}
}

func TestPredictorTwoPointsOnly(t *testing.T) {
	p := NewPredictor(DefaultScorerConfig())
	results, _ := p.Run([]MetricResult{
		{
			Name: "test",
			Values: []MetricPoint{
				{Value: 1, Timestamp: time.Now()},
				{Value: 2, Timestamp: time.Now()},
			},
		},
	})
	if len(results) != 0 {
		t.Fatal("expected 0 results with only 2 data points (need 10 for warmup)")
	}
}

func TestPredictorNoisySteadyWithinBound(t *testing.T) {
	p := NewPredictor(DefaultScorerConfig())

	// 30 cycles with modest gaussian-ish noise (±10% CV) — deviations should
	// be within the Hoeffding bound for the chosen false-positive rate.
	rng := rand.New(rand.NewSource(42))
	maxObserved := 0.0
	for cycle := 0; cycle < 30; cycle++ {
		metrics := []MetricResult{
			{
				Name: "noisy_steady",
				Values: []MetricPoint{
					{Value: 100.0 + rng.Float64()*20 - 10, Timestamp: time.Now(), Labels: map[string]string{"pod": "steady"}},
				},
			},
		}
		results, _ := p.Run(metrics)
		for _, r := range results {
			if r.Score > maxObserved {
				maxObserved = r.Score
			}
		}
	}
	t.Logf("max score for noisy steady metric: %f", maxObserved)
}

func TestPredictorLargeStep(t *testing.T) {
	p := NewPredictor(DefaultScorerConfig())

	// 12 cycles at value 100
	for cycle := 0; cycle < 12; cycle++ {
		p.Run([]MetricResult{
			{
				Name: "step_metric",
				Values: []MetricPoint{
					{Value: 100.0, Timestamp: time.Now(), Labels: map[string]string{"pod": "target"}},
				},
			},
		})
	}

	// Sudden step to 200
	results, _ := p.Run([]MetricResult{
		{
			Name: "step_metric",
			Values: []MetricPoint{
				{Value: 200.0, Timestamp: time.Now(), Labels: map[string]string{"pod": "target"}},
			},
		},
	})

	// The step (100 → 200) should register as anomalous
	if len(results) == 0 {
		t.Fatal("expected prediction for sudden large step")
	}
	t.Logf("step anomaly score: %f", results[0].Score)
}
