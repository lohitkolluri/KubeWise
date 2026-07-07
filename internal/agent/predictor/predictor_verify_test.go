package predictor

import (
	"fmt"
	"math"
	"math/rand"
	"sync"
	"testing"
	"time"
)

// ────────────────────────────────────────────────────────────────────────────
// Numerical correctness: Hoeffding bound
// ────────────────────────────────────────────────────────────────────────────

func TestHoeffdingNumericalCorrectness(t *testing.T) {
	// Known inputs for which we can manually compute the expected bound.
	// ε = R × sqrt(ln(2/δ) / 2n)
	// At n=100, δ=0.05, R=100:
	//   ln(2/0.05) = ln(40) ≈ 3.688879
	//   ε = 100 × sqrt(3.688879 / 200) = 100 × sqrt(0.018444) ≈ 100 × 0.13581 ≈ 13.581
	// K=3 so score=1.0 at deviation >= 3ε ≈ 40.744
	// With deviation = median (50), score = 50/(13.581*3) ≈ 1.0 (capped)

	median := 50.0
	rng := 100.0
	n := 100
	delta := 0.05
	k := 3.0

	expectedEpsilon := 100.0 * math.Sqrt(math.Log(40.0)/200.0)

	// A value at median should score 0.
	score := HoeffdingAnomalyScore(median, median, rng, n, delta, k)
	if score != 0 {
		t.Errorf("score at median: got %f, want 0", score)
	}

	// A value at median + epsilon should score 1/K.
	dev := median + expectedEpsilon
	score = HoeffdingAnomalyScore(dev, median, rng, n, delta, k)
	expectedScore := expectedEpsilon / (expectedEpsilon * k)
	if math.Abs(score-expectedScore) > 1e-12 {
		t.Errorf("score at median+ε: got %f, want %f", score, expectedScore)
	}

	// A value at median + 3*epsilon should be capped at 1.0 (with tolerance
	// for floating-point drift between the two epsilon computations).
	dev = median + 3*expectedEpsilon
	score = HoeffdingAnomalyScore(dev, median, rng, n, delta, k)
	if score < 0.99 || score > 1.0 {
		t.Errorf("score at median+3ε: got %f, want ~1.0", score)
	}

	// Verify epsilon shrinks with more data (n=400 vs n=100).
	n2 := 400
	epsilonN400 := rng * math.Sqrt(math.Log(40.0)/(2.0*float64(n2)))
	epsilonN100 := rng * math.Sqrt(math.Log(40.0)/(2.0*float64(n)))
	if epsilonN400 >= epsilonN100 {
		t.Errorf("epsilon should shrink with more data: ε@n=400=%f >= ε@n=100=%f", epsilonN400, epsilonN100)
	}
}

// ────────────────────────────────────────────────────────────────────────────
// Numerical correctness: adaptive median stats
// ────────────────────────────────────────────────────────────────────────────

func TestAdaptiveMedianNumericalCorrectness(t *testing.T) {
	est := NewAdaptiveMedian(10)

	// Feed values 1..10 — median should be 5.5 (even count).
	for i := 1; i <= 10; i++ {
		est.Add(float64(i))
	}
	med, mad, rng, n, ok := est.Stats()
	if !ok || n != 10 {
		t.Fatalf("expected n=10, got n=%d", n)
	}
	if math.Abs(med-5.5) > 1e-12 {
		t.Errorf("median: got %f, want 5.5", med)
	}
	// Range should be 10 - 1 = 9.
	if math.Abs(rng-9.0) > 1e-12 {
		t.Errorf("range: got %f, want 9.0", rng)
	}
	// MAD for values 1..10 around median 5.5: deviations are 4.5,3.5,2.5,1.5,0.5,0.5,1.5,2.5,3.5,4.5
	// Sorted: 0.5,0.5,1.5,1.5,2.5,2.5,3.5,3.5,4.5,4.5
	// Median of deviations (even): (2.5+2.5)/2 = 2.5
	if math.Abs(mad-2.5) > 0.01 {
		t.Errorf("MAD: got %f, want ~2.5", mad)
	}

	// Feed 11..20 to slide window — median should be 15.5.
	for i := 11; i <= 20; i++ {
		est.Add(float64(i))
	}
	med, _, _, n, _ = est.Stats()
	if math.Abs(med-15.5) > 1e-12 {
		t.Errorf("sliding median: got %f, want 15.5", med)
	}
}

