package gate

import (
	"testing"
	"time"
)

func TestGate_SustainedScore(t *testing.T) {
	g := NewGate(DefaultConfig())
	g.config.SustainCount = 3
	g.config.ScrapeInterval = 30 * time.Second
	g.config.Threshold = 0.3
	g.config.CooldownDuration = 5 * time.Minute
	now := time.Now()

	// Scrape 1: score 0.3 — not enough data yet
	r := g.Filter("pod-1", "cpu", 0.3, "statistical", now)
	if r.Pass {
		t.Fatal("expected no pass on first scrape (need 3)")
	}

	// Scrape 2: score 0.3 — still not enough
	r = g.Filter("pod-1", "cpu", 0.3, "statistical", now.Add(30*time.Second))
	if r.Pass {
		t.Fatal("expected no pass on second scrape (need 3)")
	}

	// Scrape 3: score 0.3 — should pass now
	r = g.Filter("pod-1", "cpu", 0.3, "statistical", now.Add(60*time.Second))
	if !r.Pass {
		t.Fatalf("expected pass on third scrape, got reason=%s", r.Reason)
	}
	if r.Reason != "sustainment" {
		t.Fatalf("expected reason sustainment, got %s", r.Reason)
	}
}

func TestGate_SingleSpikeDropped(t *testing.T) {
	g := NewGate(DefaultConfig())
	g.config.SustainCount = 3
	g.config.Threshold = 0.3
	now := time.Now()

	// Single spike at 0.5 is below high-score bypass, so need sustainment.
	r := g.Filter("pod-1", "cpu", 0.5, "statistical", now)
	if r.Pass {
		t.Fatal("expected drop on single spike (need 3 consecutive)")
	}

	// Second scrape below threshold.
	r = g.Filter("pod-1", "cpu", 0.1, "statistical", now.Add(30*time.Second))
	if r.Pass {
		t.Fatal("expected drop when score drops below threshold")
	}
}

func TestGate_HighScoreBypass(t *testing.T) {
	g := NewGate(DefaultConfig())
	g.config.HighScoreBypass = 0.8
	g.config.ScrapeInterval = 30 * time.Second
	g.config.CooldownDuration = 5 * time.Minute
	now := time.Now()

	// Score 0.9 should pass immediately on first scrape.
	r := g.Filter("pod-1", "cpu", 0.9, "statistical", now)
	if !r.Pass {
		t.Fatalf("expected pass on high score bypass, got reason=%s", r.Reason)
	}
	if r.Reason != "high_score_bypass" {
		t.Fatalf("expected reason high_score_bypass, got %s", r.Reason)
	}
}

func TestGate_PatternBypass(t *testing.T) {
	g := NewGate(DefaultConfig())
	g.config.BypassPatterns = true
	now := time.Now()

	// Pattern type should pass even at moderate confidence.
	r := g.Filter("pod-1", "memory", 0.6, "pattern", now)
	if !r.Pass {
		t.Fatalf("expected pass on pattern bypass, got reason=%s", r.Reason)
	}
	if r.Reason != "pattern_bypass" {
		t.Fatalf("expected reason pattern_bypass, got %s", r.Reason)
	}
}

func TestGate_Cooldown(t *testing.T) {
	g := NewGate(DefaultConfig())
	g.config.CooldownDuration = 5 * time.Minute
	g.config.HighScoreBypass = 0.3 // low bypass for test convenience
	now := time.Now()

	// First pass: goes through.
	r := g.Filter("pod-1", "cpu", 0.5, "statistical", now)
	if !r.Pass {
		t.Fatalf("expected first pass, got reason=%s", r.Reason)
	}

	// Second attempt within cooldown: should drop.
	r = g.Filter("pod-1", "cpu", 0.5, "statistical", now.Add(30*time.Second))
	if r.Pass {
		t.Fatal("expected drop during cooldown")
	}
	if r.Reason != "cooldown_active" {
		t.Fatalf("expected reason cooldown_active, got %s", r.Reason)
	}
}

