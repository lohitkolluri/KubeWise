package promptctx

import (
	"context"
	"testing"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// fakeStore implements DataSource for testing.
type fakeStore struct {
	samples []Sample
}

func (f *fakeStore) GetMetricSamples(_ string, _ map[string]string, _ int) ([]Sample, error) {
	return f.samples, nil
}

func TestBuilder_Build(t *testing.T) {
	t.Parallel()

	now := time.Now()
	s := &fakeStore{}

	b := New(s)
	anomaly := models.AnomalyRecord{
		Entity:     "default/nginx-pod-xyz",
		Namespace:  "default",
		MetricName: "",
		Score:      0.85,
		Pattern:    "CrashLoopBackOff",
	}

	cc := b.Build(context.Background(), anomaly)

	if cc.Anomaly.Entity != "default/nginx-pod-xyz" {
		t.Errorf("Entity = %q, want %q", cc.Anomaly.Entity, "default/nginx-pod-xyz")
	}
	if cc.Anomaly.Score != 0.85 {
		t.Errorf("Score = %f, want %f", cc.Anomaly.Score, 0.85)
	}
	if cc.Anomaly.Pattern != "CrashLoopBackOff" {
		t.Errorf("Pattern = %q, want %q", cc.Anomaly.Pattern, "CrashLoopBackOff")
	}
	if cc.BuiltAt.Before(now) {
		t.Error("BuiltAt is before now")
	}
	if cc.Metrics != nil {
		t.Error("Metrics should be nil when MetricName is empty")
	}
}

func TestBuilder_BuildWithMetric(t *testing.T) {
	t.Parallel()

	s := &fakeStore{
		samples: []Sample{
			{Value: 10, TS: 1000},
			{Value: 20, TS: 1001},
			{Value: 30, TS: 1002},
		},
	}

	b := New(s)
	anomaly := models.AnomalyRecord{
		Entity:     "default/nginx-pod-xyz",
		Namespace:  "default",
		MetricName: "cpu_usage",
		Score:      0.92,
		Pattern:    "Spike",
	}

	cc := b.Build(context.Background(), anomaly)

	if cc.Anomaly.Metric != "cpu_usage" {
		t.Errorf("Metric = %q, want %q", cc.Anomaly.Metric, "cpu_usage")
	}
	if len(cc.Metrics) == 0 {
		t.Fatal("expected at least one metric summary")
	}
	if cc.Metrics[0].Current != 30 {
		t.Errorf("Current = %f, want %f", cc.Metrics[0].Current, 30.0)
	}
	if cc.Metrics[0].Max != 30 {
		t.Errorf("Max = %f, want %f", cc.Metrics[0].Max, 30.0)
	}
}

func TestBuilder_BuildWithMetricNoData(t *testing.T) {
	t.Parallel()

	s := &fakeStore{} // no samples

	b := New(s)
	anomaly := models.AnomalyRecord{
		Entity:     "default/nginx-pod-xyz",
		Namespace:  "default",
		MetricName: "cpu_usage",
		Score:      0.5,
		Pattern:    "Spike",
	}

	cc := b.Build(context.Background(), anomaly)
	if len(cc.Metrics) != 1 {
		t.Fatalf("expected 1 fallback metric summary, got %d", len(cc.Metrics))
	}
	if cc.Metrics[0].Current != 0.5 {
		t.Errorf("fallback Current = %f, want %f", cc.Metrics[0].Current, 0.5)
	}
	if cc.Metrics[0].Trend != "unknown" {
		t.Errorf("fallback Trend = %q, want 'unknown'", cc.Metrics[0].Trend)
	}
}

func TestBuildMetricSummaryTrends(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name string
		pts  []Sample
		want string
	}{
		{
			name: "rising",
			pts: []Sample{
				{Value: 10, TS: 1000},
				{Value: 20, TS: 1001},
				{Value: 30, TS: 1002},
			},
			want: "rising",
		},
		{
			name: "falling",
			pts: []Sample{
				{Value: 30, TS: 1000},
				{Value: 20, TS: 1001},
				{Value: 10, TS: 1002},
			},
			want: "falling",
		},
		{
			name: "stable",
			pts: []Sample{
				{Value: 20, TS: 1000},
				{Value: 21, TS: 1001},
				{Value: 20, TS: 1002},
			},
			want: "stable",
		},
		{
			name: "single_point",
			pts:  []Sample{{Value: 42, TS: 1000}},
			want: "stable",
		},
		{
			name: "empty",
			pts:  nil,
			want: "unknown",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			s := &fakeStore{samples: tt.pts}
			ms := BuildMetricSummary(s, "test_metric", "default/pod", "default", 0.5)
			if tt.pts == nil {
				if ms[0].Trend != "unknown" {
					t.Errorf("Trend = %q, want 'unknown'", ms[0].Trend)
				}
				return
			}
			if ms[0].Trend != tt.want {
				t.Errorf("Trend = %q, want %q", ms[0].Trend, tt.want)
			}
		})
	}
}