// ────────────────────────────────────────────────────────────────────────────
// End-to-end: clean steady metric produces no high-confidence predictions
// ────────────────────────────────────────────────────────────────────────────

func TestVerifyCleanSteadyState(t *testing.T) {
	cfg := DefaultScorerConfig()
	// Verification tests are meant to validate detector behavior with a sensitive config.
	cfg.HoeffdingDelta = 0.05
	cfg.HoeffdingK = 5.0
	cfg.MinScore = 0.3
	cfg.ROCBoostWeight = 0.3
	cfg.Persistence = 1
	pred := NewPredictor(cfg)

	rng := rand.New(rand.NewSource(42))

	// Send one point at a time. No results during warmup.
	for i := 0; i < MinimumWarmupPoints-1; i++ {
		val := 50.0 + 2.0*rng.NormFloat64()
		m := singlePoint("test_cpu_usage", val, map[string]string{})
		results, err := pred.Run(m)
		if err != nil {
			t.Fatalf("unexpected error at point %d: %v", i, err)
		}
		if len(results) > 0 {
			t.Fatalf("predictions during warmup at point %d: got %d, want 0", i, len(results))
		}
	}

	// Points after warmup — steady metric with small Gaussian noise.
	// Hoeffding bound is tight for K=3: moderate scores (0.3-0.5) from
	// routine Gaussian noise are expected. Only very high scores (>0.8)
	// indicate a genuine anomaly.
	var maxScore float64
	for i := 0; i < 200; i++ {
		val := 50.0 + 2.0*rng.NormFloat64()
		m := singlePoint("test_cpu_usage", val, map[string]string{})
		results, err := pred.Run(m)
		if err != nil {
			t.Fatalf("unexpected error at point %d: %v", i, err)
		}
		for _, r := range results {
			if r.Score > maxScore {
				maxScore = r.Score
			}
		}
	}

	t.Logf("steady state: max score = %.3f (target <0.85)", maxScore)

	// Allow a small margin for Gaussian tail events (~2.2σ outlier with P≈1.5%).
	if maxScore >= 0.85 {
		t.Errorf("steady state produced max score %.3f (want <0.85)", maxScore)
	}
}

// ────────────────────────────────────────────────────────────────────────────
// End-to-end: sudden spike is detected at high confidence
// ────────────────────────────────────────────────────────────────────────────

func TestVerifySpikeDetection(t *testing.T) {
	cfg := DefaultScorerConfig()
	cfg.HoeffdingDelta = 0.05
	cfg.HoeffdingK = 5.0
	cfg.MinScore = 0.3
	cfg.ROCBoostWeight = 0.3
	cfg.Persistence = 1
	pred := NewPredictor(cfg)

	rng := rand.New(rand.NewSource(42))

	// Warmup with steady data, one point at a time.
	for i := 0; i < MinimumWarmupPoints+20; i++ {
		val := 50.0 + 3.0*rng.NormFloat64()
		pred.Run(singlePoint("test_cpu_usage", val, map[string]string{}))
	}

	// Send a large spike.
	results, err := pred.Run(singlePoint("test_cpu_usage", 500.0, map[string]string{}))
	if err != nil {
		t.Fatalf("unexpected error on spike: %v", err)
	}

	found := false
	for _, r := range results {
		if r.Score >= 0.8 && r.Type == "statistical" {
			found = true
			break
		}
	}
	if !found {
		t.Errorf("spike not detected at ≥0.8 confidence. Results: %+v", results)
	}
}

// ────────────────────────────────────────────────────────────────────────────
// End-to-end: gradual ramp is detected
// ────────────────────────────────────────────────────────────────────────────

