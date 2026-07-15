package predictor

import (
	"math"
	"sort"
	"sync"
	"time"

	"pgregory.net/changepoint"
)

// MetricPoint represents a single metric data point with optional labels.
type MetricPoint struct {
	Timestamp time.Time
	Value     float64
	Labels    map[string]string
}

// MetricResult contains a named metric with one or more data points.
type MetricResult struct {
	Name   string
	Values []MetricPoint
}

const (
	// DefaultAdaptiveMedianWindow is the default sliding window size for the
	// adaptive median estimator.
	DefaultAdaptiveMedianWindow = 100

	// DefaultHoeffdingDelta is the default false-positive target for the
	// Hoeffding inequality bound (5%).
	DefaultHoeffdingDelta = 0.05

	// DefaultHoeffdingK is the default sensitivity multiplier: score reaches
	// 1.0 when |x-median| >= K * epsilon.
	DefaultHoeffdingK = 5.0

	// MinimumWarmupPoints is the minimum data points before producing anomaly scores.
	MinimumWarmupPoints = 10

	// DefaultChangepointMinSeg is the minimum segment length for ED-PELT changepoint detection.
	DefaultChangepointMinSeg = 5

	// DefaultChangepointInterval is how often (in data points) to run the
	// O(n log n) changepoint detector.
	DefaultChangepointInterval = 50

	// maxChangepointBuffer caps buffered values to prevent unbounded growth.
	maxChangepointBuffer = 500
)

// ---------------------------------------------------------------------------
// AdaptiveMedian — sliding-window robust central tendency + dispersion
// ---------------------------------------------------------------------------

// AdaptiveMedian maintains a sliding window of values and computes the median
// and median absolute deviation (MAD), which are robust statistics insensitive
// to outliers.
type AdaptiveMedian struct {
	mu     sync.RWMutex
	window int
	values []float64
}

// NewAdaptiveMedian creates an estimator with the given sliding window size.
func NewAdaptiveMedian(window int) *AdaptiveMedian {
	if window < MinimumWarmupPoints {
		window = MinimumWarmupPoints
	}
	return &AdaptiveMedian{
		window: window,
		values: make([]float64, 0, window),
	}
}

// Add appends a value to the sliding window, discarding the oldest if at
// capacity.
func (a *AdaptiveMedian) Add(v float64) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.values = append(a.values, v)
	if len(a.values) > a.window {
		a.values = a.values[1:]
	}
}

// Reset clears the window entirely.
func (a *AdaptiveMedian) Reset() {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.values = make([]float64, 0, a.window)
}

// Count returns the number of values currently in the window.
func (a *AdaptiveMedian) Count() int {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return len(a.values)
}

// Stats returns the median, MAD, range, and count from the current window.
// ok is false when the window is empty.
func (a *AdaptiveMedian) Stats() (median, mad, rng float64, n int, ok bool) {
	a.mu.RLock()
	defer a.mu.RUnlock()

	n = len(a.values)
	if n == 0 {
		return 0, 0, 0, 0, false
	}

	// Sort a copy for median and range.
	sorted := make([]float64, n)
	copy(sorted, a.values)
	sort.Float64s(sorted)

	rng = sorted[n-1] - sorted[0]

	if n%2 == 0 {
		median = (sorted[n/2-1] + sorted[n/2]) / 2
	} else {
		median = sorted[n/2]
	}

	// Compute MAD from sorted deviations (avoids a second sort).
	devs := make([]float64, n)
	for i, v := range sorted {
		devs[i] = math.Abs(v - median)
	}
	sort.Float64s(devs)
	if n%2 == 0 {
		mad = (devs[n/2-1] + devs[n/2]) / 2
	} else {
		mad = devs[n/2]
	}
	if mad < 1e-10 {
		mad = 1e-10
	}

	return median, mad, rng, n, true
}

// ---------------------------------------------------------------------------
// HoeffdingAnomalyScore — distribution-free anomaly scoring
// ---------------------------------------------------------------------------

// HoeffdingAnomalyScore computes an anomaly score in [0,1] for a new value
// using the adaptive-median estimator and the Hoeffding inequality bound.
//
// The Hoeffding bound ε = R × sqrt(ln(2/δ) / 2n) gives a distribution-free
// confidence interval at false-positive rate δ.  The score is:
//
//	score = min(|x - median| / (ε × K), 1)
//
// With K=3, a deviation of ε scores 0.33, 2ε scores 0.67, and 3ε scores 1.0.
func HoeffdingAnomalyScore(value, median, rng float64, n int, delta, k float64) float64 {
	if n < MinimumWarmupPoints || rng < 1e-12 {
		return 0
	}

	// Hoeffding bound: ε = R × sqrt(ln(2/δ) / (2n))
	lnTerm := math.Log(2.0 / delta)
	epsilon := rng * math.Sqrt(lnTerm/(2.0*float64(n)))
	if epsilon < 1e-12 {
		return 0
	}

	deviation := math.Abs(value-median) / epsilon
	score := deviation / k
	if score > 1.0 {
		return 1.0
	}
	if score < 0 {
		return 0
	}
	return score
}

