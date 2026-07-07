package predictor

import (
	"testing"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

const testMemLimit = 1 << 30 // 1 GiB

func memBytes(ratio float64) float64 {
	return ratio * float64(testMemLimit)
}

func TestOOMPatternNoEventLowMemory(t *testing.T) {
	p := &OOMPattern{}
	metrics := []MetricResult{
		{
			Name: "pod_memory_usage",
			Values: []MetricPoint{
				{Value: memBytes(0.3), Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
				{Value: memBytes(0.3), Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
			},
		},
	}
	matches := p.Match(metrics, nil, ResourceSnapshot{})
	if len(matches) != 0 {
		t.Fatal("expected no OOM match for flat low memory without trend")
	}
}

func TestOOMPatternPredictsOOM(t *testing.T) {
	p := &OOMPattern{}
	resources := ResourceSnapshot{
		PodResources: []PodResource{{Name: "web-1", Namespace: "default", MemLimit: testMemLimit}},
	}
	metrics := []MetricResult{
		{
			Name: "pod_memory_usage",
			Values: []MetricPoint{
				{Value: memBytes(0.6), Timestamp: time.Now().Add(-60 * time.Second), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
				{Value: memBytes(0.75), Timestamp: time.Now().Add(-30 * time.Second), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
				{Value: memBytes(0.9), Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
			},
		},
	}
	matches := p.Match(metrics, nil, resources)
	if len(matches) == 0 {
		t.Fatal("expected OOM match with rising memory and no prior event")
	}
	if matches[0].Pattern != "OOMRisk" {
		t.Fatalf("expected OOMRisk pattern, got %s", matches[0].Pattern)
	}
	if matches[0].Confidence < 0.7 {
		t.Fatalf("expected confidence >= 0.7, got %f", matches[0].Confidence)
	}
	if matches[0].TimeToFailure <= 0 {
		t.Fatalf("expected positive TimeToFailure, got %v", matches[0].TimeToFailure)
	}
}

func TestOOMPatternMemoryFlat(t *testing.T) {
	p := &OOMPattern{}
	now := time.Now()
	metrics := []MetricResult{
		{
			Name: "pod_memory_usage",
			Values: []MetricPoint{
				{Value: memBytes(0.3), Timestamp: now, Labels: map[string]string{"pod": "web-1"}},
				{Value: memBytes(0.3), Timestamp: now.Add(30 * time.Second), Labels: map[string]string{"pod": "web-1"}},
				{Value: memBytes(0.3), Timestamp: now.Add(60 * time.Second), Labels: map[string]string{"pod": "web-1"}},
			},
		},
	}
	events := []models.AnomalyRecord{
		{Entity: "web-1", Pattern: "OOMKilled"},
	}
	matches := p.Match(metrics, events, ResourceSnapshot{})
	if len(matches) != 0 {
		t.Fatal("expected no OOM match with flat memory even with prior OOM event")
	}
}

func TestOOMPatternMultiplePods(t *testing.T) {
	p := &OOMPattern{}
	resources := ResourceSnapshot{
		PodResources: []PodResource{{Name: "web-1", Namespace: "ns1", MemLimit: testMemLimit}},
	}
	now := time.Now()
	metrics := []MetricResult{
		{
			Name: "pod_memory_usage",
			Values: []MetricPoint{
				{Value: memBytes(0.6), Timestamp: now, Labels: map[string]string{"pod": "web-1", "namespace": "ns1"}},
				{Value: memBytes(0.8), Timestamp: now.Add(30 * time.Second), Labels: map[string]string{"pod": "web-1", "namespace": "ns1"}},
				{Value: memBytes(0.3), Timestamp: now, Labels: map[string]string{"pod": "web-2", "namespace": "ns2"}},
				{Value: memBytes(0.3), Timestamp: now.Add(30 * time.Second), Labels: map[string]string{"pod": "web-2", "namespace": "ns2"}},
			},
		},
	}
	matches := p.Match(metrics, nil, resources)
	if len(matches) == 0 {
		t.Fatal("expected at least one OOM match")
	}
	for _, m := range matches {
		if m.Entity == "web-2" {
			t.Fatal("web-2 should not match with flat low memory")
		}
	}
}

func TestCrashLoopPatternPredictsCrash(t *testing.T) {
	p := &CrashLoopPattern{}
	metrics := []MetricResult{
		{
			Name: "restart_rate",
			Values: []MetricPoint{
				{Value: 0.05, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1", "namespace": "prod"}},
				{Value: 0.15, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1", "namespace": "prod"}},
				{Value: 0.40, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1", "namespace": "prod"}},
			},
		},
	}
	matches := p.Match(metrics, nil, ResourceSnapshot{})
	if len(matches) == 0 {
		t.Fatal("expected CrashLoop match with rising restarts and no event")
	}
	if matches[0].Confidence < 0.6 {
		t.Fatalf("expected confidence >= 0.6, got %f", matches[0].Confidence)
	}
}

func TestCrashLoopPatternNoMatch(t *testing.T) {
	p := &CrashLoopPattern{}
	metrics := []MetricResult{
		{
			Name: "restart_rate",
			Values: []MetricPoint{
				{Value: 0.001, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1"}},
				{Value: 0.001, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1"}},
			},
		},
	}
	matches := p.Match(metrics, nil, ResourceSnapshot{})
	if len(matches) != 0 {
		t.Fatal("expected no CrashLoop match with very low restart rate")
	}
}

func TestCrashLoopPatternAlreadyCrashing(t *testing.T) {
	p := &CrashLoopPattern{}
	metrics := []MetricResult{
		{
			Name: "restart_rate",
			Values: []MetricPoint{
				{Value: 0.8, Timestamp: time.Now(), Labels: map[string]string{"pod": "crash-1", "namespace": "default"}},
				{Value: 1.2, Timestamp: time.Now(), Labels: map[string]string{"pod": "crash-1", "namespace": "default"}},
			},
		},
	}
	matches := p.Match(metrics, nil, ResourceSnapshot{})
	if len(matches) == 0 {
		t.Fatal("expected CrashLoop match when already at critical restart rate")
	}
	if matches[0].TimeToFailure != 0 {
		t.Fatalf("expected TimeToFailure=0 when already crashing, got %v", matches[0].TimeToFailure)
	}
}

func TestDegradationPatternPodNotReady(t *testing.T) {
	p := &DegradationPattern{}
	metrics := []MetricResult{
		{
			Name: "pod_not_ready",
			Values: []MetricPoint{
				{Value: 2, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
			},
		},
	}
	matches := p.Match(metrics, nil, ResourceSnapshot{})
	if len(matches) == 0 {
		t.Fatal("expected Degradation match for pod_not_ready")
	}
}

func TestDegradationPatternDiskPressure(t *testing.T) {
	p := &DegradationPattern{}
	metrics := []MetricResult{
		{
			Name: "node_disk_pressure",
			Values: []MetricPoint{
				{Value: 1, Timestamp: time.Now(), Labels: map[string]string{"node": "node-1"}},
			},
		},
	}
	matches := p.Match(metrics, nil, ResourceSnapshot{})
	if len(matches) == 0 {
		t.Fatal("expected Degradation match for disk pressure")
	}
}

func TestDegradationPatternFailingPods(t *testing.T) {
	p := &DegradationPattern{}
	resources := ResourceSnapshot{FailingPods: []string{"default/broken-pod"}}
	matches := p.Match(nil, nil, resources)
	if len(matches) == 0 {
		t.Fatal("expected Degradation match for failing pod")
	}
}

func TestTimeToFailureEstimates(t *testing.T) {
	limit := float64(testMemLimit)
	pts := []MetricPoint{
		{Value: memBytes(0.6)},
		{Value: memBytes(0.75)},
		{Value: memBytes(0.9)},
	}
	ratioPts := ratioSeries(pts, limit)
	eta := estimateOOMTimeToFailure(ratioPts, 0.9)
	if eta <= 0 || eta > time.Minute {
		t.Fatalf("unexpected OOM ETA: %v", eta)
	}

	restartPts := []MetricPoint{
		{Value: 0.05},
		{Value: 0.15},
		{Value: 0.40},
	}
	eta = estimateCrashLoopTimeToFailure(restartPts, 0.40)
	if eta <= 0 || eta > 2*time.Hour {
		t.Fatalf("unexpected crash-loop ETA: %v", eta)
	}
}

func TestPredictorRunPatterns(t *testing.T) {
	cfg := DefaultScorerConfig()
	// Unit test expects immediate pattern emission; disable debounce here.
	cfg.PatternPersistence = 1
	cfg.PatternCooldownScrapes = 0
	pred := NewPredictor(cfg)
	pred.AddPattern(&OOMPattern{})
	pred.AddPattern(&CrashLoopPattern{})
	pred.AddPattern(&DegradationPattern{})

	resources := ResourceSnapshot{
		PodResources: []PodResource{{Name: "web-1", Namespace: "default", MemLimit: testMemLimit}},
		FailingPods:  []string{"broken-pod"},
	}
	now := time.Now()
	metrics := []MetricResult{
		{
			Name: "pod_memory_usage",
			Values: []MetricPoint{
				{Value: memBytes(0.6), Timestamp: now, Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
				{Value: memBytes(0.75), Timestamp: now.Add(30 * time.Second), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
				{Value: memBytes(0.9), Timestamp: now.Add(60 * time.Second), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
			},
		},
		{
			Name: "restart_rate",
			Values: []MetricPoint{
				{Value: 0.05, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1", "namespace": "prod"}},
				{Value: 0.15, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1", "namespace": "prod"}},
				{Value: 0.40, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1", "namespace": "prod"}},
			},
		},
	}

	results := pred.RunPatterns(metrics, nil, resources)
	if len(results) == 0 {
		t.Fatal("expected pattern results")
	}

	foundOOM := false
	foundCrashLoop := false
	for _, r := range results {
		if r.Entity == "web-1" && r.MetricName == "OOMRisk" {
			foundOOM = true
		}
		if r.Entity == "app-1" && r.MetricName == "CrashLoopRisk" {
			foundCrashLoop = true
		}
	}
	if !foundOOM {
		t.Fatal("expected OOM pattern for web-1")
	}
	if !foundCrashLoop {
		t.Fatal("expected CrashLoop pattern for app-1")
	}
}

func TestPatternHelpers(t *testing.T) {
	metrics := []MetricResult{
		{Name: "cpu", Values: []MetricPoint{{Value: 1}}},
		{Name: "mem", Values: []MetricPoint{{Value: 2}}},
	}
	m, ok := findMetric(metrics, "mem")
	if !ok || m.Name != "mem" {
		t.Fatal("findMetric failed")
	}

	pts := []MetricPoint{
		{Value: 10, Labels: map[string]string{"pod": "a", "namespace": "ns1"}},
		{Value: 20, Labels: map[string]string{"pod": "a", "namespace": "ns1"}},
		{Value: 30, Labels: map[string]string{"pod": "b", "namespace": "ns2"}},
	}
	grouped := groupByEntity(pts)
	if len(grouped) != 2 {
		t.Fatalf("expected 2 groups, got %d", len(grouped))
	}
	if grouped["ns1/a"] == nil {
		t.Fatal("expected ns1/a group")
	}

	if !contains([]string{"a", "b", "c"}, "b") {
		t.Fatal("contains failed to find element")
	}
}

func TestPreparePatternMetrics(t *testing.T) {
	pred := NewPredictor(DefaultScorerConfig())
	now := time.Now()

	for i := 0; i < 3; i++ {
		pred.PreparePatternMetrics([]MetricResult{{
			Name: "pod_memory_usage",
			Values: []MetricPoint{{
				Value:     float64(100 + i*50),
				Timestamp: now.Add(time.Duration(i) * 30 * time.Second),
				Labels:    map[string]string{"pod": "web-1", "namespace": "default"},
			}},
		}})
	}

	enriched := pred.PreparePatternMetrics([]MetricResult{{
		Name: "pod_memory_usage",
		Values: []MetricPoint{{
			Value:     250,
			Timestamp: now.Add(3 * 30 * time.Second),
			Labels:    map[string]string{"pod": "web-1", "namespace": "default"},
		}},
	}})

	mem, ok := findMetric(enriched, "pod_memory_usage")
	if !ok || len(mem.Values) < 4 {
		t.Fatalf("expected >=4 history points, got %d", len(mem.Values))
	}
}