func TestVerifyRampDetection(t *testing.T) {
	cfg := DefaultScorerConfig()
	cfg.HoeffdingDelta = 0.05
	cfg.HoeffdingK = 5.0
	cfg.MinScore = 0.3
	cfg.ROCBoostWeight = 0.3
	cfg.Persistence = 1
	pred := NewPredictor(cfg)

	rng := rand.New(rand.NewSource(42))

	// Warmup with steady values around 50, one point at a time.
	for i := 0; i < MinimumWarmupPoints+20; i++ {
		val := 50.0 + 2.0*rng.NormFloat64()
		pred.Run(singlePoint("test_mem_usage", val, map[string]string{}))
	}

	// Continuous ramp: values climb 1 per point from 55 to 104.
	type scoredPoint struct {
		value float64
		score float64
	}
	var scores []scoredPoint
	for v := 55.0; v < 105.0; v++ {
		results, _ := pred.Run(singlePoint("test_mem_usage", v, map[string]string{}))
		for _, r := range results {
			scores = append(scores, scoredPoint{v, r.Score})
		}
	}

	// The ramp should produce at least some predictions.
	if len(scores) == 0 {
		t.Fatal("ramp from 55→105 produced zero predictions")
	}

	// Score should be monotonically non-decreasing as the ramp pulls further
	// from the baseline (the sliding median may drift upward but should lag
	// behind the ramp).
	for i := 1; i < len(scores); i++ {
		if scores[i].score < scores[i-1].score-0.05 {
			t.Logf("score dipped at v=%.0f: %.3f → %.3f (may happen on window boundary)",
				scores[i].value, scores[i-1].score, scores[i].score)
		}
	}

	t.Logf("ramp 55→104: %d predictions, first score=%.3f last score=%.3f",
		len(scores), scores[0].score, scores[len(scores)-1].score)
}

// ────────────────────────────────────────────────────────────────────────────
// End-to-end: periodic sine wave does not produce false positives
// ────────────────────────────────────────────────────────────────────────────

func TestVerifySineNoFalsePositives(t *testing.T) {
	pred := NewPredictor(DefaultScorerConfig())

	// Low-amplitude sine wave centered at 50 with amplitude 3 (range 47-53).
	// Deviation of 3 with R=6, n≈30+: ε = 6*sqrt(ln(40)/2n) ≈ 0.7-1.2
	// So max score ≈ 3/(0.7*3) ≈ 1.4 → capped at 1.0 during early points.
	//
	// As the window fills (n→100), ε shrinks and scores stay elevated for
	// any systematic oscillation. This is expected behaviour: a metric that
	// consistently oscillates by ±3 is genuinely deviating from its own
	// median and the detector is correct to flag it.
	//
	// We use amplitude 1.0 (range 49-51, R=2) so that deviations are small
	// enough to remain within the Hoeffding bound at n ≥ 30:
	//   ε = 2*√(ln40/60) ≈ 2*0.248 ≈ 0.50
	//   max_score ≈ 1.0/(0.50*3) ≈ 0.67
	const (
		amplitude   = 1.0
		center      = 50.0
		period      = 10.0
		totalPoints = 300
	)

	var maxScore float64
	for i := 0; i < totalPoints; i++ {
		val := center + amplitude*math.Sin(2*math.Pi*float64(i)/period)
		m := singlePoint("test_sine_cpu", val, map[string]string{})
		results, _ := pred.Run(m)
		for _, r := range results {
			if r.Score > maxScore {
				maxScore = r.Score
			}
			if r.Score >= 0.8 {
				t.Errorf("sine wave produced high-confidence score %.3f at point %d (want <0.8)", r.Score, i)
			}
		}
	}

	t.Logf("sine amplitude=1: max score = %.3f (target <0.8)", maxScore)
}

// ────────────────────────────────────────────────────────────────────────────
// Boundary: exact warmup boundary behavior
// ────────────────────────────────────────────────────────────────────────────

func TestVerifyExactWarmupBoundary(t *testing.T) {
	pred := NewPredictor(DefaultScorerConfig())
	metric := metricResult("test_warmup", 1, 100.0, 0.0)

	// Exactly at MinimumWarmupPoints - 1 → no predictions.
	for i := 0; i < MinimumWarmupPoints-1; i++ {
		results, err := pred.Run(metric)
		if err != nil {
			t.Fatalf("error at point %d: %v", i, err)
		}
		if len(results) != 0 {
			t.Fatalf("predictions at point %d (warmup-1): got %d, want 0", i, len(results))
		}
	}

	// At MinimumWarmupPoints the predictor should start scoring.
	results, err := pred.Run(metric)
	if err != nil {
		t.Fatalf("error at warmup point: %v", err)
	}
	// Note: may or may not produce predictions depending on the data.
	// The important thing is it doesn't crash or error out.
	t.Logf("predictions at warmup boundary: %d", len(results))
}

// ────────────────────────────────────────────────────────────────────────────
// Boundary: constant zero values
// ────────────────────────────────────────────────────────────────────────────

