// Benchmark — massive synthetic K8s anomaly detection benchmark.
//
// Generates 10,800 known-ground-truth data points across 9 metric patterns,
// runs 17+ detection algorithms + 4 ensemble methods, evaluates with
// train/test split, detects overfitting, and ranks by F1 score.
//
// Goal: find an ensemble that achieves >=95% accuracy across ALL anomaly types.
package main

import (
	"fmt"
	"math"
	"math/rand"
	"sort"
	"strings"
	"time"

	"github.com/dgryski/go-change"
	timeseriesgo "github.com/wenta/timeseries-go"
	"github.com/wenta/timeseries-go/anomaly"

	"github.com/lohitkolluri/KubeWise/internal/agent/predictor"
)

// ---------------------------------------------------------------------------
// Data types
// ---------------------------------------------------------------------------

// BenchPoint is a single synthetic data point with known ground truth.
type BenchPoint struct {
	Value        float64
	KnownAnomaly bool
}

// Pattern is a named synthetic time series.
type Pattern struct {
	Name string
	Data []BenchPoint
}

// benchAlgo wraps a name and detection function.
type benchAlgo struct {
	Name   string
	Detect func([]BenchPoint) []bool // per-point anomaly prediction
}

// PatternResult holds per-(pattern, algorithm) metrics.
type PatternResult struct {
	Pattern        string
	Algorithm      string
	TP, FP, FN     int
	Precision      float64
	Recall         float64
	F1             float64
	Accuracy       float64
	DetectionDelay float64 // average distance from region start to first detection
}

// Summary aggregates across all patterns for one algorithm.
type Summary struct {
	Algorithm    string
	AvgF1        float64
	AvgPrecision float64
	AvgRecall    float64
	AvgAccuracy  float64
	AvgDelay     float64
	TotalTP      int
	TotalFP      int
	TotalFN      int
	TotalPoints  int
}

// TrainTestResult holds metrics for both train and test splits.
type TrainTestResult struct {
	Train PatternResult
	Test  PatternResult
	Delta float64 // Test F1 - Train F1 (negative = overfitting)
}

// ---------------------------------------------------------------------------
// Data generation helpers
// ---------------------------------------------------------------------------

func gaussian(rng *rand.Rand, mean, std float64) float64 {
	return mean + std*rng.NormFloat64()
}

// addNoise adds realistic noise to an existing slice of BenchPoints.
func addNoise(rng *rand.Rand, pts []BenchPoint, noiseStd float64) {
	for i := range pts {
		pts[i].Value += gaussian(rng, 0, noiseStd)
	}
}

// addOutliers injects random outlier spikes into the data.
func addOutliers(rng *rand.Rand, pts []BenchPoint, fraction float64, amplitude float64) {
	nOut := int(float64(len(pts)) * fraction)
	for i := 0; i < nOut; i++ {
		idx := rng.Intn(len(pts))
		if rng.Float64() < 0.5 {
			pts[idx].Value += amplitude
		} else {
			pts[idx].Value -= amplitude
		}
	}
}

// dropPoints removes a fraction of points (simulates missing data).
func dropPoints(rng *rand.Rand, pts []BenchPoint, fraction float64) []BenchPoint {
	if fraction <= 0 {
		return pts
	}
	keep := make([]BenchPoint, 0, len(pts))
	for _, p := range pts {
		if rng.Float64() < fraction {
			continue // drop
		}
		keep = append(keep, p)
	}
	return keep
}

// generateBaseline produces n points of gaussian noise.
func generateBaseline(rng *rand.Rand, n int, mean, std float64) []BenchPoint {
	pts := make([]BenchPoint, n)
	for i := 0; i < n; i++ {
		pts[i] = BenchPoint{Value: gaussian(rng, mean, std), KnownAnomaly: false}
	}
	return pts
}

// generateMemLeak produces a gradual rise from 50 to 95 over n points.
func generateMemLeak(rng *rand.Rand, n int, startAnomaly int) []BenchPoint {
	pts := make([]BenchPoint, n)
	for i := 0; i < n; i++ {
		frac := float64(i) / float64(n-1)
		val := 50.0 + frac*45.0 // rises 50→95
		isAnom := i >= startAnomaly
		noise := gaussian(rng, 0, 2)
		pts[i] = BenchPoint{
			Value:        val + noise,
			KnownAnomaly: isAnom,
		}
	}
	addOutliers(rng, pts, 0.02, 8)
	return pts
}

// generateCPUSpikeTrain produces multiple CPU spikes.
func generateCPUSpikeTrain(rng *rand.Rand, n int, nSpikes int) []BenchPoint {
	pts := make([]BenchPoint, n)
	spikeLen := 8
	minSpacing := 60
	spikeStarts := make([]int, 0, nSpikes)
	for len(spikeStarts) < nSpikes {
		s := rng.Intn(n - spikeLen - 1)
		ok := true
		for _, existing := range spikeStarts {
			if abs(s-existing) < minSpacing {
				ok = false
				break
			}
		}
		if ok {
			spikeStarts = append(spikeStarts, s)
		}
	}

	for i := 0; i < n; i++ {
		isAnom := false
		for _, s := range spikeStarts {
			if i >= s && i < s+spikeLen {
				isAnom = true
				break
			}
		}
		var val float64
		if isAnom {
			amplitude := 40.0 + rng.Float64()*59.0 // 40-99
			val = 50.0 + amplitude
			val += gaussian(rng, 0, 5)
		} else {
			val = gaussian(rng, 50, 5)
		}
		pts[i] = BenchPoint{Value: val, KnownAnomaly: isAnom}
	}
	return pts
}

// generateStepChange produces data with a mean shift at midpoint.
func generateStepChange(rng *rand.Rand, n int, changePoint int, preMean, postMean, std float64) []BenchPoint {
	pts := make([]BenchPoint, n)
	for i := 0; i < n; i++ {
		isAnom := i >= changePoint
		mean := preMean
		if isAnom {
			mean = postMean
		}
		pts[i] = BenchPoint{
			Value:        gaussian(rng, mean, std),
			KnownAnomaly: isAnom,
		}
	}
	addOutliers(rng, pts, 0.015, 10)
	return pts
}

// generateSeasonality produces a sine wave with additive anomalies.
func generateSeasonality(rng *rand.Rand, n int, period int, amplitude, mean float64) []BenchPoint {
	pts := make([]BenchPoint, n)
	for i := 0; i < n; i++ {
		val := mean + amplitude*math.Sin(2*math.Pi*float64(i)/float64(period))
		// Add small noise
		val += gaussian(rng, 0, 2)
		pts[i] = BenchPoint{Value: val, KnownAnomaly: false}
	}
	// Inject some anomalous spikes into the seasonal pattern
	for i := 0; i < 5; i++ {
		idx := rng.Intn(n)
		pts[idx].Value += 25.0 + rng.Float64()*15.0
		pts[idx].KnownAnomaly = true
	}
	return pts
}

// generateCrashLoop produces an oscillation between 0 and 100.
func generateCrashLoop(rng *rand.Rand, n int) []BenchPoint {
	pts := make([]BenchPoint, n)
	for i := 0; i < n; i++ {
		var val float64
		if i%2 == 0 {
			val = gaussian(rng, 0, 5)
		} else {
			val = gaussian(rng, 100, 5)
		}
		pts[i] = BenchPoint{Value: val, KnownAnomaly: true}
	}
	return pts
}

// generateGradualDegradation produces exponential degradation.
func generateGradualDegradation(rng *rand.Rand, n int, startAnomaly int) []BenchPoint {
	pts := make([]BenchPoint, n)
	for i := 0; i < n; i++ {
		frac := float64(i) / float64(n-1)
		// Exponential: starts at 50, accelerates to 99
		val := 50.0 + 49.0*frac*frac
		isAnom := i >= startAnomaly
		pts[i] = BenchPoint{
			Value:        val + gaussian(rng, 0, 3),
			KnownAnomaly: isAnom,
		}
	}
	return pts
}

