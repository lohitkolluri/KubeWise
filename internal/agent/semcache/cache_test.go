package semcache

import (
	"fmt"
	"sync"
	"testing"
	"time"
)

func TestNew(t *testing.T) {
	t.Parallel()

	c := New(Config{})
	if c == nil {
		t.Fatal("New returned nil")
	}
	if c.cfg.TTL != DefaultTTL {
		t.Errorf("TTL = %v, want %v", c.cfg.TTL, DefaultTTL)
	}
	if c.cfg.MaxEntries != DefaultMaxEntries {
		t.Errorf("MaxEntries = %d, want %d", c.cfg.MaxEntries, DefaultMaxEntries)
	}
	if c.Len() != 0 {
		t.Errorf("Len = %d, want 0", c.Len())
	}
}

func TestNewCustom(t *testing.T) {
	t.Parallel()

	c := New(Config{TTL: 5 * time.Minute, MaxEntries: 50})
	if c.cfg.TTL != 5*time.Minute {
		t.Errorf("TTL = %v, want 5m", c.cfg.TTL)
	}
	if c.cfg.MaxEntries != 50 {
		t.Errorf("MaxEntries = %d, want 50", c.cfg.MaxEntries)
	}
}

func TestSetGet(t *testing.T) {
	t.Parallel()

	c := New(Config{TTL: 10 * time.Minute, MaxEntries: 100})

	fp := "test-fingerprint-123"
	plan := `{"action":"restart_pod","target":"nginx"}`
	c.Set(fp, plan)

	got := c.Get(fp)
	if got == nil {
		t.Fatal("expected cached entry, got nil")
	}
	if got.PlanJSON != plan {
		t.Errorf("PlanJSON = %q, want %q", got.PlanJSON, plan)
	}
	if got.Fingerprint != fp {
		t.Errorf("Fingerprint = %q, want %q", got.Fingerprint, fp)
	}
	// HitCount tracks number of Gets. First Get after Set increments from 0 to 1.
	if got.HitCount != 1 {
		t.Errorf("HitCount = %d, want 1", got.HitCount)
	}
}

func TestGetMiss(t *testing.T) {
	t.Parallel()

	c := New(Config{})
	if got := c.Get("nonexistent"); got != nil {
		t.Errorf("expected nil for miss, got %v", got)
	}
}

func TestGetExpired(t *testing.T) {
	t.Parallel()

	c := New(Config{TTL: 1 * time.Nanosecond})
	// Tiny TTL ensures entry expires before we read it.
	c.Set("fp", `{"action":"noop"}`)

	// Sleep briefly to ensure expiry.
	time.Sleep(1 * time.Microsecond)

	if got := c.Get("fp"); got != nil {
		t.Error("expected nil for expired entry")
	}
	// Cache should be empty after lazy expiry.
	if c.Len() != 0 {
		t.Error("expired entry should have been deleted")
	}
}

func TestFingerprintDeterministic(t *testing.T) {
	t.Parallel()

	f1 := Fingerprint("default/nginx", "default", "cpu_usage", "Spike", 0.85)
	f2 := Fingerprint("default/nginx", "default", "cpu_usage", "Spike", 0.85)
	if f1 != f2 {
		t.Errorf("fingerprints should match: %q vs %q", f1, f2)
	}
}

func TestFingerprintDifferent(t *testing.T) {
	t.Parallel()

	f1 := Fingerprint("default/nginx", "default", "cpu_usage", "Spike", 0.85)
	f2 := Fingerprint("default/nginx", "default", "memory_usage", "Spike", 0.85)
	if f1 == f2 {
		t.Error("fingerprints should differ for different metric names")
	}
}

func TestFingerprintIgnoresScore(t *testing.T) {
	t.Parallel()

	f1 := Fingerprint("default/nginx", "default", "cpu_usage", "Spike", 0.84)
	f2 := Fingerprint("default/nginx", "default", "cpu_usage", "Spike", 0.81)
	if f1 != f2 {
		t.Errorf("fingerprints should ignore score differences")
	}
}

