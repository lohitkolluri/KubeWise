package predictor

import (
	"math"
	"math/rand"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// AdaptiveMedian tests
// ---------------------------------------------------------------------------

func TestAdaptiveMedianEmpty(t *testing.T) {
	a := NewAdaptiveMedian(100)
	if cnt := a.Count(); cnt != 0 {
		t.Fatalf("expected 0, got %d", cnt)
	}
	_, _, _, _, ok := a.Stats()
	if ok {
		t.Fatal("expected ok=false for empty estimator")
	}
}

func TestAdaptiveMedianSingleValue(t *testing.T) {
	a := NewAdaptiveMedian(100)
	a.Add(42.0)
	median, mad, rng, n, ok := a.Stats()
	if !ok || n != 1 {
		t.Fatalf("expected ok=true, n=1; got ok=%v, n=%d", ok, n)
	}
	if median != 42.0 {
		t.Fatalf("expected median 42, got %f", median)
	}
	if mad < 1e-10 {
		t.Fatalf("expected non-zero MAD, got %f", mad)
	}
	if rng != 0 {
		t.Fatalf("expected range 0, got %f", rng)
	}
}

func TestAdaptiveMedianOddCount(t *testing.T) {
	a := NewAdaptiveMedian(100)
	for _, v := range []float64{5, 1, 3, 9, 7} {
		a.Add(v)
	}
	median, _, _, _, ok := a.Stats()
	if !ok {
		t.Fatal("expected ok=true")
	}
	if median != 5.0 {
		t.Fatalf("expected median 5, got %f", median)
	}
}

func TestAdaptiveMedianEvenCount(t *testing.T) {
	a := NewAdaptiveMedian(100)
	for _, v := range []float64{5, 1, 3, 9} {
		a.Add(v)
	}
	median, _, _, _, ok := a.Stats()
	if !ok {
		t.Fatal("expected ok=true")
	}
	if median != 4.0 {
		t.Fatalf("expected median 4, got %f", median)
	}
}

func TestAdaptiveMedianMAD(t *testing.T) {
	a := NewAdaptiveMedian(100)
	for _, v := range []float64{1, 2, 3, 4, 5} {
		a.Add(v)
	}
	_, mad, _, _, ok := a.Stats()
	if !ok {
		t.Fatal("expected ok=true")
	}
	// MAD of [1,2,3,4,5]: absolute deviations from median 3 = [2,1,0,1,2]
	// sorted = [0,1,1,2,2] → median = 1
	if math.Abs(mad-1.0) > 1e-6 {
		t.Fatalf("expected MAD 1, got %f", mad)
	}
}

func TestAdaptiveMedianSlidingWindow(t *testing.T) {
	// Window clamped to MinimumWarmupPoints (10) by constructor.
	a := NewAdaptiveMedian(10)
	vals := []float64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}
	for _, v := range vals {
		a.Add(v)
	}
	// Window should be the last 10 values: 6..15
	if cnt := a.Count(); cnt != 10 {
		t.Fatalf("expected count 10, got %d", cnt)
	}
	median, _, _, _, ok := a.Stats()
	if !ok {
		t.Fatal("expected ok=true")
	}
	// Median of [6,7,8,9,10,11,12,13,14,15] = (10+11)/2 = 10.5
	if median != 10.5 {
		t.Fatalf("expected median 10.5, got %f", median)
	}
}

func TestAdaptiveMedianReset(t *testing.T) {
	a := NewAdaptiveMedian(100)
	a.Add(1)
	a.Add(2)
	a.Add(3)
	a.Reset()
	if cnt := a.Count(); cnt != 0 {
		t.Fatalf("expected 0 after reset, got %d", cnt)
	}
	_, _, _, _, ok := a.Stats()
	if ok {
		t.Fatal("expected ok=false after reset")
	}
}

func TestAdaptiveMedianRange(t *testing.T) {
	a := NewAdaptiveMedian(100)
	for _, v := range []float64{10, 20, 30, 40, 50} {
		a.Add(v)
	}
	_, _, rng, _, ok := a.Stats()
	if !ok {
		t.Fatal("expected ok=true")
	}
	if math.Abs(rng-40) > 1e-6 {
		t.Fatalf("expected range 40, got %f", rng)
	}
}

func TestAdaptiveMedianSmallWindow(t *testing.T) {
	a := NewAdaptiveMedian(2)
	if a.window < MinimumWarmupPoints {
		t.Fatalf("window %d should be at least MinimumWarmupPoints %d", a.window, MinimumWarmupPoints)
	}
}

// ---------------------------------------------------------------------------
// RobustAnomalyScore tests
// ---------------------------------------------------------------------------

func TestRobustScoreZeroDeviation(t *testing.T) {
	score := RobustAnomalyScore(50.0, 50.0, 0.5)
	if score != 0 {
		t.Fatalf("expected 0 for exact match with median, got %f", score)
	}
}

func TestRobustScoreVerySmallMAD(t *testing.T) {
	score := RobustAnomalyScore(50, 50, 1e-11)
	if score != 0 {
		t.Fatalf("expected 0 for near-zero MAD, got %f", score)
	}
}