func TestGate_CooldownEscalation(t *testing.T) {
	g := NewGate(DefaultConfig())
	g.config.CooldownDuration = 5 * time.Minute
	g.config.HighScoreBypass = 0.3
	now := time.Now()

	// First pass at 0.5.
	r := g.Filter("pod-1", "cpu", 0.5, "statistical", now)
	if !r.Pass {
		t.Fatalf("expected first pass, got reason=%s", r.Reason)
	}

	// Escalated to 0.7 within cooldown — should pass (high_score_bypass rule triggers).
	r = g.Filter("pod-1", "cpu", 0.7, "statistical", now.Add(30*time.Second))
	if !r.Pass {
		t.Fatalf("expected pass on escalation override, got reason=%s", r.Reason)
	}

	// Same score again within cooldown — should drop (not escalated, cooldown active).
	r = g.Filter("pod-1", "cpu", 0.7, "statistical", now.Add(60*time.Second))
	if r.Pass {
		t.Fatal("expected drop on repeat non-escalated score during cooldown")
	}
}

func TestGate_MultiMetricCorrelation(t *testing.T) {
	g := NewGate(DefaultConfig())
	g.config.CorrelationWindow = 60 * time.Second
	g.config.ScrapeInterval = 30 * time.Second
	now := time.Now()

	// First metric at 0.4 — not enough to sustain or correlate.
	r := g.Filter("pod-1", "cpu", 0.4, "statistical", now)
	if r.Pass {
		t.Fatal("expected drop on single metric, no sustainment yet")
	}

	// Second metric at 0.5 within correlation window — should pass via correlation.
	r = g.Filter("pod-1", "memory", 0.5, "statistical", now.Add(10*time.Second))
	if !r.Pass {
		t.Fatalf("expected pass on multi-metric correlation, got reason=%s", r.Reason)
	}
	if r.Reason != "multi_metric_correlation" {
		t.Fatalf("expected reason multi_metric_correlation, got %s", r.Reason)
	}
}

func TestGate_MultiMetricDifferentEntities(t *testing.T) {
	g := NewGate(DefaultConfig())
	g.config.CorrelationWindow = 60 * time.Second
	g.config.SustainCount = 3
	g.config.Threshold = 0.3
	now := time.Now()

	// Different entities with different metrics should NOT correlate.
	r := g.Filter("pod-1", "cpu", 0.5, "statistical", now)
	if r.Pass {
		t.Fatal("expected drop for pod-1 cpu (no sustainment)")
	}

	r = g.Filter("pod-2", "memory", 0.5, "statistical", now.Add(10*time.Second))
	if r.Pass {
		t.Fatal("expected drop for pod-2 memory (different entity, no sustainment)")
	}
}

func TestGate_NoRuleTriggered(t *testing.T) {
	g := NewGate(DefaultConfig())
	g.config.Threshold = 0.3
	g.config.HighScoreBypass = 0.8
	now := time.Now()

	// Score below threshold, not a pattern, single metric — should drop.
	r := g.Filter("pod-1", "cpu", 0.1, "statistical", now)
	if r.Pass {
		t.Fatal("expected drop for low score")
	}
	if r.Reason != "no_rule_triggered" {
		t.Fatalf("expected reason no_rule_triggered, got %s", r.Reason)
	}
}

func TestGate_ShouldPersist(t *testing.T) {
	g := NewGate(DefaultConfig())
	now := time.Now()

	pass, reason := g.ShouldPersist("pod-1", "cpu", 0.1, "statistical", now)
	if pass {
		t.Fatal("expected drop for low score")
	}
	if reason != "no_rule_triggered" {
		t.Fatalf("expected no_rule_triggered, got %s", reason)
	}

	pass, reason = g.ShouldPersist("pod-1", "OOMRisk", 0.7, "pattern", now)
	if !pass {
		t.Fatalf("expected pattern pass, got %s", reason)
	}
}