func TestSummarizeEvents(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 7, 8, 12, 0, 0, 0, time.UTC)

	events := []K8sEvent{
		{Reason: "BackOff", Involved: "default/nginx", LastTimestamp: now.Add(-2 * time.Minute)},
		{Reason: "BackOff", Involved: "default/nginx", LastTimestamp: now.Add(-1 * time.Minute)},
		{Reason: "Pulled", Involved: "default/nginx", LastTimestamp: now.Add(-5 * time.Minute)},
		{Reason: "OOMKilling", Involved: "kube-system/metrics-server", LastTimestamp: now.Add(-30 * time.Minute)},
	}

	summaries := SummarizeEvents(events, now)

	if len(summaries) != 3 {
		t.Fatalf("expected 3 deduped summaries, got %d", len(summaries))
	}

	// BackOff should be first (highest count)
	if summaries[0].Reason != "BackOff" {
		t.Errorf("first summary Reason = %q, want 'BackOff'", summaries[0].Reason)
	}
	if summaries[0].Count != 2 {
		t.Errorf("BackOff Count = %d, want 2", summaries[0].Count)
	}
	if summaries[0].LastSeen != "1m ago" {
		t.Errorf("BackOff LastSeen = %q, want '1m ago'", summaries[0].LastSeen)
	}

	// OOMKilling should have the oldest last_seen.
	for _, s := range summaries {
		if s.Reason == "OOMKilling" {
			if s.LastSeen != "30m ago" {
				t.Errorf("OOMKilling LastSeen = %q, want '30m ago'", s.LastSeen)
			}
		}
	}
}

func TestSummarizeEventsEmpty(t *testing.T) {
	t.Parallel()
	if s := SummarizeEvents(nil, time.Now()); s != nil {
		t.Errorf("expected nil for no events, got %v", s)
	}
}

func TestHashK8sState(t *testing.T) {
	t.Parallel()

	state := &K8sState{
		Deployment: &DeploymentState{
			Name:      "nginx",
			Replicas:  3,
			Available: 3,
			Strategy:  "RollingUpdate",
			Image:     "nginx:1.25",
		},
	}

	h1 := HashK8sState(state)

	// Same state should produce same hash.
	h2 := HashK8sState(state)
	if h1 != h2 {
		t.Error("hash changed for identical state")
	}

	// Different state should produce different hash.
	state.Deployment.Available = 2
	h3 := HashK8sState(state)
	if h1 == h3 {
		t.Error("hash should differ when state changes")
	}
}

func TestHashK8sStateNil(t *testing.T) {
	t.Parallel()
	if h := HashK8sState(nil); h != "" {
		t.Errorf("expected empty hash for nil, got %q", h)
	}
}

func TestTokenEstimate(t *testing.T) {
	t.Parallel()

	cc := CompactContext{
		Anomaly: AnomalyContext{
			Entity:    "default/nginx",
			Namespace: "default",
			Metric:    "cpu_usage",
			Score:     0.85,
			Pattern:   "Spike",
		},
		Metrics: []MetricSummary{
			{Name: "cpu", Current: 92, Trend: "rising", Max: 95, SampleCount: 30},
		},
		BuiltAt: time.Now(),
	}

	tokens := cc.TokenEstimate()
	// Rough check: token count should be positive.
	if tokens <= 0 {
		t.Errorf("TokenEstimate() = %d, want > 0", tokens)
	}
}

func TestRelativeTime(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 7, 8, 12, 0, 0, 0, time.UTC)

	tests := []struct {
		t    time.Time
		want string
	}{
		{now.Add(-30 * time.Second), "30s ago"},
		{now.Add(-2 * time.Minute), "2m ago"},
		{now.Add(-3 * time.Hour), "3h ago"},
		{now.Add(-48 * time.Hour), "2d ago"},
		{time.Time{}, ""},
	}

	for _, tt := range tests {
		got := relativeTime(tt.t, now)
		if got != tt.want {
			t.Errorf("relativeTime(%v) = %q, want %q", tt.t, got, tt.want)
		}
	}
}

func TestShortEventMessage(t *testing.T) {
	t.Parallel()

	msg := "this is a very long message that should be truncated"
	short := shortEventMessage(msg, 20)
	if len(short) > 24 {
		t.Errorf("shortEventMessage too long: %q", short)
	}
	if short != "this is a very long ..." {
		t.Errorf("got %q, want %q", short, "this is a very long ...")
	}

	// No truncation needed.
	if shortEventMessage("short", 100) != "short" {
		t.Error("short message should not be truncated")
	}

	// Newlines replaced with spaces.
	if shortEventMessage("line1\nline2", 100) != "line1 line2" {
		t.Error("newlines should be replaced")
	}
}