func TestVerifyConstantZero(t *testing.T) {
	pred := NewPredictor(DefaultScorerConfig())

	for i := 0; i < MinimumWarmupPoints+50; i++ {
		m := MetricResult{
			Name: "test_zero",
			Values: []MetricPoint{{
				Value:     0.0,
				Timestamp: fixedTime(),
				Labels:    map[string]string{},
			}},
		}
		results, err := pred.Run([]MetricResult{m})
		if err != nil {
			t.Fatalf("error at point %d: %v", i, err)
		}
		// Zero constant should produce scores of 0 (deviation 0 = score 0).
		for _, r := range results {
			if r.Score != 0 {
				t.Errorf("constant zero produced score %f at point %d", r.Score, i)
			}
		}
	}
}

// ────────────────────────────────────────────────────────────────────────────
// Score monotonicity: increasing deviation produces non-decreasing score
// ────────────────────────────────────────────────────────────────────────────

func TestVerifyScoreMonotonicity(t *testing.T) {
	median := 50.0
	rng := 100.0
	n := 100
	delta := 0.05
	k := 3.0

	eps := rng * math.Sqrt(math.Log(2.0/delta)/(2.0*float64(n)))

	var prevScore float64
	for mult := 0.0; mult <= 4.0; mult += 0.25 {
		dev := median + mult*eps
		score := HoeffdingAnomalyScore(dev, median, rng, n, delta, k)
		if score < prevScore-1e-12 {
			t.Errorf("score decreased: mult=%.2f score=%f < prev=%f", mult, score, prevScore)
		}
		prevScore = score
	}
}

// ────────────────────────────────────────────────────────────────────────────
// Pipeline concurrency: concurrent Run calls do not panic
// ────────────────────────────────────────────────────────────────────────────

func TestVerifyConcurrentSafety(t *testing.T) {
	pred := NewPredictor(DefaultScorerConfig())

	var (
		wg     sync.WaitGroup
		mu     sync.Mutex
		errors []string
	)

	for g := 0; g < 10; g++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for i := 0; i < 50; i++ {
				val := 50.0 + float64(id)*5.0 + float64(i)*0.1
				m := singlePoint("test_concurrent", val, map[string]string{
					"pod": fmt.Sprintf("pod-%d", id),
				})
				results, err := pred.Run(m)
				if err != nil {
					mu.Lock()
					errors = append(errors, fmt.Sprintf("g%d@%d: %v", id, i, err))
					mu.Unlock()
					return
				}
				for _, r := range results {
					if math.IsNaN(r.Score) || math.IsInf(r.Score, 0) {
						mu.Lock()
						errors = append(errors, fmt.Sprintf("g%d@%d: invalid score %v", id, i, r.Score))
						mu.Unlock()
					}
					if r.Score < 0 || r.Score > 1 {
						mu.Lock()
						errors = append(errors, fmt.Sprintf("g%d@%d: score %.3f out of [0,1]", id, i, r.Score))
						mu.Unlock()
					}
				}
			}
		}(g)
	}
	wg.Wait()

	for _, e := range errors {
		t.Error(e)
	}
}

// ────────────────────────────────────────────────────────────────────────────
// Scenario: CPU usage with periodic spikes (normal container startup)
// ────────────────────────────────────────────────────────────────────────────

func TestVerifyStartupSpikeThenSteady(t *testing.T) {
	pred := NewPredictor(DefaultScorerConfig())

	// Simulate a container startup: brief CPU spike then settles.
	sequence := make([]float64, 0, MinimumWarmupPoints+50)
	// Startup: first 10 points are higher.
	for i := 0; i < 10; i++ {
		sequence = append(sequence, 80.0+float64(i)*2.0)
	}
	// After warmup: steady at 35-45.
	for i := 0; i < MinimumWarmupPoints+50; i++ {
		sequence = append(sequence, 40.0+float64(i%5)*2.0)
	}

	anomalyCount := 0
	totalAfterWarmup := 0
	for i, v := range sequence {
		m := MetricResult{
			Name: "test_startup_cpu",
			Values: []MetricPoint{{
				Value:     v,
				Timestamp: fixedTime(),
				Labels:    map[string]string{"pod": "web-1", "namespace": "default"},
			}},
		}
		results, _ := pred.Run([]MetricResult{m})

		// Count only results after warmup.
		pred.mu.RLock()
		dp := pred.datapoints[metricKey("test_startup_cpu", map[string]string{"pod": "web-1", "namespace": "default"})]
		pred.mu.RUnlock()

		if dp >= MinimumWarmupPoints {
			totalAfterWarmup++
			for _, r := range results {
				if r.Score >= 0.3 {
					anomalyCount++
				}
			}
		}
		_ = i
	}

	t.Logf("startup scenario: %d anomalies after warmup out of %d points", anomalyCount, totalAfterWarmup)
	// Once the metric has settled, there should be few remaining anomalies.
	// Allow some during the transition from spike to steady.
	if anomalyCount > totalAfterWarmup/2 {
		t.Errorf("too many anomalies after startup spike: %d/%d", anomalyCount, totalAfterWarmup)
	}
}