// ---------------------------------------------------------------------------
// ChangepointDetector — online wrapper around ED-PELT
// ---------------------------------------------------------------------------

// ChangepointDetector wraps the batch ED-PELT algorithm
// (changepoint.NonParametric) for online use by accumulating a buffer and
// running detection periodically. When a changepoint is found in the recent
// third of the buffer the detector signals a regime change, allowing the
// caller to reset its estimator and adapt to the new process behaviour.
type ChangepointDetector struct {
	mu          sync.Mutex
	buffer      []float64
	minSegment  int
	checkEvery  int
	pointsSince int
}

// NewChangepointDetector creates a detector that checks for changepoints every
// checkEvery data points.  minSegment is the minimum homogeneous segment
// length passed to ED-PELT.
func NewChangepointDetector(minSegment, checkEvery int) *ChangepointDetector {
	if minSegment < 1 {
		minSegment = DefaultChangepointMinSeg
	}
	if checkEvery < 1 {
		checkEvery = DefaultChangepointInterval
	}
	return &ChangepointDetector{
		minSegment: minSegment,
		checkEvery: checkEvery,
	}
}

// Add records a new value and runs the changepoint detector if enough points
// have accumulated since the last check.  Returns true when a changepoint is
// detected in the recent third of the buffer.
func (c *ChangepointDetector) Add(v float64) bool {
	c.mu.Lock()
	needUnlock := true
	defer func() {
		if needUnlock {
			c.mu.Unlock()
		}
	}()

	c.buffer = append(c.buffer, v)
	if len(c.buffer) > maxChangepointBuffer {
		c.buffer = c.buffer[len(c.buffer)-maxChangepointBuffer:]
	}
	c.pointsSince++

	if c.pointsSince < c.checkEvery || len(c.buffer) < c.minSegment*3 {
		return false
	}
	c.pointsSince = 0

	// Run ED-PELT (O(n log n) in the buffer size).
	cps := changepoint.NonParametric(c.buffer, c.minSegment)
	if len(cps) == 0 {
		return false
	}

	lastCP := cps[len(cps)-1]
	if lastCP <= len(c.buffer)*2/3 {
		// Changepoint too far back — not a recent regime shift.
		return false
	}

	// Trim buffer to data after the last changepoint.
	c.buffer = append([]float64(nil), c.buffer[lastCP:]...)
	needUnlock = false
	c.mu.Unlock()

	return true
}

// Reset discards the buffer.
func (c *ChangepointDetector) Reset() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.buffer = nil
	c.pointsSince = 0
}

// Count returns the number of buffered values.
func (c *ChangepointDetector) Count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.buffer)
}

// ---------------------------------------------------------------------------
// RateOfChange — linear-regression velocity and acceleration
// ---------------------------------------------------------------------------

// RateOfChange computes velocity (slope) and acceleration from a time series.
type RateOfChange struct{}

// VelocityResult holds the linear-regression slope and second-difference
// acceleration.
type VelocityResult struct {
	Slope        float64
	Acceleration float64
}

// Velocity computes the linear regression slope and acceleration from a
// sequence of MetricPoints.  Requires at least 2 points for slope and 4+
// for meaningful acceleration. Uses actual timestamps (seconds since first
// point) for the x-axis so the slope represents change per unit time.
func (r *RateOfChange) Velocity(points []MetricPoint) VelocityResult {
	if len(points) < 2 {
		return VelocityResult{}
	}

	base := points[0].Timestamp
	n := float64(len(points))
	var sumX, sumY, sumXY, sumX2 float64

	for _, p := range points {
		x := p.Timestamp.Sub(base).Seconds()
		y := p.Value
		sumX += x
		sumY += y
		sumXY += x * y
		sumX2 += x * x
	}

	denom := n*sumX2 - sumX*sumX
	if denom == 0 {
		return VelocityResult{}
	}
	slope := (n*sumXY - sumX*sumY) / denom

	// Acceleration: difference between second-half and first-half slopes.
	accel := 0.0
	if len(points) >= 4 {
		mid := len(points) / 2
		slope1 := simpleSlope(points[:mid])
		slope2 := simpleSlope(points[mid:])
		accel = slope2 - slope1
	}

	return VelocityResult{
		Slope:        slope,
		Acceleration: accel,
	}
}

func simpleSlope(points []MetricPoint) float64 {
	if len(points) < 2 {
		return 0
	}
	base := points[0].Timestamp
	n := float64(len(points))
	var sumX, sumY, sumXY, sumX2 float64
	for _, p := range points {
		x := p.Timestamp.Sub(base).Seconds()
		y := p.Value
		sumX += x
		sumY += y
		sumXY += x * y
		sumX2 += x * x
	}
	denom := n*sumX2 - sumX*sumX
	if denom == 0 {
		return 0
	}
	return (n*sumXY - sumX*sumY) / denom
}