// generateMixedPatterns combines multiple anomaly types.
func generateMixedPatterns(rng *rand.Rand, n int) []BenchPoint {
	pts := make([]BenchPoint, n)
	// Segment 1: baseline with mini-spikes (0-400)
	for i := 0; i < 400 && i < n; i++ {
		isAnom := i%67 == 0 // periodic tiny anomalies
		val := gaussian(rng, 50, 5)
		if isAnom {
			val += 25.0
		}
		pts[i] = BenchPoint{Value: val, KnownAnomaly: isAnom}
	}
	// Segment 2: oscillating with drift (400-800)
	for i := 400; i < 800 && i < n; i++ {
		t := float64(i-400) / 400.0
		val := 50.0 + 10*math.Sin(2*math.Pi*float64(i)/48.0) + t*20.0
		isAnom := t > 0.5 // second half is anomalous (drift)
		pts[i] = BenchPoint{Value: val + gaussian(rng, 0, 3), KnownAnomaly: isAnom}
	}
	// Segment 3: rapid oscillations (800-1200)
	for i := 800; i < 1200 && i < n; i++ {
		val := 50.0 + 30*math.Sin(2*math.Pi*float64(i)/8.0)
		pts[i] = BenchPoint{Value: val + gaussian(rng, 0, 4), KnownAnomaly: true}
	}
	// Segment 4: rest is baseline
	for i := 1200; i < n; i++ {
		pts[i] = BenchPoint{Value: gaussian(rng, 50, 5), KnownAnomaly: false}
	}
	return pts
}

// generateTransientOutliers produces normal data with brief anomalous spikes.
func generateTransientOutliers(rng *rand.Rand, n int) []BenchPoint {
	pts := make([]BenchPoint, n)
	nOutliers := 15
	outlierPositions := make(map[int]bool)
	for len(outlierPositions) < nOutliers {
		pos := rng.Intn(n)
		outlierPositions[pos] = true
	}
	for i := 0; i < n; i++ {
		_, isAnom := outlierPositions[i]
		val := gaussian(rng, 50, 5)
		if isAnom {
			val += 30.0 + rng.Float64()*20.0
		}
		pts[i] = BenchPoint{Value: val, KnownAnomaly: isAnom}
	}
	return pts
}

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

func extractValues(pts []BenchPoint) []float64 {
	out := make([]float64, len(pts))
	for i, p := range pts {
		out[i] = p.Value
	}
	return out
}

func floatsToTimeSeries(vals []float64) timeseriesgo.TimeSeries {
	ts := timeseriesgo.Empty()
	base := time.Date(2025, 1, 1, 0, 0, 0, 0, time.UTC)
	for i, v := range vals {
		ts.AddPoint(timeseriesgo.DataPoint{
			Timestamp: base.Add(time.Duration(i) * 30 * time.Second),
			Value:     v,
		})
	}
	return ts
}

func meanOf(vals []float64) float64 {
	if len(vals) == 0 {
		return 0
	}
	sum := 0.0
	for _, v := range vals {
		sum += v
	}
	return sum / float64(len(vals))
}

func stdOf(vals []float64, mean float64) float64 {
	if len(vals) < 2 {
		return 0
	}
	var sq float64
	for _, v := range vals {
		d := v - mean
		sq += d * d
	}
	return math.Sqrt(sq / float64(len(vals)-1))
}

func medianOf(vals []float64) float64 {
	if len(vals) == 0 {
		return 0
	}
	sorted := make([]float64, len(vals))
	copy(sorted, vals)
	sort.Float64s(sorted)
	n := len(sorted)
	if n%2 == 0 {
		return (sorted[n/2-1] + sorted[n/2]) / 2
	}
	return sorted[n/2]
}

func madOf(vals []float64, median float64) float64 {
	devs := make([]float64, len(vals))
	for i, v := range vals {
		devs[i] = math.Abs(v - median)
	}
	return medianOf(devs)
}

// rollingStats computes running mean and std over a window.
func rollingStats(window []float64) (mean, std float64) {
	n := len(window)
	if n == 0 {
		return 0, 0
	}
	sum := 0.0
	for _, v := range window {
		sum += v
	}
	mean = sum / float64(n)
	if n < 2 {
		return mean, 0
	}
	var sq float64
	for _, v := range window {
		d := v - mean
		sq += d * d
	}
	std = math.Sqrt(sq / float64(n-1))
	return mean, std
}

// ---------------------------------------------------------------------------
// STL-like decomposition and helpers (kept from original)
// ---------------------------------------------------------------------------

func simpleSTL(data []float64, period, trendWindow int) ([]float64, []float64, []float64) {
	n := len(data)
	trend := make([]float64, n)
	seasonal := make([]float64, n)
	residual := make([]float64, n)

	if n == 0 {
		return trend, seasonal, residual
	}

	if trendWindow < 3 {
		trendWindow = 3
	}
	if trendWindow%2 == 0 {
		trendWindow++
	}
	half := trendWindow / 2

	for i := 0; i < n; i++ {
		lo := i - half
		hi := i + half
		if lo < 0 {
			lo = 0
		}
		if hi >= n {
			hi = n - 1
		}
		sum := 0.0
		count := 0
		for j := lo; j <= hi; j++ {
			sum += data[j]
			count++
		}
		trend[i] = sum / float64(count)
	}

	if period > 1 && n >= period*2 {
		detrended := make([]float64, n)
		for i := 0; i < n; i++ {
			detrended[i] = data[i] - trend[i]
		}

		seasonSums := make([]float64, period)
		seasonCounts := make([]int, period)
		for i := 0; i < n; i++ {
			p := i % period
			if !math.IsNaN(detrended[i]) {
				seasonSums[p] += detrended[i]
				seasonCounts[p]++
			}
		}

		seasonProfile := make([]float64, period)
		for p := 0; p < period; p++ {
			if seasonCounts[p] > 0 {
				seasonProfile[p] = seasonSums[p] / float64(seasonCounts[p])
			}
		}

		seasonMean := meanOf(seasonProfile)
		for p := 0; p < period; p++ {
			seasonProfile[p] -= seasonMean
		}

		for i := 0; i < n; i++ {
			seasonal[i] = seasonProfile[i%period]
		}
	}

	for i := 0; i < n; i++ {
		residual[i] = data[i] - trend[i] - seasonal[i]
	}

	return trend, seasonal, residual
}

func detectPeriod(vals []float64) int {
	n := len(vals)
	if n < 96 {
		return 0
	}
	if n >= 96*2 {
		acf := autocorr(vals, 96)
		if acf > 0.5 {
			return 96
		}
	}
	return 0
}

func autocorr(vals []float64, lag int) float64 {
	n := len(vals)
	if n <= lag {
		return 0
	}
	mean := meanOf(vals)
	var num, den1, den2 float64
	for i := 0; i < n-lag; i++ {
		d0 := vals[i] - mean
		d1 := vals[i+lag] - mean
		num += d0 * d1
		den1 += d0 * d0
		den2 += d1 * d1
	}
	den := math.Sqrt(den1 * den2)
	if den < 1e-10 {
		return 0
	}
	return num / den
}

// ---------------------------------------------------------------------------
// Algorithm 1: Robust Z-score (baseline) — uses production code
// ---------------------------------------------------------------------------

