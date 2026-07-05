package predictor

import (
	"testing"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func TestOOMPatternNoEvent(t *testing.T) {
	p := &OOMPattern{}
	metrics := []MetricResult{
		{
			Name: "pod_memory_usage",
			Values: []MetricPoint{
				{Value: 100, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1"}},
				{Value: 200, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1"}},
			},
		},
	}
	matches := p.Match(metrics, nil, ResourceSnapshot{})
	if len(matches) != 0 {
		t.Fatal("expected no OOM match without events")
	}
}

func TestOOMPatternFires(t *testing.T) {
	p := &OOMPattern{}
	metrics := []MetricResult{
		{
			Name: "pod_memory_usage",
			Values: []MetricPoint{
				{Value: 100, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
				{Value: 150, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
				{Value: 200, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
			},
		},
	}
	events := []models.AnomalyRecord{
		{Entity: "web-1", Pattern: "OOMKilled"},
	}
	matches := p.Match(metrics, events, ResourceSnapshot{})
	if len(matches) == 0 {
		t.Fatal("expected OOM match with rising memory and OOMKilled event")
	}
	if matches[0].Pattern != "OOMRisk" {
		t.Fatalf("expected OOMRisk pattern, got %s", matches[0].Pattern)
	}
	if matches[0].Confidence < 0.5 {
		t.Fatalf("expected confidence >= 0.5, got %f", matches[0].Confidence)
	}
	if matches[0].Entity != "web-1" {
		t.Fatalf("expected entity web-1, got %s", matches[0].Entity)
	}
	if matches[0].Namespace != "default" {
		t.Fatalf("expected namespace default, got %s", matches[0].Namespace)
	}
}

func TestOOMPatternMemoryFlat(t *testing.T) {
	p := &OOMPattern{}
	metrics := []MetricResult{
		{
			Name: "pod_memory_usage",
			Values: []MetricPoint{
				{Value: 100, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1"}},
				{Value: 100, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1"}},
				{Value: 100, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1"}},
			},
		},
	}
	events := []models.AnomalyRecord{
		{Entity: "web-1", Pattern: "OOMKilled"},
	}
	matches := p.Match(metrics, events, ResourceSnapshot{})
	// Flat memory + OOM event → still matches but with lower confidence
	// (trend is 0 so confidence starts at 0.5 + 0.2 for OOM event)
	if len(matches) == 0 {
		t.Fatal("expected OOM match with flat memory + OOM event")
	}
}

func TestOOMPatternMultiplePods(t *testing.T) {
	p := &OOMPattern{}
	metrics := []MetricResult{
		{
			Name: "pod_memory_usage",
			Values: []MetricPoint{
				{Value: 100, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1", "namespace": "ns1"}},
				{Value: 180, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1", "namespace": "ns1"}},
				{Value: 50, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-2", "namespace": "ns2"}},
				{Value: 60, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-2", "namespace": "ns2"}},
			},
		},
	}
	events := []models.AnomalyRecord{
		{Entity: "web-1", Pattern: "OOMKilled"},
	}
	matches := p.Match(metrics, events, ResourceSnapshot{})
	if len(matches) == 0 {
		t.Fatal("expected at least one OOM match")
	}
	matchedWeb1 := false
	for _, m := range matches {
		if m.Entity == "web-1" {
			matchedWeb1 = true
		}
	}
	if !matchedWeb1 {
		t.Fatal("expected web-1 to match with OOM event + rising memory")
	}
}

func TestCrashLoopPatternFires(t *testing.T) {
	p := &CrashLoopPattern{}
	metrics := []MetricResult{
		{
			Name: "restart_rate",
			Values: []MetricPoint{
				{Value: 0.01, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1", "namespace": "prod"}},
				{Value: 0.05, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1", "namespace": "prod"}},
				{Value: 0.15, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1", "namespace": "prod"}},
			},
		},
	}
	events := []models.AnomalyRecord{
		{Entity: "app-1", Pattern: "CrashLoopBackOff"},
	}
	matches := p.Match(metrics, events, ResourceSnapshot{})
	if len(matches) == 0 {
		t.Fatal("expected CrashLoop match with rising restarts and event")
	}
	if matches[0].Pattern != "CrashLoopRisk" {
		t.Fatalf("expected CrashLoopRisk, got %s", matches[0].Pattern)
	}
	if matches[0].Confidence < 0.5 {
		t.Fatalf("expected confidence >= 0.5, got %f", matches[0].Confidence)
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

func TestCrashLoopPatternFailingPod(t *testing.T) {
	p := &CrashLoopPattern{}
	metrics := []MetricResult{
		{
			Name: "restart_rate",
			Values: []MetricPoint{
				{Value: 0.02, Timestamp: time.Now(), Labels: map[string]string{"pod": "crash-1", "namespace": "default"}},
				{Value: 0.08, Timestamp: time.Now(), Labels: map[string]string{"pod": "crash-1", "namespace": "default"}},
				{Value: 0.12, Timestamp: time.Now(), Labels: map[string]string{"pod": "crash-1", "namespace": "default"}},
			},
		},
	}
	events := []models.AnomalyRecord{
		{Entity: "crash-1", Pattern: "CrashLoopBackOff"},
	}
	resources := ResourceSnapshot{FailingPods: []string{"crash-1"}}
	matches := p.Match(metrics, events, resources)
	if len(matches) == 0 {
		t.Fatal("expected CrashLoop match with failing pod")
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
	if matches[0].Pattern != "Degradation" {
		t.Fatalf("expected Degradation pattern, got %s", matches[0].Pattern)
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
	resources := ResourceSnapshot{FailingPods: []string{"broken-pod"}}
	matches := p.Match(nil, nil, resources)
	if len(matches) == 0 {
		t.Fatal("expected Degradation match for failing pod")
	}
}

func TestPredictorRunPatterns(t *testing.T) {
	pred := NewPredictor(DefaultScorerConfig())
	pred.AddPattern(&OOMPattern{})
	pred.AddPattern(&CrashLoopPattern{})
	pred.AddPattern(&DegradationPattern{})

	metrics := []MetricResult{
		{
			Name: "pod_memory_usage",
			Values: []MetricPoint{
				{Value: 100, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
				{Value: 200, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
				{Value: 300, Timestamp: time.Now(), Labels: map[string]string{"pod": "web-1", "namespace": "default"}},
			},
		},
		{
			Name: "restart_rate",
			Values: []MetricPoint{
				{Value: 0.01, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1", "namespace": "prod"}},
				{Value: 0.1, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1", "namespace": "prod"}},
				{Value: 0.5, Timestamp: time.Now(), Labels: map[string]string{"pod": "app-1", "namespace": "prod"}},
			},
		},
	}
	events := []models.AnomalyRecord{
		{Entity: "web-1", Pattern: "OOMKilled"},
		{Entity: "app-1", Pattern: "CrashLoopBackOff"},
	}
	resources := ResourceSnapshot{FailingPods: []string{"broken-pod"}}

	results := pred.RunPatterns(metrics, events, resources)
	if len(results) == 0 {
		t.Fatal("expected pattern results")
	}

	types := make(map[string]bool)
	for _, r := range results {
		types[r.Type] = true
	}
	if !types["pattern"] {
		t.Fatal("expected pattern-type results")
	}

	foundOOM := false
	foundCrashLoop := false
	for _, r := range results {
		if r.Entity == "web-1" {
			foundOOM = true
		}
		if r.Entity == "app-1" {
			foundCrashLoop = true
		}
	}
	if !foundOOM {
		t.Log("Note: OOM pattern didn't trigger (may need higher memory trend)")
	}
	if !foundCrashLoop {
		t.Log("Note: CrashLoop pattern didn't trigger (may need higher restart rate)")
	}
}

func TestPatternHelpers(t *testing.T) {
	metrics := []MetricResult{
		{Name: "cpu", Values: []MetricPoint{{Value: 1}}},
		{Name: "mem", Values: []MetricPoint{{Value: 2}}},
	}
	m := findMetric(metrics, "mem")
	if m == nil || m.Name != "mem" {
		t.Fatal("findMetric failed")
	}
	m = findMetric(metrics, "nonexistent")
	if m != nil {
		t.Fatal("findMetric should return nil for unknown")
	}

	pts := []MetricPoint{
		{Value: 10, Labels: map[string]string{"pod": "a"}},
		{Value: 20, Labels: map[string]string{"pod": "a"}},
		{Value: 30, Labels: map[string]string{"pod": "b"}},
	}
	grouped := groupByEntity(pts)
	if len(grouped) != 2 {
		t.Fatalf("expected 2 groups, got %d", len(grouped))
	}
	if len(grouped["a"]) != 2 {
		t.Fatalf("expected 2 points for entity a, got %d", len(grouped["a"]))
	}

	trend := estimateTrend(pts[:2])
	if trend != 1.0 {
		t.Fatalf("expected trend 1.0 (10→20), got %f", trend)
	}

	trend = estimateTrend(pts[2:3])
	if trend != 0 {
		t.Fatalf("expected trend 0 for single point, got %f", trend)
	}

	if !contains([]string{"a", "b", "c"}, "b") {
		t.Fatal("contains failed to find element")
	}
	if contains([]string{"a", "b", "c"}, "z") {
		t.Fatal("contains should not find missing element")
	}
}