func TestPrune(t *testing.T) {
	t.Parallel()

	c := New(Config{TTL: 1 * time.Nanosecond})
	c.Set("fp1", `{}`)
	c.Set("fp2", `{}`)

	// Wait for expiry.
	time.Sleep(1 * time.Microsecond)

	removed := c.Prune()
	if removed != 2 {
		t.Errorf("Prune removed %d, want 2", removed)
	}
	if c.Len() != 0 {
		t.Errorf("Len = %d, want 0 after prune", c.Len())
	}
}

func TestPrunePartial(t *testing.T) {
	t.Parallel()

	c := New(Config{TTL: 10 * time.Minute})
	c.Set("fresh", `{}`)

	// Manually add an expired entry.
	c.mu.Lock()
	c.store["stale"] = &Entry{
		Fingerprint: "stale",
		PlanJSON:    `{"action":"old"}`,
		CreatedAt:   time.Now().Add(-2 * time.Hour),
		ExpiresAt:   time.Now().Add(-1 * time.Hour),
	}
	c.mu.Unlock()

	removed := c.Prune()
	if removed != 1 {
		t.Errorf("Prune removed %d, want 1", removed)
	}
	if c.Len() != 1 {
		t.Errorf("Len = %d, want 1 (fresh entry should survive)", c.Len())
	}
}

func TestEviction(t *testing.T) {
	t.Parallel()

	c := New(Config{TTL: 10 * time.Minute, MaxEntries: 3})

	// Fill to capacity.
	c.Set("a", `{"action":"a"}`)
	c.Set("b", `{"action":"b"}`)
	c.Set("c", `{"action":"c"}`)

	if c.Len() != 3 {
		t.Errorf("Len = %d, want 3", c.Len())
	}

	// Adding a 4th should evict the oldest.
	c.Set("d", `{"action":"d"}`)

	if c.Len() != 3 {
		t.Errorf("Len = %d, want 3 after eviction", c.Len())
	}

	// "a" should be evicted (oldest).
	if got := c.Get("a"); got != nil {
		t.Error("oldest entry 'a' should have been evicted")
	}
	if got := c.Get("d"); got == nil {
		t.Error("newest entry 'd' should exist")
	}
}

func TestConcurrentAccess(_ *testing.T) {
	// No t.Parallel — this IS the concurrency test.
	c := New(Config{TTL: 10 * time.Minute, MaxEntries: 100})

	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			fp := Fingerprint("default/pod", "default", "metric", "pattern", float64(n))
			c.Set(fp, `{"n":`+fmt.Sprintf("%d", n)+`}`)
			c.Get(fp)
			c.Prune()
			c.Len()
			c.Stats()
			c.Keys()
		}(i)
	}
	wg.Wait()
}

func TestStats(t *testing.T) {
	t.Parallel()

	c := New(Config{TTL: 30 * time.Minute, MaxEntries: 50})
	c.Set("fp", `{}`)

	s := c.Stats()
	if s.Entries != 1 {
		t.Errorf("Entries = %d, want 1", s.Entries)
	}
	if s.Capacity != 50 {
		t.Errorf("Capacity = %d, want 50", s.Capacity)
	}
	if s.TTL != 30*time.Minute {
		t.Errorf("TTL = %v, want 30m", s.TTL)
	}
}

func TestKeys(t *testing.T) {
	t.Parallel()

	c := New(Config{})
	c.Set("b", `{}`)
	c.Set("a", `{}`)
	c.Set("c", `{}`)

	keys := c.Keys()
	if len(keys) != 3 {
		t.Fatalf("Keys len = %d, want 3", len(keys))
	}
	// Should be sorted.
	if keys[0] != "a" || keys[1] != "b" || keys[2] != "c" {
		t.Errorf("Keys not sorted: %v", keys)
	}
}

func TestHitCountIncrement(t *testing.T) {
	t.Parallel()

	c := New(Config{})
	c.Set("fp", `{}`)

	for i := 0; i < 5; i++ {
		c.Get("fp")
	}

	e := c.Get("fp")
	if e == nil {
		t.Fatal("expected entry")
	}
	// 0 from Set + 6 Gets = 6
	if e.HitCount != 6 {
		t.Errorf("HitCount = %d, want 6", e.HitCount)
	}
}