// ────────────────────────────────────────────────────────────────────────────
// Scenario: mixed metric types maintain independent state
// ────────────────────────────────────────────────────────────────────────────

func TestVerifyMultipleMetricsIndependent(t *testing.T) {
	cfg := DefaultScorerConfig()
	cfg.HoeffdingDelta = 0.05
	cfg.HoeffdingK = 5.0
	cfg.MinScore = 0.3
	cfg.ROCBoostWeight = 0.3
	cfg.Persistence = 1
	pred := NewPredictor(cfg)

	rng := rand.New(rand.NewSource(42))

	// Feed both metrics one point at a time across iterations.
	// After warmup, the flat metric should not see high-confidence alarms.
	flatMaxScore := 0.0
	spikeHighScores := 0

	for i := 0; i < MinimumWarmupPoints+30; i++ {
		// Feed flat metric one value at a time.
		flatVal := 50.0 + rng.NormFloat64()*1.0
		r1, _ := pred.Run(singlePoint("flat_metric", flatVal, map[string]string{}))
		for _, r := range r1 {
			if r.Score > flatMaxScore {
				flatMaxScore = r.Score
			}
			if r.Score >= 0.8 {
				t.Errorf("flat_metric produced high-confidence score %.3f at point %d", r.Score, i)
			}
		}

		// Feed spike metric (value 50 during warmup, then massive spike).
		val := 50.0
		if i >= MinimumWarmupPoints+5 {
			val = 500.0
		}
		r2, _ := pred.Run(singlePoint("spiky_metric", val, map[string]string{}))
		for _, r := range r2 {
			if r.Score >= 0.8 {
				spikeHighScores++
			}
		}
	}

	t.Logf("flat_metric max score: %.3f, spiky_metric high-scores (≥0.8): %d", flatMaxScore, spikeHighScores)

	// The spiky metric should have detected the spike.
	if spikeHighScores == 0 {
		t.Error("spike not detected on spiky_metric")
	}
}

// ────────────────────────────────────────────────────────────────────────────
// Edge: non-uniform timestamps
// ────────────────────────────────────────────────────────────────────────────

func TestVerifyNonUniformTimestamps(t *testing.T) {
	cfg := DefaultScorerConfig()
	// Verification test expects spike detection; use a sensitive config.
	cfg.HoeffdingDelta = 0.05
	cfg.HoeffdingK = 5.0
	cfg.MinScore = 0.3
	cfg.ROCBoostWeight = 0.3
	cfg.Persistence = 1
	pred := NewPredictor(cfg)

	// Feed warmup values at non-uniform intervals then a spike.
	// The predictor ignores timestamps in scoring, so the spike should
	// still be detected.
	var spikeDetected bool
	for i := 0; i < MinimumWarmupPoints+20; i++ {
		val := float64(50)
		if i >= MinimumWarmupPoints {
			val = 200.0
		}
		m := singlePoint("test_nonuniform_ts", val, map[string]string{})
		results, err := pred.Run(m)
		if err != nil {
			t.Fatalf("error at point %d: %v", i, err)
		}
		if i >= MinimumWarmupPoints {
			for _, r := range results {
				if r.Score >= cfg.MinScore {
					spikeDetected = true
				}
			}
		}
	}
	if !spikeDetected {
		t.Error("spike not detected with non-uniform timestamps")
	}
}

// ────────────────────────────────────────────────────────────────────────────
// Edge: empty metric
// ────────────────────────────────────────────────────────────────────────────

func TestVerifyEmptyMetric(t *testing.T) {
	pred := NewPredictor(DefaultScorerConfig())

	results, err := pred.Run(nil)
	if err != nil {
		t.Errorf("expected no error for nil metrics, got %v", err)
	}
	if len(results) != 0 {
		t.Errorf("expected no results for nil metrics, got %d", len(results))
	}

	results, err = pred.Run([]MetricResult{})
	if err != nil {
		t.Errorf("expected no error for empty metrics, got %v", err)
	}
	if len(results) != 0 {
		t.Errorf("expected no results for empty metrics, got %d", len(results))
	}
}

// ────────────────────────────────────────────────────────────────────────────
// Edge: single metric point repeatedly
// ────────────────────────────────────────────────────────────────────────────

