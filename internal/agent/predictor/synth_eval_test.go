package predictor

import (
	"fmt"
	"math"
	"testing"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// This is a synthetic (in-memory) evaluation to estimate false/true positives for:
// - statistical anomaly detection (Predictor.Run)
// - pattern-based failure prediction (Predictor.RunPatterns)
//
// It generates two pods:
// - healthy pod: stable CPU/memory, low restart rate, ready
// - failing pod: rising restarts + not-ready + rising memory (plus a CrashLoopBackOff event)
//
// Run it with:
//
//	go test ./internal/agent/predictor -run TestSyntheticTPFP -v
func TestSyntheticTPFP(t *testing.T) {
	cfg := DefaultScorerConfig()
	cfg.MinWarmup = 6 // keep the synthetic run short but meaningful

	p := NewPredictor(cfg)
	p.AddPattern(&CrashLoopPattern{})
	p.AddPattern(&OOMPattern{})
	p.AddPattern(&DegradationPattern{})
	SetScrapeInterval(30 * time.Second)

	ns := "synth"
	healthy := "api-healthy"
	failing := "api-failing"

	// Approx 10 minutes of 30s scrapes.
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	steps := 20

	// Ground-truth: failing pod is "positive", healthy is "negative".
	type counts struct {
		statTP int
		statFP int
		patTP  int
		patFP  int
	}
	var c counts

	seenStat := map[string]bool{}
	seenPat := map[string]bool{}

	// Resource snapshot provides memory limits used by OOMPattern.
	resources := ResourceSnapshot{
		FailingPods: []string{models.FormatEntity(ns, failing)},
		PodResources: []PodResource{
			{Name: healthy, Namespace: ns, MemLimit: 256 * 1024 * 1024},
			{Name: failing, Namespace: ns, MemLimit: 256 * 1024 * 1024},
		},
	}

	// Provide a CrashLoopBackOff “event anomaly” for the failing pod, so the
	// CrashLoopPattern can use hasEvent(...) signals.
	events := []models.AnomalyRecord{
		{
			Entity:     models.FormatEntity(ns, failing),
			Namespace:  ns,
			MetricName: "k8s_event",
			Pattern:    "CrashLoopBackOff",
			Score:      1.0,
			Status:     models.AnomalyStatusDetected,
		},
	}

	// Small deterministic oscillation to avoid RNG and keep runs stable.
	wave := func(i int, amp float64) float64 {
		return math.Sin(float64(i)*0.7) * amp
	}

	for i := 0; i < steps; i++ {
		now := start.Add(time.Duration(i) * 30 * time.Second)

		// Healthy pod metrics: stable, tiny oscillation.
		healthyMem := 90e6 + wave(i, 2e6)   // ~90MB
		healthyCPU := 0.02 + wave(i, 0.005) // ~0.02 cores
		healthyRR := 0.00
		healthyNotReady := 0.0

		// Failing pod metrics: restarts rise, readiness flips, memory rises toward limit.
		failingMem := 110e6 + float64(i)*7e6 + wave(i, 2e6) // approaches 256MB
		failingCPU := 0.03 + wave(i, 0.01)
		failingRR := 0.02 + float64(i)*0.06 // rises each step
		failingNotReady := 0.0
		if i >= 10 {
			failingNotReady = 1.0
		}

		metrics := []MetricResult{
			{
				Name: "pod_memory_usage",
				Values: []MetricPoint{
					{Timestamp: now, Value: healthyMem, Labels: map[string]string{"namespace": ns, "pod": healthy}},
					{Timestamp: now, Value: failingMem, Labels: map[string]string{"namespace": ns, "pod": failing}},
				},
			},
			{
				Name: "pod_cpu_usage",
				Values: []MetricPoint{
					{Timestamp: now, Value: healthyCPU, Labels: map[string]string{"namespace": ns, "pod": healthy}},
					{Timestamp: now, Value: failingCPU, Labels: map[string]string{"namespace": ns, "pod": failing}},
				},
			},
			{
				Name: "restart_rate",
				Values: []MetricPoint{
					{Timestamp: now, Value: healthyRR, Labels: map[string]string{"namespace": ns, "pod": healthy}},
					{Timestamp: now, Value: failingRR, Labels: map[string]string{"namespace": ns, "pod": failing}},
				},
			},
			{
				Name: "pod_not_ready",
				Values: []MetricPoint{
					{Timestamp: now, Value: healthyNotReady, Labels: map[string]string{"namespace": ns, "pod": healthy}},
					{Timestamp: now, Value: failingNotReady, Labels: map[string]string{"namespace": ns, "pod": failing}},
				},
			},
		}

		stat, err := p.Run(metrics)
		if err != nil {
			t.Fatalf("Run: %v", err)
		}
		enriched := p.PreparePatternMetrics(metrics)
		pats := p.RunPatterns(enriched, events, resources)

		for _, r := range stat {
			k := r.Namespace + "/" + r.Entity
			seenStat[k] = true
		}
		for _, r := range pats {
			k := r.Namespace + "/" + r.Entity + "/" + r.MetricName
			seenPat[k] = true
		}
	}

	// Reduce “seen” sets into TP/FP counts.
	for k := range seenStat {
		switch k {
		case ns + "/" + failing:
			c.statTP++
		case ns + "/" + healthy:
			c.statFP++
		}
	}
	for k := range seenPat {
		// Pattern keys include MetricName, but TP/FP is per-entity for this summary.
		if len(k) >= len(ns+"/"+failing) && k[:len(ns+"/"+failing)] == ns+"/"+failing {
			c.patTP++
			continue
		}
		if len(k) >= len(ns+"/"+healthy) && k[:len(ns+"/"+healthy)] == ns+"/"+healthy {
			c.patFP++
			continue
		}
	}

	precision := func(tp, fp int) float64 {
		if tp+fp == 0 {
			return 0
		}
		return float64(tp) / float64(tp+fp)
	}
	recall := func(tp int) float64 {
		// Ground truth has exactly 1 positive entity.
		if tp == 0 {
			return 0
		}
		return 1
	}

	t.Logf("Synthetic run: steps=%d interval=30s warmup=%d", steps, cfg.MinWarmup)
	t.Logf("Statistical anomalies: TP=%d FP=%d precision=%.2f recall=%.2f", c.statTP, c.statFP, precision(c.statTP, c.statFP), recall(c.statTP))
	t.Logf("Pattern predictions:   TP=%d FP=%d precision=%.2f recall=%.2f", c.patTP, c.patFP, precision(c.patTP, c.patFP), recall(c.patTP))
}

// A more realistic synthetic benchmark:
// - many healthy pods with noise + transient spikes
// - multiple failing pods with staggered failure onsets
// - missing samples / jitter
// - evaluates per-timestep precision/recall and FP rate per pod-hour
func TestSyntheticTPFP_Complicated(t *testing.T) {
	cfg := DefaultScorerConfig()
	cfg.MinWarmup = 10

	p := NewPredictor(cfg)
	p.AddPattern(&CrashLoopPattern{})
	p.AddPattern(&OOMPattern{})
	p.AddPattern(&DegradationPattern{})
	SetScrapeInterval(30 * time.Second)

	type podKind string
	const (
		kindHealthy podKind = "healthy"
		kindFailing podKind = "failing"
	)
	type podSpec struct {
		ns          string
		name        string
		kind        podKind
		failStartIx int // for failing pods only
		failMode    string
		memLimit    float64
	}

	// Deterministic pseudo-rng (no math/rand global).
	seed := uint64(0xC0FFEE)
	nextU01 := func() float64 {
		// xorshift64*
		seed ^= seed >> 12
		seed ^= seed << 25
		seed ^= seed >> 27
		x := seed * 2685821657736338717
		// use top 53 bits for float64
		return float64(x>>11) / float64(uint64(1)<<53)
	}
	normalish := func(std float64) float64 {
		// Box-Muller with deterministic uniform.
		u1 := math.Max(nextU01(), 1e-9)
		u2 := nextU01()
		z := math.Sqrt(-2*math.Log(u1)) * math.Cos(2*math.Pi*u2)
		return z * std
	}

	// Build pod population.
	const (
		ns         = "synth"
		steps      = 120 // 1h at 30s
		nHealthy   = 200
		nFailing   = 20
		missRate   = 0.08 // drop 8% of points (simulates scrape gaps)
		spikeRate  = 0.03 // transient spikes in healthy
		baseMemLim = 256 * 1024 * 1024
	)
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

	pods := make([]podSpec, 0, nHealthy+nFailing)
	for i := 0; i < nHealthy; i++ {
		pods = append(pods, podSpec{
			ns:       ns,
			name:     fmt.Sprintf("h-%03d", i),
			kind:     kindHealthy,
			memLimit: baseMemLim,
		})
	}
	for i := 0; i < nFailing; i++ {
		failStart := 25 + int(nextU01()*60) // between ~12.5m and ~42.5m
		mode := "crashloop"
		if i%3 == 1 {
			mode = "oom"
		} else if i%3 == 2 {
			mode = "notready"
		}
		pods = append(pods, podSpec{
			ns:          ns,
			name:        fmt.Sprintf("f-%03d", i),
			kind:        kindFailing,
			failStartIx: failStart,
			failMode:    mode,
			memLimit:    baseMemLim,
		})
	}

	// Prepare static resources (memory limits) for OOM pattern.
	resources := ResourceSnapshot{}
	for _, pd := range pods {
		resources.PodResources = append(resources.PodResources, PodResource{
			Name:      pd.name,
			Namespace: pd.ns,
			MemLimit:  pd.memLimit,
		})
	}

	// Confusion bookkeeping for statistical and pattern signals.
	type confusion struct{ tp, fp, tn, fn int }
	var statC, patC, combC confusion

	// FP rate normalization: fp alerts per pod-hour (only on negatives).
	var statNegPodSteps, patNegPodSteps, combNegPodSteps int

	// For each step, build metrics.
	for ix := 0; ix < steps; ix++ {
		now := start.Add(time.Duration(ix) * 30 * time.Second)

		// Build failing set for this step (used by DegradationPattern).
		resources.FailingPods = resources.FailingPods[:0]
		stepEvents := make([]models.AnomalyRecord, 0, nFailing)

		// Metric buffers.
		memPts := make([]MetricPoint, 0, len(pods))
		cpuPts := make([]MetricPoint, 0, len(pods))
		rrPts := make([]MetricPoint, 0, len(pods))
		nrPts := make([]MetricPoint, 0, len(pods))

		for _, pd := range pods {
			// Simulate missing points.
			if nextU01() < missRate {
				continue
			}

			labels := map[string]string{"namespace": pd.ns, "pod": pd.name}

			// Healthy baseline.
			mem := 90e6 + normalish(6e6)
			cpu := 0.02 + normalish(0.007)
			restartRate := math.Max(0, normalish(0.01))
			notReady := 0.0

			// Occasional transient spikes in healthy.
			if pd.kind == kindHealthy && nextU01() < spikeRate {
				which := int(nextU01() * 3)
				switch which {
				case 0:
					mem += 80e6
				case 1:
					cpu += 0.2
				default:
					restartRate += 0.3
				}
			}

			positive := false
			if pd.kind == kindFailing && ix >= pd.failStartIx {
				positive = true
				switch pd.failMode {
				case "crashloop":
					restartRate += 0.25 + float64(ix-pd.failStartIx)*0.08
					notReady = 1.0
					stepEvents = append(stepEvents, models.AnomalyRecord{
						Entity:     models.FormatEntity(pd.ns, pd.name),
						Namespace:  pd.ns,
						MetricName: "k8s_event",
						Pattern:    "CrashLoopBackOff",
						Score:      1.0,
						Status:     models.AnomalyStatusDetected,
					})
					resources.FailingPods = append(resources.FailingPods, models.FormatEntity(pd.ns, pd.name))
				case "oom":
					mem += float64(ix-pd.failStartIx) * 10e6
					if mem > pd.memLimit*0.92 {
						stepEvents = append(stepEvents, models.AnomalyRecord{
							Entity:     models.FormatEntity(pd.ns, pd.name),
							Namespace:  pd.ns,
							MetricName: "k8s_event",
							Pattern:    "OOMKilling",
							Score:      1.0,
							Status:     models.AnomalyStatusDetected,
						})
					}
				default: // notready
					notReady = 1.0
					mem += 20e6 + normalish(5e6)
				}
			}

			_ = positive // used in eval section below

			memPts = append(memPts, MetricPoint{Timestamp: now, Value: math.Max(0, mem), Labels: labels})
			cpuPts = append(cpuPts, MetricPoint{Timestamp: now, Value: math.Max(0, cpu), Labels: labels})
			rrPts = append(rrPts, MetricPoint{Timestamp: now, Value: math.Max(0, restartRate), Labels: labels})
			nrPts = append(nrPts, MetricPoint{Timestamp: now, Value: notReady, Labels: labels})
		}

		metrics := []MetricResult{
			{Name: "pod_memory_usage", Values: memPts},
			{Name: "pod_cpu_usage", Values: cpuPts},
			{Name: "restart_rate", Values: rrPts},
			{Name: "pod_not_ready", Values: nrPts},
		}

		stat, err := p.Run(metrics)
		if err != nil {
			t.Fatalf("Run: %v", err)
		}
		enriched := p.PreparePatternMetrics(metrics)
		pats := p.RunPatterns(enriched, stepEvents, resources)

		// Convert results to “alert sets” for this timestep.
		statAlert := map[string]bool{}
		for _, r := range stat {
			statAlert[r.Namespace+"/"+r.Entity] = true
		}
		patAlert := map[string]bool{}
		for _, r := range pats {
			patAlert[r.Namespace+"/"+r.Entity] = true
		}

		// Evaluate each pod at this timestep.
		for _, pd := range pods {
			pos := pd.kind == kindFailing && ix >= pd.failStartIx
			key := pd.ns + "/" + pd.name

			sa := statAlert[key]
			pa := patAlert[key]
			ca := sa || pa

			// Statistical confusion.
			switch {
			case pos && sa:
				statC.tp++
			case pos && !sa:
				statC.fn++
			case !pos && sa:
				statC.fp++
			case !pos && !sa:
				statC.tn++
			}
			if !pos {
				statNegPodSteps++
			}

			// Pattern confusion.
			switch {
			case pos && pa:
				patC.tp++
			case pos && !pa:
				patC.fn++
			case !pos && pa:
				patC.fp++
			case !pos && !pa:
				patC.tn++
			}
			if !pos {
				patNegPodSteps++
			}

			// Combined confusion (statistical OR pattern).
			switch {
			case pos && ca:
				combC.tp++
			case pos && !ca:
				combC.fn++
			case !pos && ca:
				combC.fp++
			case !pos && !ca:
				combC.tn++
			}
			if !pos {
				combNegPodSteps++
			}
		}
	}

	precision := func(tp, fp int) float64 {
		if tp+fp == 0 {
			return 0
		}
		return float64(tp) / float64(tp+fp)
	}
	recall := func(tp, fn int) float64 {
		if tp+fn == 0 {
			return 0
		}
		return float64(tp) / float64(tp+fn)
	}
	fpPerPodHour := func(fp int, negPodSteps int) float64 {
		// each step is 30s
		hours := float64(negPodSteps) * 30 / 3600
		if hours <= 0 {
			return 0
		}
		return float64(fp) / hours
	}

	accuracy := func(tp, tn, fp, fn int) float64 {
		total := tp + tn + fp + fn
		if total == 0 {
			return 0
		}
		return float64(tp+tn) / float64(total)
	}

	t.Logf("Complicated synthetic benchmark: pods=%d (healthy=%d failing=%d) steps=%d interval=30s", len(pods), nHealthy, nFailing, steps)
	t.Logf("Statistical: TP=%d FP=%d TN=%d FN=%d precision=%.3f recall=%.3f FP/(pod-hour)=%.4f",
		statC.tp, statC.fp, statC.tn, statC.fn, precision(statC.tp, statC.fp), recall(statC.tp, statC.fn), fpPerPodHour(statC.fp, statNegPodSteps))
	t.Logf("Pattern:     TP=%d FP=%d TN=%d FN=%d precision=%.3f recall=%.3f FP/(pod-hour)=%.4f",
		patC.tp, patC.fp, patC.tn, patC.fn, precision(patC.tp, patC.fp), recall(patC.tp, patC.fn), fpPerPodHour(patC.fp, patNegPodSteps))
	t.Logf("Combined:    TP=%d FP=%d TN=%d FN=%d accuracy=%.3f precision=%.3f recall=%.3f FP/(pod-hour)=%.4f",
		combC.tp, combC.fp, combC.tn, combC.fn,
		accuracy(combC.tp, combC.tn, combC.fp, combC.fn),
		precision(combC.tp, combC.fp),
		recall(combC.tp, combC.fn),
		fpPerPodHour(combC.fp, combNegPodSteps),
	)

}