func TestGate_ObserveScoreThenFilter(t *testing.T) {
	g := NewGate(DefaultConfig())
	g.config.SustainCount = 3
	g.config.Threshold = 0.3
	g.config.CooldownDuration = 5 * time.Minute
	now := time.Now()

	// Pre-record via ObserveScore — this is the normal agent loop flow.
	for i := 0; i < 3; i++ {
		g.ObserveScore("pod-1", "cpu", 0.5, now.Add(time.Duration(i)*30*time.Second))
	}

	// Filter should now find the pre-recorded sustainment.
	r := g.Filter("pod-1", "cpu", 0.5, "statistical", now.Add(90*time.Second))
	if !r.Pass {
		t.Fatalf("expected pass via sustainment, got reason=%s", r.Reason)
	}
	if r.Reason != "sustainment" {
		t.Fatalf("expected reason sustainment, got %s", r.Reason)
	}
}

func TestGate_SustainResetsOnSubthreshold(t *testing.T) {
	g := NewGate(DefaultConfig())
	g.config.SustainCount = 3
	g.config.Threshold = 0.3
	now := time.Now()

	// Two good scrapes, then a bad one resets the counter.
	g.Filter("pod-1", "cpu", 0.5, "statistical", now)          // scrape 1: recorded
	g.Filter("pod-1", "cpu", 0.5, "statistical", now.Add(30*time.Second)) // scrape 2: recorded

	// Sub-threshold score — resets history.
	r := g.Filter("pod-1", "cpu", 0.1, "statistical", now.Add(60*time.Second))
	if r.Pass {
		t.Fatal("expected drop after sub-threshold reset")
	}

	// Need 3 more consecutive good scrapes.
	for i := 0; i < 2; i++ {
		r = g.Filter("pod-1", "cpu", 0.5, "statistical", now.Add(time.Duration(90+i*30)*time.Second))
		if r.Pass {
			t.Fatalf("expected drop at scrape %d (need 3)", i+1)
		}
	}
	r = g.Filter("pod-1", "cpu", 0.5, "statistical", now.Add(150*time.Second))
	if !r.Pass {
		t.Fatalf("expected pass at scrape 3, got reason=%s", r.Reason)
	}
}

func TestGate_MultiMetricAcceleratesSustain(t *testing.T) {
	g := NewGate(DefaultConfig())
	g.config.SustainCount = 3
	g.config.Threshold = 0.3
	g.config.CorrelationWindow = 60 * time.Second
	g.config.ScrapeInterval = 30 * time.Second
	now := time.Now()

	// First metric at scrape 1: recorded but insufficient.
	r := g.Filter("pod-1", "cpu", 0.4, "statistical", now)
	if r.Pass {
		t.Fatal("expected drop on single metric scrape 1")
	}

	// First metric at scrape 2: still only 2/3 sustain.
	r = g.Filter("pod-1", "cpu", 0.4, "statistical", now.Add(30*time.Second))
	if r.Pass {
		t.Fatal("expected drop on single metric scrape 2")
	}

	// Second metric for same entity within CorrelationWindow — correlation passes.
	r = g.Filter("pod-1", "memory", 0.5, "statistical", now.Add(40*time.Second))
	if !r.Pass {
		t.Fatalf("expected pass via multi-metric, got reason=%s", r.Reason)
	}
	if r.Reason != "multi_metric_correlation" {
		t.Fatalf("expected reason multi_metric_correlation, got %s", r.Reason)
	}
}

func TestGate_DefaultConfig(t *testing.T) {
	g := NewGate(DefaultConfig())

	if g.config.Threshold != 0.3 {
		t.Fatalf("expected Threshold 0.3, got %f", g.config.Threshold)
	}
	if g.config.SustainCount != 3 {
		t.Fatalf("expected SustainCount 3, got %d", g.config.SustainCount)
	}
	if g.config.CooldownDuration != 5*time.Minute {
		t.Fatalf("expected CooldownDuration 5m, got %v", g.config.CooldownDuration)
	}
	if g.config.ScrapeInterval != 30*time.Second {
		t.Fatalf("expected ScrapeInterval 30s, got %v", g.config.ScrapeInterval)
	}
	if g.config.CorrelationWindow != 60*time.Second {
		t.Fatalf("expected CorrelationWindow 60s, got %v", g.config.CorrelationWindow)
	}
	if g.config.HighScoreBypass != 0.8 {
		t.Fatalf("expected HighScoreBypass 0.8, got %f", g.config.HighScoreBypass)
	}
	if !g.config.BypassPatterns {
		t.Fatal("expected BypassPatterns true")
	}
}