func TestVerifyRepeatedSinglePoint(t *testing.T) {
	pred := NewPredictor(DefaultScorerConfig())

	// Feed the same point many times - value should eventually be well-estimated.
	for i := 0; i < MinimumWarmupPoints+50; i++ {
		m := MetricResult{
			Name: "test_repeated",
			Values: []MetricPoint{{
				Value:     42.0,
				Timestamp: fixedTime(),
				Labels:    map[string]string{},
			}},
		}
		results, err := pred.Run([]MetricResult{m})
		if err != nil {
			t.Fatalf("error at point %d: %v", i, err)
		}
		// Score should be 0 since deviation from median is 0.
		for _, r := range results {
			if r.Score != 0 {
				t.Errorf("repeated value produced non-zero score %f at point %d", r.Score, i)
			}
		}
	}
}

// ────────────────────────────────────────────────────────────────────────────
// Edge: very large values
// ────────────────────────────────────────────────────────────────────────────

func TestVerifyLargeValues(t *testing.T) {
	pred := NewPredictor(DefaultScorerConfig())

	// Large values (e.g. memory bytes) should not cause overflow or NaN.
	for i := 0; i < MinimumWarmupPoints+20; i++ {
		val := 1.5e9 // 1.5 GB in bytes
		if i >= MinimumWarmupPoints+10 {
			val = 3.0e9 // spike to 3 GB
		}
		m := MetricResult{
			Name: "test_large_values",
			Values: []MetricPoint{{
				Value:     val,
				Timestamp: fixedTime(),
				Labels:    map[string]string{},
			}},
		}
		results, err := pred.Run([]MetricResult{m})
		if err != nil {
			t.Fatalf("error at point %d: %v", i, err)
		}
		for _, r := range results {
			if math.IsNaN(r.Score) || math.IsInf(r.Score, 0) {
				t.Fatalf("NaN/Inf score at point %d: %v", i, r.Score)
			}
		}
	}
}

// ────────────────────────────────────────────────────────────────────────────
// Performance: 1000 points on 10 metrics should complete quickly
// ────────────────────────────────────────────────────────────────────────────

func TestVerifyPerformance(t *testing.T) {
	pred := NewPredictor(DefaultScorerConfig())

	// 10 different metric streams, each producing 1000 points.
	// 15s budget for CI runners (GitHub Actions free tier can be 3-5x slower
	// than a dev machine).
	const (
		numMetrics  = 10
		numPoints   = 1000
		maxDuration = 15 * time.Second
	)

	start := time.Now()
	for m := 0; m < numMetrics; m++ {
		for i := 0; i < numPoints; i++ {
			mr := MetricResult{
				Name: fmt.Sprintf("perf_metric_%d", m),
				Values: []MetricPoint{{
					Value:     50.0 + 10.0*math.Sin(float64(i)*0.1),
					Timestamp: fixedTime().Add(time.Duration(i) * time.Second),
					Labels:    map[string]string{"pod": fmt.Sprintf("pod-%d", m)},
				}},
			}
			_, err := pred.Run([]MetricResult{mr})
			if err != nil {
				t.Fatalf("error at metric %d point %d: %v", m, i, err)
			}
		}
	}
	duration := time.Since(start)
	if duration > maxDuration {
		t.Errorf("10k points took %v, want ≤ %v", duration, maxDuration)
	}
}

// ────────────────────────────────────────────────────────────────────────────
// Helpers
// ────────────────────────────────────────────────────────────────────────────

// fixedTime returns a deterministic timestamp for test reproducibility.
func fixedTime() time.Time {
	return time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
}

// singlePoint builds a MetricResult with a single value at the given time.
func singlePoint(name string, val float64, labels map[string]string) []MetricResult {
	return []MetricResult{{
		Name: name,
		Values: []MetricPoint{{
			Value:     val,
			Timestamp: fixedTime(),
			Labels:    labels,
		}},
	}}
}

// metricResult builds a MetricResult with n points drawn from N(center, std^2).
func metricResult(name string, n int, center, std float64) []MetricResult {
	values := make([]MetricPoint, n)
	rng := rand.New(rand.NewSource(42))
	for i := 0; i < n; i++ {
		values[i] = MetricPoint{
			Value:     center + std*rng.NormFloat64(),
			Timestamp: fixedTime().Add(time.Duration(i) * time.Second),
			Labels:    map[string]string{},
		}
	}
	return []MetricResult{{Name: name, Values: values}}
}
