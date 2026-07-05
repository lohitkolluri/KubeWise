package predictor

import (
	"math"
	"testing"
	"time"
)

func TestStatisticalEWMA(t *testing.T) {
	m := NewEWMAModel(0.5)

	// First value initializes smoothed
	smoothed, dev := m.Update("cpu/pod-a", 100.0)
	if smoothed != 100.0 {
		t.Fatalf("expected smoothed=100, got %f", smoothed)
	}
	if dev != 0 {
		t.Fatalf("expected deviation=0 on init, got %f", dev)
	}

	// Second value: smoothed = 0.5*110 + 0.5*100 = 105
	smoothed, dev = m.Update("cpu/pod-a", 110.0)
	if smoothed != 105.0 {
		t.Fatalf("expected smoothed=105, got %f", smoothed)
	}
	if dev != 5.0 {
		t.Fatalf("expected deviation=5, got %f", dev)
	}

	// Flat values should converge
	m2 := NewEWMAModel(0.3)
	_, _ = m2.Update("flat/val", 50.0)
	for i := 0; i < 10; i++ {
		smoothed, dev = m2.Update("flat/val", 50.0)
	}
	if math.Abs(dev) > 1.0 {
		t.Fatalf("expected deviation near 0 for flat values, got %f", dev)
	}
	// Smoothed should be close to 50
	if math.Abs(smoothed-50.0) > 1.0 {
		t.Fatalf("expected smoothed near 50, got %f", smoothed)
	}

	// Separate keys maintain separate state
	_, devA := m.Update("cpu/pod-a", 120.0)
	_, devB := m.Update("cpu/pod-b", 10.0)
	if math.Abs(devA-devB) < 1 {
		t.Fatal("expected different deviations for different keys")
	}
}

func TestStatisticalZScore(t *testing.T) {
	z := NewZScoreModel(10)

	// First two values: warmup not complete
	ok := z.AddValue("mem/db", 100.0)
	if ok {
		t.Fatal("expected false after 1 value (warmup)")
	}
	ok = z.AddValue("mem/db", 101.0)
	if ok {
		t.Fatal("expected false after 2 values (warmup)")
	}

	// Third value: warmup complete
	ok = z.AddValue("mem/db", 99.0)
	if !ok {
		t.Fatal("expected true after 3 values (warmup complete)")
	}

	// Z-score for value near mean should be small
	score := z.Score("mem/db", 100.0)
	if math.Abs(score) > 1.5 {
		t.Fatalf("expected z-score near 0 for normal value, got %f", score)
	}

	// Z-score for extreme value should be large
	score = z.Score("mem/db", 1000.0)
	if score < 2.0 {
		t.Fatalf("expected z-score >= 2 for extreme value, got %f", score)
	}

	// Unknown key should return 0
	score = z.Score("unknown", 100.0)
	if score != 0 {
		t.Fatalf("expected 0 for unknown key, got %f", score)
	}

	// Pre-warmup Score should return 0
	z2 := NewZScoreModel(5)
	z2.AddValue("only/one", 50.0)
	z2.AddValue("only/one", 51.0)
	score = z2.Score("only/one", 100.0)
	if score != 0 {
		t.Fatalf("expected 0 before warmup, got %f", score)
	}
}

func TestStatisticalZScoreMonotonicIncrease(t *testing.T) {
	// Monotonically increasing values should produce high z-scores
	z := NewZScoreModel(10)

	for i := 0; i < 10; i++ {
		z.AddValue("rising/val", float64(i*10))
	}

	// Last value 90 vs prior mean (~45) and stddev (~28)
	// z-score should be around 1.5
	score := z.Score("rising/val", 100.0)
	if score < 1.0 {
		t.Fatalf("expected z-score >= 1 for rising trend, got %f", score)
	}
}

func TestStatisticalZScoreConstantValues(t *testing.T) {
	// Constant values → stddev near 0 → Score returns 0
	z := NewZScoreModel(5)

	for i := 0; i < 5; i++ {
		z.AddValue("constant/val", 50.0)
	}

	score := z.Score("constant/val", 50.0)
	if score != 0 {
		t.Fatalf("expected 0 for constant values (stddev=0), got %f", score)
	}
}

func TestStatisticalVelocity(t *testing.T) {
	r := &RateOfChange{}

	// Less than 2 points
	v := r.Velocity(nil)
	if v.Slope != 0 || v.Acceleration != 0 {
		t.Fatal("expected zero for nil input")
	}

		v = r.Velocity([]MetricPoint{})
	if v.Slope != 0 {
		t.Fatal("expected zero for empty input")
	}

	v = r.Velocity([]MetricPoint{{Value: 1}})
	if v.Slope != 0 {
		t.Fatal("expected zero for single point")
	}

	// Linear: y = 2x, so slope should be ~2
	points := make([]MetricPoint, 5)
	for i := 0; i < 5; i++ {
		points[i] = MetricPoint{
			Timestamp: time.Now(),
			Value:     float64(2 * i),
		}
	}
	v = r.Velocity(points)
	if math.Abs(v.Slope-2.0) > 0.5 {
		t.Fatalf("expected slope ~2 for linear data, got %f", v.Slope)
	}

	// Quadratic: y = x^2 (accelerating)
	quad := make([]MetricPoint, 6)
	for i := 0; i < 6; i++ {
		quad[i] = MetricPoint{
			Timestamp: time.Now(),
			Value:     float64(i * i),
		}
	}
	v = r.Velocity(quad)
	if v.Acceleration <= 0 {
		t.Fatalf("expected positive acceleration for quadratic, got %f", v.Acceleration)
	}
}