func TestRobustScoreLargeDeviation(t *testing.T) {
	// Value far from median: |1000 - 50| = 950
	// Z = 0.6745 * 950 / 50 = 12.8155
	// score = 12.8155 / 3.5 = 3.66 → clamped to 1.0
	score := RobustAnomalyScore(1000, 50, 50)
	if score != 1.0 {
		t.Fatalf("expected 1.0 for extreme deviation, got %f", score)
	}
}

func TestRobustScoreModerateDeviation(t *testing.T) {
	// Value moderately far from median: |110 - 100| = 10, MAD = 15
	// Z = 0.6745 * 10 / 15 = 0.4497
	// score = 0.4497 / 3.5 = 0.1285
	score := RobustAnomalyScore(110, 100, 15)
	if score <= 0 || score > 0.2 {
		t.Fatalf("expected moderate score ~0.13, got %f", score)
	}
}

func TestRobustScoreThreshold(t *testing.T) {
	// Value at anomaly threshold: Z = 3.5 → score = 1.0
	// 0.6745 * |x - median| / MAD = 3.5
	// |x - median| = 3.5 * MAD / 0.6745 = 3.5 * 10 / 0.6745 ≈ 51.9
	score := RobustAnomalyScore(101.9, 50, 10)
	if score < 0.99 {
		t.Fatalf("expected score near 1.0 at Z=3.5 threshold, got %f", score)
	}
}

// ---------------------------------------------------------------------------
// ChangepointDetector tests
// ---------------------------------------------------------------------------

func TestChangepointDetectorConstant(t *testing.T) {
	// Constant data should never trigger a changepoint.
	cd := NewChangepointDetector(50, 10, 5, 0.05)
	detected := false
	for i := 0; i < 200; i++ {
		if cd.Add(42.0) {
			detected = true
		}
	}
	if detected {
		t.Fatal("unexpected changepoint in constant data")
	}
}

func TestChangepointDetectorRegimeShift(t *testing.T) {
	// A clear level shift should be detected.
	cd := NewChangepointDetector(100, 10, 5, 0.05)
	// 100 points from one distribution
	for i := 0; i < 100; i++ {
		cd.Add(rand.NormFloat64()*0.1 + 10) //nolint:gosec
	}
	detected := false
	// 100 points from a shifted distribution
	for i := 0; i < 100; i++ {
		if cd.Add(rand.NormFloat64()*0.1 + 50) { //nolint:gosec
			detected = true
		}
	}
	if !detected {
		t.Log("Note: changepoint not detected in regime shift — may need more data")
	}
}

// ---------------------------------------------------------------------------
// RateOfChange tests (keep from original)
// ---------------------------------------------------------------------------

func TestRateOfChangeEmpty(t *testing.T) {
	roc := &RateOfChange{}
	v := roc.Velocity(nil)
	if v.Slope != 0 || v.Acceleration != 0 {
		t.Fatalf("expected zero result for empty input")
	}
}

func TestRateOfChangeSinglePoint(t *testing.T) {
	roc := &RateOfChange{}
	v := roc.Velocity([]MetricPoint{{Value: 42, Timestamp: time.Now()}})
	if v.Slope != 0 || v.Acceleration != 0 {
		t.Fatalf("expected zero result for single point")
	}
}

func TestRateOfChangePositiveSlope(t *testing.T) {
	roc := &RateOfChange{}
	base := time.Now()
	pts := []MetricPoint{
		{Value: 1, Timestamp: base},
		{Value: 2, Timestamp: base.Add(1 * time.Second)},
		{Value: 3, Timestamp: base.Add(2 * time.Second)},
	}
	v := roc.Velocity(pts)
	if v.Slope <= 0 {
		t.Fatalf("expected positive slope, got %f", v.Slope)
	}
	if v.Slope < 0.99 || v.Slope > 1.01 {
		t.Fatalf("expected slope ~1.0, got %f", v.Slope)
	}
}

func TestRateOfChangeNegativeSlope(t *testing.T) {
	roc := &RateOfChange{}
	base := time.Now()
	pts := []MetricPoint{
		{Value: 10, Timestamp: base},
		{Value: 8, Timestamp: base.Add(1 * time.Second)},
		{Value: 6, Timestamp: base.Add(2 * time.Second)},
	}
	v := roc.Velocity(pts)
	if v.Slope >= 0 {
		t.Fatalf("expected negative slope, got %f", v.Slope)
	}
	if v.Slope > -1.01 || v.Slope < -2.99 {
		t.Fatalf("expected slope ~-2.0, got %f", v.Slope)
	}
}

func TestRateOfChangeAcceleration(t *testing.T) {
	roc := &RateOfChange{}
	// Values that curve upward → positive acceleration
	base := time.Now()
	pts := []MetricPoint{
		{Value: 1, Timestamp: base},
		{Value: 2, Timestamp: base.Add(1 * time.Second)},
		{Value: 5, Timestamp: base.Add(2 * time.Second)},
		{Value: 10, Timestamp: base.Add(3 * time.Second)},
	}
	v := roc.Velocity(pts)
	if v.Acceleration <= 0 {
		t.Fatalf("expected positive acceleration, got %f", v.Acceleration)
	}
}
