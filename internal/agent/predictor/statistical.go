package predictor

import (
	"math"
	"sync"
	"time"
)

type MetricPoint struct {
	Timestamp time.Time
	Value     float64
	Labels    map[string]string
}

type MetricResult struct {
	Name   string
	Values []MetricPoint
}

// DefaultEWMAAlpha is the default smoothing factor for EWMA.
const DefaultEWMAAlpha = 0.3

// DefaultZScoreWindow is the default rolling window size for Z-score.
const DefaultZScoreWindow = 10

// minimumWarmupPoints is the minimum data points before producing scores.
const minimumWarmupPoints = 3

// EWMAModel tracks an exponentially weighted moving average per metric key.
// Each unique metric name + label combination gets its own smoothed value.
type EWMAModel struct {
	mu    sync.RWMutex
	alpha float64
	state map[string]*ewmaState
}

type ewmaState struct {
	smoothed    float64
	initialized bool
}

// NewEWMAModel creates an EWMA model with the given alpha (0 < alpha <= 1).
func NewEWMAModel(alpha float64) *EWMAModel {
	if alpha <= 0 || alpha > 1 {
		alpha = DefaultEWMAAlpha
	}
	return &EWMAModel{
		alpha: alpha,
		state: make(map[string]*ewmaState),
	}
}

// Update updates the EWMA with a new value for the given metric key.
// Returns the smoothed value and the raw deviation (value - smoothed).
func (e *EWMAModel) Update(metricKey string, value float64) (smoothed, deviation float64) {
	e.mu.Lock()
	defer e.mu.Unlock()

	s, ok := e.state[metricKey]
	if !ok {
		s = &ewmaState{}
		e.state[metricKey] = s
	}

	if !s.initialized {
		s.smoothed = value
		s.initialized = true
		return s.smoothed, 0
	}

	prev := s.smoothed
	s.smoothed = e.alpha*value + (1-e.alpha)*prev
	return s.smoothed, value - s.smoothed
}

// ZScoreModel tracks a rolling window of values and computes Z-scores.
// Maintains separate statistics per metric key.
type ZScoreModel struct {
	mu     sync.RWMutex
	window int
	state  map[string]*zScoreState
}

type zScoreState struct {
	values []float64
	mean   float64
	stddev float64
}

// NewZScoreModel creates a Z-score model with the given window size (min 3).
func NewZScoreModel(window int) *ZScoreModel {
	if window < 3 {
		window = DefaultZScoreWindow
	}
	return &ZScoreModel{
		window: window,
		state:  make(map[string]*zScoreState),
	}
}

// AddValue adds a value to the rolling window and recomputes statistics.
// Returns true when warmup is complete (>= 3 data points).
func (z *ZScoreModel) AddValue(metricKey string, value float64) bool {
	z.mu.Lock()
	defer z.mu.Unlock()

	s, ok := z.state[metricKey]
	if !ok {
		s = &zScoreState{}
		z.state[metricKey] = s
	}

	s.values = append(s.values, value)
	if len(s.values) > z.window {
		s.values = s.values[1:]
	}

	if len(s.values) < minimumWarmupPoints {
		return false
	}

	// Compute mean and population stddev
	var sum, sumSq float64
	for _, v := range s.values {
		sum += v
		sumSq += v * v
	}
	n := float64(len(s.values))
	s.mean = sum / n
	variance := (sumSq / n) - (s.mean * s.mean)
	if variance < 0 {
		variance = 0
	}
	s.stddev = math.Sqrt(variance)
	return true
}

// Score returns the Z-score of the given value relative to the current distribution.
// Returns 0 if warmup is incomplete or stddev is effectively zero.
func (z *ZScoreModel) Score(metricKey string, value float64) float64 {
	z.mu.RLock()
	defer z.mu.RUnlock()

	s, ok := z.state[metricKey]
	if !ok || len(s.values) < minimumWarmupPoints || s.stddev < 1e-10 {
		return 0
	}

	return (value - s.mean) / s.stddev
}

// RateOfChange computes velocity (slope) and acceleration from time series.
type RateOfChange struct{}

// VelocityResult holds linear regression results.
type VelocityResult struct {
	Slope        float64
	Acceleration float64
}

// Velocity computes the linear regression slope and second-derivative acceleration.
// Requires at least 2 points for slope; 4+ points for meaningful acceleration.
func (r *RateOfChange) Velocity(points []MetricPoint) VelocityResult {
	if len(points) < 2 {
		return VelocityResult{}
	}

	n := float64(len(points))
	var sumX, sumY, sumXY, sumX2 float64

	for i, p := range points {
		x := float64(i)
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

	// Acceleration: difference between second-half and first-half slopes
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
	n := float64(len(points))
	var sumX, sumY, sumXY, sumX2 float64
	for i, p := range points {
		x := float64(i)
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