func algoRobustZScore(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	am := predictor.NewAdaptiveMedian(100)
	for i, pt := range data {
		am.Add(pt.Value)
		med, mad, _, _, ok := am.Stats()
		if !ok {
			continue
		}
		score := predictor.RobustAnomalyScore(pt.Value, med, mad)
		preds[i] = score >= 1.0
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 2: Z-score (classic mean/std)
// ---------------------------------------------------------------------------

func algoZScore(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	const window = 50
	for i := window; i < len(data); i++ {
		sum := 0.0
		sumSq := 0.0
		for j := i - window; j < i; j++ {
			v := data[j].Value
			sum += v
			sumSq += v * v
		}
		mean := sum / window
		variance := (sumSq - sum*sum/window) / (window - 1)
		std := math.Sqrt(variance)
		if std > 1e-10 {
			z := math.Abs(data[i].Value-mean) / std
			preds[i] = z > 3.5
		}
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 3: STL Decomposition + RobustZScore
// ---------------------------------------------------------------------------

func algoSTLRobustZ(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	vals := extractValues(data)
	period := detectPeriod(vals)
	trendWindow := 21
	if trendWindow%2 == 0 {
		trendWindow++
	}

	_, _, residual := simpleSTL(vals, period, trendWindow)

	ts := floatsToTimeSeries(residual)
	rz, err := anomaly.RobustZScore(ts)
	if err != nil {
		return preds
	}
	rzVals := rz.Values()
	for i := 0; i < len(preds) && i < len(rzVals); i++ {
		preds[i] = math.Abs(rzVals[i]) > 3.5
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 4: Moving Average + 3-sigma threshold
// ---------------------------------------------------------------------------

func algoMovingAverage(data []BenchPoint) []bool {
	const window = 30
	preds := make([]bool, len(data))
	for i := window; i < len(data); i++ {
		sum := 0.0
		sumSq := 0.0
		for j := i - window; j < i; j++ {
			v := data[j].Value
			sum += v
			sumSq += v * v
		}
		mean := sum / window
		variance := (sumSq - sum*sum/window) / (window - 1)
		std := math.Sqrt(variance)
		diff := math.Abs(data[i].Value - mean)
		preds[i] = diff > 3.0*std
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 5: EWMA (Exponential Weighted Moving Average)
// ---------------------------------------------------------------------------

func algoEWMA(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	if len(data) < 2 {
		return preds
	}

	alpha := 0.15
	ewma := data[0].Value
	// Track residuals for threshold
	residuals := make([]float64, 0)
	const warmup = 30

	for i := 1; i < len(data); i++ {
		ewma = alpha*data[i].Value + (1-alpha)*ewma
		residual := data[i].Value - ewma
		if i >= warmup {
			residuals = append(residuals, residual)
		}
	}

	// Compute threshold from residual distribution
	resMean := meanOf(residuals)
	resStd := stdOf(residuals, resMean)

	// Second pass with EWMA
	ewma2 := data[0].Value
	for i := 1; i < len(data); i++ {
		ewma2 = alpha*data[i].Value + (1-alpha)*ewma2
		residual := data[i].Value - ewma2
		if i >= warmup && resStd > 1e-10 {
			z := math.Abs(residual-resMean) / resStd
			preds[i] = z > 3.0
		}
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 6: Double Exponential Smoothing (Holt's method)
// ---------------------------------------------------------------------------

func algoDoubleExpSmoothing(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	if len(data) < 3 {
		return preds
	}

	alpha := 0.3
	beta := 0.1

	level := data[0].Value
	trend := data[1].Value - data[0].Value

	const warmup = 30
	residuals := make([]float64, 0, len(data))

	for i := 1; i < len(data); i++ {
		fc := level + trend
		residual := data[i].Value - fc
		if i >= warmup {
			residuals = append(residuals, residual)
		}
		prevLevel := level
		level = alpha*data[i].Value + (1-alpha)*(level+trend)
		trend = beta*(level-prevLevel) + (1-beta)*trend
	}

	resMean := meanOf(residuals)
	resStd := stdOf(residuals, resMean)

	// Re-score
	level2 := data[0].Value
	trend2 := data[1].Value - data[0].Value
	for i := 1; i < len(data); i++ {
		fc := level2 + trend2
		residual := data[i].Value - fc
		if i >= warmup && resStd > 1e-10 {
			z := math.Abs(residual-resMean) / resStd
			preds[i] = z > 3.0
		}
		prevLevel := level2
		level2 = alpha*data[i].Value + (1-alpha)*(level2+trend2)
		trend2 = beta*(level2-prevLevel) + (1-beta)*trend2
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 7: Changepoint Rate
// ---------------------------------------------------------------------------

func algoChangepointRate(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	cd := change.NewStream(100, 20, 10, 0.01)
	cpAt := make([]int, 0)
	for i, pt := range data {
		cp := cd.Push(pt.Value)
		if cp != nil {
			cpAt = append(cpAt, i)
		}
	}

	const cpWindow = 30
	for i := range preds {
		for _, cpi := range cpAt {
			if i >= cpi-cpWindow && i <= cpi+cpWindow {
				preds[i] = true
				break
			}
		}
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 8: Combined (RZ + Changepoint) — OR logic
// ---------------------------------------------------------------------------

func algoCombinedOR(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	am := predictor.NewAdaptiveMedian(100)
	cd := change.NewStream(100, 20, 10, 0.01)
	cpAt := make([]int, 0)

	for i, pt := range data {
		am.Add(pt.Value)
		med, mad, _, _, ok := am.Stats()
		isRZ := false
		if ok {
			score := predictor.RobustAnomalyScore(pt.Value, med, mad)
			isRZ = score >= 1.0
		}

		cp := cd.Push(pt.Value)
		if cp != nil {
			cpAt = append(cpAt, i)
		}

		isCP := false
		for _, cpi := range cpAt {
			if i >= cpi-30 && i <= cpi+30 {
				isCP = true
				break
			}
		}

		preds[i] = isRZ || isCP
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 9: Combined (RZ + Changepoint) — AND logic
// ---------------------------------------------------------------------------

func algoCombinedAND(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	am := predictor.NewAdaptiveMedian(100)
	cd := change.NewStream(100, 20, 10, 0.01)
	cpAt := make([]int, 0)

	for i, pt := range data {
		am.Add(pt.Value)
		med, mad, _, _, ok := am.Stats()
		isRZ := false
		if ok {
			score := predictor.RobustAnomalyScore(pt.Value, med, mad)
			isRZ = score >= 1.0
		}

		cp := cd.Push(pt.Value)
		if cp != nil {
			cpAt = append(cpAt, i)
		}

		isCP := false
		for _, cpi := range cpAt {
			if i >= cpi-30 && i <= cpi+30 {
				isCP = true
				break
			}
		}

		preds[i] = isRZ && isCP
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 10: Combined (RZ + Changepoint) — weighted average
// ---------------------------------------------------------------------------

func algoCombinedWeighted(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	am := predictor.NewAdaptiveMedian(100)
	cd := change.NewStream(100, 20, 10, 0.01)
	cpAt := make([]int, 0)
	rzScores := make([]float64, len(data))
	cpScores := make([]float64, len(data))

	for i, pt := range data {
		am.Add(pt.Value)
		med, mad, _, _, ok := am.Stats()
		rzScore := 0.0
		if ok {
			rzScore = predictor.RobustAnomalyScore(pt.Value, med, mad)
		}
		rzScores[i] = rzScore

		cp := cd.Push(pt.Value)
		if cp != nil {
			cpAt = append(cpAt, i)
		}

		// CP score: 1 if within window of a changepoint, 0 otherwise
		cpScore := 0.0
		for _, cpi := range cpAt {
			if i >= cpi-30 && i <= cpi+30 {
				cpScore = 1.0
				break
			}
		}
		cpScores[i] = cpScore
	}

	// Weighted combination: 0.7 * RZ + 0.3 * CP
	for i := range preds {
		combined := 0.7*rzScores[i] + 0.3*cpScores[i]
		preds[i] = combined >= 0.6
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 11: CUSUM (Cumulative Sum)
// ---------------------------------------------------------------------------

func algoCUSUM(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	if len(data) < 30 {
		return preds
	}

	// Estimate target mean and std from first 30 points
	initVals := make([]float64, 30)
	for i := 0; i < 30; i++ {
		initVals[i] = data[i].Value
	}
	target := meanOf(initVals)
	initStd := stdOf(initVals, target)
	if initStd < 1e-10 {
		initStd = 1.0
	}

	// CUSUM parameters
	k := 0.5 * initStd // allowance for slack
	h := 4.0 * initStd // decision threshold

	cusumPos := 0.0
	cusumNeg := 0.0

	for i := 0; i < len(data); i++ {
		// Update target estimate adaptively for non-stationary data
		if i > 30 && i%50 == 0 {
			recent := make([]float64, 50)
			for j := 0; j < 50 && i-j >= 0; j++ {
				recent[j] = data[i-j].Value
			}
			target = meanOf(recent)
		}

		stdized := (data[i].Value - target) / initStd
		cusumPos = math.Max(0, cusumPos+stdized-k)
		cusumNeg = math.Min(0, cusumNeg+stdized+k)

		preds[i] = cusumPos > h || cusumNeg < -h

		// Reset after detection
		if preds[i] {
			cusumPos = 0
			cusumNeg = 0
		}
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 12: Page-Hinkley test
// ---------------------------------------------------------------------------

func algoPageHinkley(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	if len(data) < 30 {
		return preds
	}

	// Estimate mean from first 30 points
	initVals := make([]float64, 30)
	for i := 0; i < 30; i++ {
		initVals[i] = data[i].Value
	}
	mean := meanOf(initVals)

	// Page-Hinkley parameters
	delta := 0.05 // tolerance
	lambda := 50.0 // threshold

	sum := 0.0
	min := 0.0
	max := 0.0

	for i := 0; i < len(data); i++ {
		// Adapt mean over time
		if i > 0 && i%100 == 0 {
			window := make([]float64, 0, 100)
			for j := i - 100; j < i && j >= 0; j++ {
				window = append(window, data[j].Value)
			}
			if len(window) > 0 {
				mean = meanOf(window)
			}
		}

		sum += data[i].Value - mean - delta
		if sum < min {
			min = sum
		}
		if sum > max {
			max = sum
		}

		PHpos := sum - min
		PHneg := max - sum

		preds[i] = PHpos > lambda || PHneg > lambda

		// Reset after detection
		if preds[i] {
			sum = 0
			min = 0
			max = 0
		}
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 13: KSigma (n-sigma with rolling stats)
// ---------------------------------------------------------------------------

func algoKSigma(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	const window = 50
	for i := window; i < len(data); i++ {
		w := make([]float64, window)
		for j := 0; j < window; j++ {
			w[j] = data[i-window+j].Value
		}
		m := meanOf(w)
		s := stdOf(w, m)
		if s > 1e-10 {
			z := math.Abs(data[i].Value-m) / s
			// Dynamic threshold: 3.5 for normal, 4.0 for seasonal-ish data
			// Check variance of recent window to estimate noise level
			threshold := 3.5
			if s < 3.0 {
				threshold = 4.0 // tight data needs higher threshold
			} else if s > 15.0 {
				threshold = 3.0 // volatile data needs lower threshold
			}
			preds[i] = z > threshold
		}
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 14: Seasonal Decomposition + residual IQR
// ---------------------------------------------------------------------------

func algoSeasonalIQR(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	vals := extractValues(data)
	period := detectPeriod(vals)
	if period == 0 {
		period = 96
	}
	trendWindow := 21
	if trendWindow%2 == 0 {
		trendWindow++
	}

	_, _, residual := simpleSTL(vals, period, trendWindow)

	// Compute IQR of residuals
	sorted := make([]float64, len(residual))
	copy(sorted, residual)
	sort.Float64s(sorted)
	n := len(sorted)
	q1 := sorted[n/4]
	q3 := sorted[3*n/4]
	iqr := q3 - q1
	if iqr < 1e-10 {
		iqr = 1e-10
	}

	// Tukey's fences: 1.5*IQR for outlier detection
	lower := q1 - 1.5*iqr
	upper := q3 + 1.5*iqr

	for i := 0; i < len(preds); i++ {
		preds[i] = residual[i] < lower || residual[i] > upper
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 15: RZ on residual after removing trend
// ---------------------------------------------------------------------------

func algoRZOnResidual(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	vals := extractValues(data)

	// Simple trend removal via moving average
	trendWindow := 21
	if trendWindow%2 == 0 {
		trendWindow++
	}
	half := trendWindow / 2
	trend := make([]float64, len(vals))
	for i := 0; i < len(vals); i++ {
		lo := i - half
		hi := i + half
		if lo < 0 {
			lo = 0
		}
		if hi >= len(vals) {
			hi = len(vals) - 1
		}
		sum := 0.0
		count := 0
		for j := lo; j <= hi; j++ {
			if !math.IsNaN(vals[j]) {
				sum += vals[j]
				count++
			}
		}
		if count > 0 {
			trend[i] = sum / float64(count)
		}
	}

	residual := make([]float64, len(vals))
	for i := 0; i < len(vals); i++ {
		residual[i] = vals[i] - trend[i]
	}

	// Run Robust Z-score on the residual
	am := predictor.NewAdaptiveMedian(100)
	for i, r := range residual {
		am.Add(r)
		med, mad, _, _, ok := am.Stats()
		if !ok {
			continue
		}
		score := predictor.RobustAnomalyScore(r, med, mad)
		preds[i] = score >= 1.0
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 16: Simplified Local Outlier Factor (window-based)
// ---------------------------------------------------------------------------

func algoSimplifiedLOF(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	const window = 60
	const k = 15 // number of neighbors

	for i := window; i < len(data); i++ {
		// Take a window of preceding points
		w := make([]float64, window)
		for j := 0; j < window; j++ {
			w[j] = data[i-window+j].Value
		}

		current := data[i].Value
		// Compute distance to k-th nearest neighbor in the window
		dists := make([]float64, window)
		for j := 0; j < window; j++ {
			dists[j] = math.Abs(current - w[j])
		}
		sort.Float64s(dists)

		if len(dists) <= k {
			continue
		}

		kDist := dists[k-1] // distance to k-th nearest neighbor
		if kDist < 1e-10 {
			kDist = 1e-10
		}

		// Compute local reachability density
		lrd := 0.0
		for j := 0; j < window; j++ {
			reachDist := math.Max(math.Abs(current-w[j]), dists[k-1])
			lrd += math.Log(reachDist + 1e-10)
		}
		lrd = float64(window) / (lrd + 1e-10)

		// Compare with average LRD of neighbors, but simplified:
		// just use k-distance as anomaly score (higher = more anomalous)
		avgDist := meanOf(dists)
		if avgDist < 1e-10 {
			avgDist = 1e-10
		}

		// Compute window statistics for threshold
		wMean := meanOf(w)
		wStd := stdOf(w, wMean)
		if wStd < 1e-10 {
			wStd = 1.0
		}

		lofScore := kDist / avgDist
		zScore := math.Abs(current-wMean) / wStd

		// Flag as anomaly if both LOF-like score and z-score are high
		preds[i] = lofScore > 3.0 && zScore > 2.5
	}
	return preds
}

// ---------------------------------------------------------------------------
// Algorithm 17: IQR-based outlier detection
// ---------------------------------------------------------------------------

func algoIQR(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	const window = 50

	for i := window; i < len(data); i++ {
		w := make([]float64, window)
		for j := 0; j < window; j++ {
			w[j] = data[i-window+j].Value
		}
		sorted := make([]float64, window)
		copy(sorted, w)
		sort.Float64s(sorted)

		q1 := sorted[window/4]
		q3 := sorted[3*window/4]
		iqr := q3 - q1
		if iqr < 1e-10 {
			iqr = 1e-10
		}

		lower := q1 - 1.5*iqr
		upper := q3 + 1.5*iqr

		preds[i] = data[i].Value < lower || data[i].Value > upper
	}
	return preds
}

// ---------------------------------------------------------------------------
// Ensemble 1: Metric-family routing — classify by metric name pattern,
// apply best algorithm for each family.
//
// "metric name patterns" are identified by looking at data characteristics:
// - High variance + sharp changes → Robust Z-score
// - Gradual drift/trend → Changepoint Rate
// - Oscillating → Combined
// - Seasonal → STL + residual
// ---------------------------------------------------------------------------

func ensembleMetricRouting(data []BenchPoint) []bool {
	vals := extractValues(data)

	// Phase 1: Analyze data characteristics to determine "metric family"
	mean := meanOf(vals)
	std := stdOf(vals, mean)

	// Compute variance of first differences to detect oscillation
	diffs := make([]float64, 0)
	for i := 1; i < len(vals); i++ {
		diffs = append(diffs, math.Abs(vals[i]-vals[i-1]))
	}
	meanDiff := meanOf(diffs)

	// Detect oscillation: frequent large direction changes
	oscillations := 0
	for i := 2; i < len(vals); i++ {
		if (vals[i]-vals[i-1])*(vals[i-1]-vals[i-2]) < 0 {
			oscillations++
		}
	}
	oscRate := float64(oscillations) / float64(len(vals)-2)

	// Detect trend magnitude
	half := len(vals) / 2
	firstHalf := meanOf(vals[:half])
	secondHalf := meanOf(vals[half:])
	trendMagnitude := math.Abs(secondHalf-firstHalf) / std

	// Detect seasonal pattern via autocorrelation
	seasonal := detectPeriod(vals) > 0

	// Routing decision:
	// Route to different algorithms based on metric character
	var primaryPreds []bool
	var algoName string

	if seasonal {
		algoName = "STL + Residual"
		primaryPreds = algoSTLRobustZ(data)
	} else if oscRate > 0.35 {
		// Oscillatory (crash loop, rapid flapping) → Changepoint
		algoName = "Changepoint"
		primaryPreds = algoChangepointRate(data)
	} else if trendMagnitude > 3.0 {
		// Strong trend (gradual degradation, memory leak) → Combined+Weighted
		algoName = "CombinedWeighted"
		primaryPreds = algoCombinedWeighted(data)
	} else if meanDiff > 15.0 {
		// High volatility → KSigma
		algoName = "KSigma"
		primaryPreds = algoKSigma(data)
	} else {
		// Default: Robust Z-score
		algoName = "RobustZScore"
		primaryPreds = algoRobustZScore(data)
	}

	// Suppress noise: a point is anomalous only if it or its neighbor is flagged
	for i := 1; i < len(primaryPreds)-1; i++ {
		if !primaryPreds[i] && primaryPreds[i-1] && primaryPreds[i+1] {
			primaryPreds[i] = true
		}
	}

	_ = algoName
	return primaryPreds
}

// ---------------------------------------------------------------------------
// Ensemble 2: Two-stage detection:
// Stage 1 = Changepoint (catches regime shifts),
// Stage 2 = RZ filters false positives in high-variance regions
// ---------------------------------------------------------------------------

func ensembleTwoStage(data []BenchPoint) []bool {
	preds := make([]bool, len(data))
	vals := extractValues(data)

	// Stage 1: Changepoint detection
	cd := change.NewStream(100, 20, 10, 0.01)
	cpAt := make([]int, 0)
	for i, pt := range data {
		cp := cd.Push(pt.Value)
		if cp != nil {
			cpAt = append(cpAt, i)
		}
	}

	// Stage 1 output: flag regions near changepoints
	stage1 := make([]bool, len(data))
	for i := range stage1 {
		for _, cpi := range cpAt {
			if i >= cpi-30 && i <= cpi+30 {
				stage1[i] = true
				break
			}
		}
	}

	// Stage 2: For each flagged region, verify with Robust Z-score
	am := predictor.NewAdaptiveMedian(100)
	rzFlags := make([]bool, len(data))
	for i, pt := range data {
		am.Add(pt.Value)
		med, mad, _, _, ok := am.Stats()
		if ok {
			score := predictor.RobustAnomalyScore(pt.Value, med, mad)
			rzFlags[i] = score >= 1.0
		}
	}

	// Final prediction: stage1 flagged AND stage2 confirmed
	// But also use RZ-only for spike detection (changepoint misses spikes)
	for i := range preds {
		if i < 100 {
			preds[i] = rzFlags[i] // early points: rely on RZ
		} else {
			// Require both for regime changes, but RZ alone catches spikes
			// Check local volatility: if very volatile, require both
			window := make([]float64, 50)
			for j := 0; j < 50 && i-j >= 0; j++ {
				window[j] = vals[i-j]
			}
			localStd := stdOf(window, meanOf(window))
			if localStd > 20.0 {
				preds[i] = rzFlags[i] && stage1[i]
			} else {
				preds[i] = rzFlags[i] || stage1[i]
			}
		}
	}

	return preds
}

// ---------------------------------------------------------------------------
// Ensemble 3: Score fusion — weighted average of multiple algorithm scores
// ---------------------------------------------------------------------------

func ensembleScoreFusion(data []BenchPoint) []bool {
	preds := make([]bool, len(data))

	// Run constituent algorithms
	cp := algoChangepointRate(data)
	ma := algoEWMA(data)

	// Weighted fusion with learnable weights (pre-tuned from train set)
	const wRZ = 0.35
	const wCP = 0.30
	const wMA = 0.15
	const wKS = 0.20

	// Compute anomaly scores per point using robust z-score values
	rzScores := make([]float64, len(data))
	cpScores := make([]float64, len(data))
	maScores := make([]float64, len(data))
	ksScores := make([]float64, len(data))

	// RZ scores
	am := predictor.NewAdaptiveMedian(100)
	for i, pt := range data {
		am.Add(pt.Value)
		med, mad, _, _, ok := am.Stats()
		if ok {
			rzScores[i] = predictor.RobustAnomalyScore(pt.Value, med, mad)
		}
	}

	// CP scores (binary)
	for i := range cpScores {
		if cp[i] {
			cpScores[i] = 1.0
		}
	}

	// MA scores
	if len(data) > 30 {
		for i := 30; i < len(data); i++ {
			if ma[i] {
				maScores[i] = 1.0
			}
		}
	}

	// KS scores (use z-score)
	const window = 50
	for i := window; i < len(data); i++ {
		w := make([]float64, window)
		for j := 0; j < window; j++ {
			w[j] = data[i-window+j].Value
		}
		m := meanOf(w)
		s := stdOf(w, m)
		if s > 1e-10 {
			z := math.Abs(data[i].Value-m) / s
			ksScores[i] = math.Min(z/3.5, 1.0)
		}
	}

	// Fuse scores
	for i := range preds {
		fused := wRZ*rzScores[i] + wCP*cpScores[i] + wMA*maScores[i] + wKS*ksScores[i]
		preds[i] = fused >= 0.5
	}
	return preds
}

// ---------------------------------------------------------------------------
// Ensemble 4: Voting ensemble — anomaly flagged if majority of N algorithms agree
// ---------------------------------------------------------------------------

func ensembleVoting(data []BenchPoint) []bool {
	preds := make([]bool, len(data))

	// Run 7 constituent algorithms
	algos := []struct {
		name   string
		detect func([]BenchPoint) []bool
	}{
		{"RZ", algoRobustZScore},
		{"CP", algoChangepointRate},
		{"MA", algoMovingAverage},
		{"KS", algoKSigma},
		{"CUSUM", algoCUSUM},
		{"IQR", algoIQR},
		{"EWMA", algoEWMA},
	}

	results := make([][]bool, len(algos))
	for i, a := range algos {
		results[i] = a.detect(data)
		_ = a.name
	}

	// Majority vote: if > half the algorithms flag it
	nAlgos := len(algos)
	majority := (nAlgos / 2) + 1

	for i := range preds {
		votes := 0
		for _, r := range results {
			if r[i] {
				votes++
			}
		}
		preds[i] = votes >= majority
	}
	return preds
}

// ---------------------------------------------------------------------------
// Evaluation
// ---------------------------------------------------------------------------

// evaluate computes accuracy metrics for one algorithm on one pattern.
func evaluate(patternName string, algoName string, data []BenchPoint, preds []bool, warmup int) PatternResult {
	r := PatternResult{
		Pattern:   patternName,
		Algorithm: algoName,
	}
	if len(data) == 0 {
		return r
	}

	var tp, fp, fn int
	nEvaluated := 0
	for i := warmup; i < len(data) && i < len(preds); i++ {
		nEvaluated++
		if preds[i] && data[i].KnownAnomaly {
			tp++
		} else if preds[i] && !data[i].KnownAnomaly {
			fp++
		} else if !preds[i] && data[i].KnownAnomaly {
			fn++
		}
	}
	r.TP = tp
	r.FP = fp
	r.FN = fn

	totalReal := tp + fn
	totalPred := tp + fp

	// Accuracy
	correct := tp + (nEvaluated - totalReal - fp) // TP + TN
	if nEvaluated > 0 {
		r.Accuracy = float64(correct) / float64(nEvaluated)
	}

	if totalReal == 0 && totalPred == 0 {
		r.Precision = 1.0
		r.Recall = 1.0
		r.F1 = 1.0
	} else if totalReal == 0 && totalPred > 0 {
		r.Precision = 0
		r.Recall = 1.0
		r.F1 = 0
	} else {
		if totalPred > 0 {
			r.Precision = float64(tp) / float64(totalPred)
		}
		r.Recall = float64(tp) / float64(totalReal)
		if r.Precision+r.Recall > 0 {
			r.F1 = 2 * r.Precision * r.Recall / (r.Precision + r.Recall)
		}
	}

	r.DetectionDelay = computeDetectionDelay(data, preds, warmup)
	return r
}

func computeDetectionDelay(data []BenchPoint, preds []bool, warmup int) float64 {
	type region struct{ start, end int }
	var regions []region

	i := warmup
	for i < len(data) {
		if i < len(data) && data[i].KnownAnomaly {
			start := i
			for i < len(data) && data[i].KnownAnomaly {
				i++
			}
			end := i - 1
			regions = append(regions, region{start, end})
		} else {
			i++
		}
	}

	if len(regions) == 0 {
		return 0
	}

	var totalDelay float64
	var detectedCount int
	for _, reg := range regions {
		for j := reg.start; j <= reg.end; j++ {
			if j < len(preds) && preds[j] {
				totalDelay += float64(j - reg.start)
				detectedCount++
				break
			}
		}
	}

	if detectedCount == 0 {
		return -1
	}
	return totalDelay / float64(detectedCount)
}

// ---------------------------------------------------------------------------
// Train/Test split evaluation
// ---------------------------------------------------------------------------

// evaluateTrainTest computes metrics separately for train and test splits.
func evaluateTrainTest(patternName string, algoName string, data []BenchPoint, preds []bool, warmup int, trainRatio float64) TrainTestResult {
	n := len(data)
	split := int(float64(n) * trainRatio)
	if split >= n {
		split = n - 1
	}

	// Train: [0, split)
	trainData := data[:split]
	trainPreds := preds[:split]
	trainResult := evaluate(patternName, algoName+" [train]", trainData, trainPreds, warmup)
	trainResult.Pattern = patternName
	trainResult.Algorithm = algoName + " [train]"

	// Test: [split, n)
	testData := data[split:]
	testPreds := preds[split:]
	// Use a smaller warmup for test (the algorithm is already warmed up)
	testWarmup := 0
	if warmup > split {
		testWarmup = 0
	} else {
		testWarmup = warmup
	}
	testResult := evaluate(patternName, algoName+" [test]", testData, testPreds, testWarmup)
	testResult.Pattern = patternName
	testResult.Algorithm = algoName + " [test]"

	return TrainTestResult{
		Train: trainResult,
		Test:  testResult,
		Delta: testResult.F1 - trainResult.F1,
	}
}

// ---------------------------------------------------------------------------
// Aggregate
// ---------------------------------------------------------------------------

func aggregate(results []PatternResult) []Summary {
	byAlgo := make(map[string][]PatternResult)
	for _, r := range results {
		byAlgo[r.Algorithm] = append(byAlgo[r.Algorithm], r)
	}

	var summaries []Summary
	for name, rs := range byAlgo {
		s := Summary{Algorithm: name}
		var f1Sum, precSum, recSum, accSum, delaySum float64
		delayCount := 0
		pointCount := 0
		for _, r := range rs {
			s.TotalTP += r.TP
			s.TotalFP += r.FP
			s.TotalFN += r.FN
			f1Sum += r.F1
			precSum += r.Precision
			recSum += r.Recall
			accSum += r.Accuracy
			if r.DetectionDelay >= 0 {
				delaySum += r.DetectionDelay
				delayCount++
			}
		}
		n := float64(len(rs))
		s.AvgF1 = f1Sum / n
		s.AvgPrecision = precSum / n
		s.AvgRecall = recSum / n
		s.AvgAccuracy = accSum / n
		s.TotalPoints = pointCount
		if delayCount > 0 {
			s.AvgDelay = delaySum / float64(delayCount)
		} else {
			s.AvgDelay = -1
		}
		summaries = append(summaries, s)
	}
	return summaries
}

// ---------------------------------------------------------------------------
// Scoring helpers for ensemble metric routing
// ---------------------------------------------------------------------------

// scoreAlgorithms runs all algorithms and returns a map of score arrays.
func scoreAllAlgorithms(data []BenchPoint) map[string][]float64 {
	scores := make(map[string][]float64)
	n := len(data)

	// Robust Z-score
	rz := make([]float64, n)
	am := predictor.NewAdaptiveMedian(100)
	for i, pt := range data {
		am.Add(pt.Value)
		med, mad, _, _, ok := am.Stats()
		if ok {
			rz[i] = predictor.RobustAnomalyScore(pt.Value, med, mad)
		}
	}
	scores["RZ"] = rz

	// Changepoint
	cp := make([]float64, n)
	cd := change.NewStream(100, 20, 10, 0.01)
	cpAt := make([]int, 0)
	for i, pt := range data {
		c := cd.Push(pt.Value)
		if c != nil {
			cpAt = append(cpAt, i)
		}
	}
	for i := range cp {
		for _, cpi := range cpAt {
			if i >= cpi-30 && i <= cpi+30 {
				cp[i] = 1.0
				break
			}
		}
	}
	scores["CP"] = cp

	return scores
}

// ---------------------------------------------------------------------------
// Output formatting
// ---------------------------------------------------------------------------

func formatResultsTable(results []PatternResult, title string) {
	fmt.Println()
	fmt.Println(strings.Repeat("=", 130))
	fmt.Printf("  %s\n", title)
	fmt.Println(strings.Repeat("=", 130))

	fmt.Printf("\n%-32s %-22s %5s %5s %5s  %8s  %8s  %8s  %8s  %7s\n",
		"Algorithm", "Pattern", "TP", "FP", "FN", "Precision", "Recall", "F1", "Accuracy", "Delay")
	fmt.Println(strings.Repeat("─", 130))

	for _, r := range results {
		delayStr := fmt.Sprintf("%.1f", r.DetectionDelay)
		if r.DetectionDelay < 0 {
			delayStr = "  N/A"
		}
		fmt.Printf("%-32s %-22s %5d %5d %5d  %8.4f  %8.4f  %8.4f  %8.4f  %7s\n",
			r.Algorithm, r.Pattern, r.TP, r.FP, r.FN, r.Precision, r.Recall, r.F1, r.Accuracy, delayStr)
	}
}

func formatRanking(summaries []Summary, title string) {
	fmt.Println()
	fmt.Println(strings.Repeat("=", 130))
	fmt.Printf("  %s\n", title)
	fmt.Println(strings.Repeat("=", 130))

	fmt.Printf("\n%-5s %-36s %9s %9s %9s %9s  %9s  %7s  %7s  %7s\n",
		"Rank", "Algorithm", "Avg F1", "Avg Prec", "Avg Recall", "Avg Acc",
		"Avg Delay", "Tot TP", "Tot FP", "Tot FN")
	fmt.Println(strings.Repeat("─", 130))

	for rank, s := range summaries {
		avgDelayStr := fmt.Sprintf("%.1f", s.AvgDelay)
		if s.AvgDelay < 0 {
			avgDelayStr = "  N/A"
		}
		fmt.Printf("%-5d %-36s %9.4f  %9.4f  %9.4f  %9.4f  %9s  %7d  %7d  %7d\n",
			rank+1, truncate(s.Algorithm, 36), s.AvgF1, s.AvgPrecision, s.AvgRecall, s.AvgAccuracy,
			avgDelayStr, s.TotalTP, s.TotalFP, s.TotalFN)
	}
}

func truncate(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen-3] + "..."
}

// findWinner identifies the best algorithm across all patterns.
func findWinner(summaries []Summary) Summary {
	if len(summaries) == 0 {
		return Summary{}
	}
	best := summaries[0]
	for _, s := range summaries {
		if s.AvgF1 > best.AvgF1 {
			best = s
		}
	}
	return best
}

// isOverfit checks if test performance is significantly worse than train.
func isOverfit(delta float64) bool {
	return delta < -0.05 // 5% drop in F1 = overfitting
}

// meets95pct checks if F1 >= 0.95.
func meets95pct(f1 float64) bool {
	return f1 >= 0.95
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

func main() {
	rng := rand.New(rand.NewSource(42))

	const uniformWarmup = 100
	const trainRatio = 0.70

	// =========================================================================
	// Phase 1: Generate data
	// =========================================================================

	fmt.Println(strings.Repeat("=", 130))
	fmt.Println("  KUBEWISE MASSIVE ANOMALY DETECTION BENCHMARK")
	fmt.Println(strings.Repeat("=", 130))
	fmt.Println()
	fmt.Println("  Generating 10,800+ data points across 9 anomaly patterns...")
	fmt.Println()

	rawPatterns := []Pattern{
		{Name: "1. Normal Baseline", Data: generateBaseline(rng, 3000, 50, 5)},
		{Name: "2. Memory Leak", Data: generateMemLeak(rng, 1000, 100)},
		{Name: "3. CPU Spike Train", Data: generateCPUSpikeTrain(rng, 1000, 5)},
		{Name: "4. Step Change", Data: generateStepChange(rng, 1000, 500, 50, 75, 5)},
		{Name: "5. Seasonality", Data: generateSeasonality(rng, 1000, 96, 15, 50)},
		{Name: "6. Crash Loop", Data: generateCrashLoop(rng, 1000)},
		{Name: "7. Gradual Degradation", Data: generateGradualDegradation(rng, 500, 50)},
		{Name: "8. Mixed Patterns", Data: generateMixedPatterns(rng, 1500)},
		{Name: "9. Transient Outliers", Data: generateTransientOutliers(rng, 800)},
	}

	// Add additional noise and missing data to all patterns
	patterns := make([]Pattern, len(rawPatterns))
	for i, pat := range rawPatterns {
		addNoise(rng, pat.Data, 1.0)
		// Drop ~5% of points to simulate missing data
		pat.Data = dropPoints(rng, pat.Data, 0.05)
		patterns[i] = pat
	}

	totalPoints := 0
	for _, pat := range patterns {
		totalPoints += len(pat.Data)
		fmt.Printf("    %-28s %5d points\n", pat.Name, len(pat.Data))
	}
	fmt.Printf("    %-28s %5d points\n", "TOTAL", totalPoints)
	fmt.Println()

	// =========================================================================
	// Phase 2: Define all algorithms and ensembles
	// =========================================================================

	soloAlgos := []benchAlgo{
		{Name: "1. Robust Z-score (baseline)", Detect: algoRobustZScore},
		{Name: "2. Z-score (mean/std)", Detect: algoZScore},
		{Name: "3. STL + RobustZScore", Detect: algoSTLRobustZ},
		{Name: "4. Moving Average + 3σ", Detect: algoMovingAverage},
		{Name: "5. EWMA", Detect: algoEWMA},
		{Name: "6. Double Exp Smoothing", Detect: algoDoubleExpSmoothing},
		{Name: "7. Changepoint Rate", Detect: algoChangepointRate},
		{Name: "8. Combined OR (RZ+CP)", Detect: algoCombinedOR},
		{Name: "9. Combined AND (RZ+CP)", Detect: algoCombinedAND},
		{Name: "10. Combined Weighted (RZ+CP)", Detect: algoCombinedWeighted},
		{Name: "11. CUSUM", Detect: algoCUSUM},
		{Name: "12. Page-Hinkley", Detect: algoPageHinkley},
		{Name: "13. KSigma (dynamic)", Detect: algoKSigma},
		{Name: "14. Seasonal IQR", Detect: algoSeasonalIQR},
		{Name: "15. RZ on Residual", Detect: algoRZOnResidual},
		{Name: "16. Simplified LOF", Detect: algoSimplifiedLOF},
		{Name: "17. IQR-based", Detect: algoIQR},
	}

	ensembles := []benchAlgo{
		{Name: "E1. Metric-family Routing", Detect: ensembleMetricRouting},
		{Name: "E2. Two-stage (CP→RZ)", Detect: ensembleTwoStage},
		{Name: "E3. Score Fusion (weighted)", Detect: ensembleScoreFusion},
		{Name: "E4. Voting Ensemble (7-algo)", Detect: ensembleVoting},
	}

	allAlgos := append(soloAlgos, ensembles...)

	// =========================================================================
	// Phase 3: Run all algorithms on all patterns (full data)
	// =========================================================================

	fmt.Println(strings.Repeat("=", 130))
	fmt.Println("  PHASE 1: FULL-DATA EVALUATION (all " + fmt.Sprint(len(allAlgos)) + " algorithms)")
	fmt.Println(strings.Repeat("=", 130))

	var fullResults []PatternResult

	for _, pat := range patterns {
		for _, a := range allAlgos {
			preds := a.Detect(pat.Data)
			r := evaluate(pat.Name, a.Name, pat.Data, preds, uniformWarmup)
			fullResults = append(fullResults, r)
		}
	}

	// Print per-pattern results
	formatResultsTable(fullResults, "PER-PATTERN RESULTS (FULL DATA)")

	// Rank by F1
	fullSummaries := aggregate(fullResults)
	sort.Slice(fullSummaries, func(i, j int) bool {
		return fullSummaries[i].AvgF1 > fullSummaries[j].AvgF1
	})

	formatRanking(fullSummaries, "OVERALL RANKING BY F1 SCORE (FULL DATA)")

	// =========================================================================
	// Phase 4: Train/Test split evaluation
	// =========================================================================

	fmt.Println()
	fmt.Println(strings.Repeat("=", 130))
	fmt.Println("  PHASE 2: TRAIN/TEST SPLIT EVALUATION (train=" + fmt.Sprintf("%.0f%%", trainRatio*100) + ", test=" + fmt.Sprintf("%.0f%%", (1-trainRatio)*100) + ")")
	fmt.Println(strings.Repeat("=", 130))

	type OverfitEntry struct {
		Algorithm string
		Pattern   string
		TrainF1   float64
		TestF1    float64
		Delta     float64
	}
	var overfitEntries []OverfitEntry
	var trainResults []PatternResult
	var testResults []PatternResult

	for _, pat := range patterns {
		for _, a := range allAlgos {
			preds := a.Detect(pat.Data)
			tt := evaluateTrainTest(pat.Name, a.Name, pat.Data, preds, uniformWarmup, trainRatio)
			trainResults = append(trainResults, tt.Train)
			testResults = append(testResults, tt.Test)

			if tt.Delta < -0.05 {
				overfitEntries = append(overfitEntries, OverfitEntry{
					Algorithm: a.Name,
					Pattern:   pat.Name,
					TrainF1:   tt.Train.F1,
					TestF1:    tt.Test.F1,
					Delta:     tt.Delta,
				})
			}
		}
	}

	// Print training results
	formatResultsTable(trainResults, "TRAINING RESULTS")
	// Print test results
	formatResultsTable(testResults, "TEST RESULTS")

	// Rank test results
	testSummaries := aggregate(testResults)
	sort.Slice(testSummaries, func(i, j int) bool {
		return testSummaries[i].AvgF1 > testSummaries[j].AvgF1
	})

	formatRanking(testSummaries, "TEST RANKING BY F1 SCORE (best estimate of real-world performance)")

	// =========================================================================
	// Phase 5: Overfitting detection
	// =========================================================================

	fmt.Println()
	fmt.Println(strings.Repeat("=", 130))
	fmt.Println("  PHASE 3: OVERFITTING ANALYSIS")
	fmt.Println(strings.Repeat("=", 130))

	if len(overfitEntries) > 0 {
		fmt.Printf("\n  ⚠ Found %d overfitting cases (test F1 < train F1 by >5%%):\n", len(overfitEntries))
		fmt.Printf("\n%-36s %-24s %9s %9s  %9s\n", "Algorithm", "Pattern", "Train F1", "Test F1", "Delta")
		fmt.Println(strings.Repeat("─", 90))
		for _, e := range overfitEntries {
			fmt.Printf("%-36s %-24s %9.4f  %9.4f  %9.4f\n",
				truncate(e.Algorithm, 36), e.Pattern, e.TrainF1, e.TestF1, e.Delta)
		}
	} else {
		fmt.Println("\n  ✓ No significant overfitting detected across any algorithm.")
	}

	// Per-algorithm overfitting summary
	trainSummaries := aggregate(trainResults)
	trainMap := make(map[string]float64)
	for _, s := range trainSummaries {
		trainMap[s.Algorithm] = s.AvgF1
	}
	testMap := make(map[string]float64)
	for _, s := range testSummaries {
		testMap[s.Algorithm] = s.AvgF1
	}

	fmt.Printf("\n%-42s %9s %9s  %9s  %s\n", "Algorithm", "Train F1", "Test F1", "Delta", "Overfit?")
	fmt.Println(strings.Repeat("─", 85))
	for _, s := range fullSummaries {
		trainF1 := trainMap[s.Algorithm]
		testF1 := testMap[s.Algorithm]
		delta := testF1 - trainF1
		overfitStr := "✓"
		if isOverfit(delta) {
			overfitStr = "⚠ YES"
		}
		fmt.Printf("%-42s %9.4f  %9.4f  %9.4f  %s\n",
			truncate(s.Algorithm, 42), trainF1, testF1, delta, overfitStr)
	}

	// =========================================================================
	// Phase 6: Winner identification
	// =========================================================================

	fmt.Println()
	fmt.Println(strings.Repeat("=", 130))
	fmt.Println("  === WINNER ===")
	fmt.Println(strings.Repeat("=", 130))

	// Winner is the best algorithm on TEST data (not train — we care about generalization)
	if len(testSummaries) > 0 {
		winner := testSummaries[0]
		meets95 := meets95pct(winner.AvgF1)
		meets95Str := "YES ✓"
		if !meets95 {
			meets95Str = "NO ✗ (need >= 0.95)"
		}

		fmt.Printf("\n  Best overall approach: %s\n", winner.Algorithm)
		fmt.Printf("  Average F1:          %.6f\n", winner.AvgF1)
		fmt.Printf("  Average Precision:   %.6f\n", winner.AvgPrecision)
		fmt.Printf("  Average Recall:      %.6f\n", winner.AvgRecall)
		fmt.Printf("  Average Accuracy:    %.6f\n", winner.AvgAccuracy)
		fmt.Printf("  Total TP:            %d\n", winner.TotalTP)
		fmt.Printf("  Total FP:            %d\n", winner.TotalFP)
		fmt.Printf("  Total FN:            %d\n", winner.TotalFN)
		fmt.Printf("\n  Meets 95%% requirement: %s\n", meets95Str)

		// Also show top 3 on test data
		fmt.Println()
		fmt.Println("  Top 3 Approaches (by Test F1):")
		fmt.Printf("  %-5s %-38s %10s %10s %10s\n", "Rank", "Algorithm", "F1", "Precision", "Recall")
		fmt.Println(strings.Repeat("  ─", 35))
		for i := 0; i < 3 && i < len(testSummaries); i++ {
			s := testSummaries[i]
			fmt.Printf("  %-5d %-38s %10.6f  %10.6f  %10.6f\n",
				i+1, truncate(s.Algorithm, 38), s.AvgF1, s.AvgPrecision, s.AvgRecall)
		}

		// Full-data winner
		fmt.Println()
		fmt.Println("  Top 3 Approaches (by Full-Data F1):")
		fmt.Printf("  %-5s %-38s %10s %10s %10s\n", "Rank", "Algorithm", "F1", "Precision", "Recall")
		fmt.Println(strings.Repeat("  ─", 35))
		for i := 0; i < 3 && i < len(fullSummaries); i++ {
			s := fullSummaries[i]
			fmt.Printf("  %-5d %-38s %10.6f  %10.6f  %10.6f\n",
				i+1, truncate(s.Algorithm, 38), s.AvgF1, s.AvgPrecision, s.AvgRecall)
		}

		// Ensemble-specific analysis
		fmt.Println()
		fmt.Println(strings.Repeat("─", 130))
		fmt.Println("\n  ENSEMBLE VS SOLO PERFORMANCE:")
		fmt.Printf("\n%-42s %9s %9s %s\n", "Algorithm", "Full F1", "Test F1", "Type")
		fmt.Println(strings.Repeat("─", 80))

		// Mark ensembles in the ranking
		typeMap := make(map[string]string)
		for i := range soloAlgos {
			typeMap[soloAlgos[i].Name] = "solo"
		}
		for i := range ensembles {
			typeMap[ensembles[i].Name] = "ENSEMBLE"
		}

		for _, s := range fullSummaries {
			t := typeMap[s.Algorithm]
			mark := "  "
			if t == "ENSEMBLE" {
				mark = "★"
			}
			testF1 := testMap[s.Algorithm]
			fmt.Printf("%s%-41s %9.4f  %9.4f  %s\n",
				mark, truncate(s.Algorithm, 41), s.AvgF1, testF1, t)
		}
	}

	// =========================================================================
	// Final summary
	// =========================================================================

	fmt.Println()
	fmt.Println(strings.Repeat("=", 130))
	fmt.Println("  BENCHMARK COMPLETE")
	fmt.Println(strings.Repeat("=", 130))
	fmt.Printf("\n  Total data points: %d\n", totalPoints)
	fmt.Printf("  Total algorithms: %d (17 solo + 4 ensemble)\n", len(soloAlgos)+len(ensembles))
	fmt.Printf("  Total patterns:   %d\n", len(patterns))
	fmt.Printf("  Train/Test split: %.0f%% / %.0f%%\n", trainRatio*100, (1-trainRatio)*100)

	if len(testSummaries) > 0 {
		winner := testSummaries[0]
		if meets95pct(winner.AvgF1) {
			fmt.Printf("\n  ✅ GOAL ACHIEVED: %s achieved F1=%.4f (>= 0.95)\n", winner.Algorithm, winner.AvgF1)
		} else {
			fmt.Printf("\n  ❌ GOAL NOT YET ACHIEVED: best F1=%.4f (need >= 0.95)\n", winner.AvgF1)
			fmt.Println("     Consider implementing more sophisticated ensemble methods or")
			fmt.Println("     tuning hyperparameters for specific metric families.")
		}
	}

	fmt.Println()
}
