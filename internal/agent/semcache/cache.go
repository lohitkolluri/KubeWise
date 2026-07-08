// Package semcache provides a hash-based semantic cache for LLM remediation results.
// It deduplicates identical anomaly incidents to avoid redundant LLM calls.
// Uses SHA256 content hashing — no embedding models or vector storage.
package semcache

import (
	"crypto/sha256"
	"fmt"
	"sort"
	"sync"
	"time"
)

// DefaultTTL is how long a cached result remains valid.
const DefaultTTL = 1 * time.Hour

// DefaultMaxEntries limits the cache size to prevent memory growth.
const DefaultMaxEntries = 1000

// Config controls cache behavior.
type Config struct {
	TTL        time.Duration // entry time-to-live (default 1h)
	MaxEntries int           // max cached entries before GC (default 1000)
}

// Entry is a single cached remediation result.
type Entry struct {
	Fingerprint string    // hash key
	PlanJSON    string    // serialized RemediationPlan
	CreatedAt   time.Time // when this entry was cached
	ExpiresAt   time.Time // when this entry expires
	HitCount    int       // how many times this entry was served (incl. the Set)
}

// Cache is an in-memory hash-based semantic cache.
// Thread-safe: all exported methods are safe for concurrent use.
type Cache struct {
	mu    sync.RWMutex
	store map[string]*Entry
	cfg   Config
}

// New creates a cache with the given config.
// Zero-value config uses sensible defaults.
func New(cfg Config) *Cache {
	if cfg.TTL <= 0 {
		cfg.TTL = DefaultTTL
	}
	if cfg.MaxEntries <= 0 {
		cfg.MaxEntries = DefaultMaxEntries
	}
	return &Cache{
		store: make(map[string]*Entry, 64),
		cfg:   cfg,
	}
}

// Fingerprint computes a content hash of an anomaly's distinguishing fields:
// entity, namespace, metric name, pattern, and score (rounded to 1 decimal).
// Returns a hex-encoded SHA256 string.
func Fingerprint(entity, namespace, metricName, pattern string, score float64) string {
	// Round score to 1 decimal for fuzzy matching within same severity bucket.
	rounded := fmt.Sprintf("%.1f", score)

	h := sha256.New()
	// Sort-stable fields so the order is deterministic.
	h.Write([]byte(entity))
	h.Write([]byte(namespace))
	h.Write([]byte(metricName))
	h.Write([]byte(pattern))
	h.Write([]byte(rounded))
	return fmt.Sprintf("%x", h.Sum(nil))
}

// Get returns the cached entry for the given fingerprint, or nil on miss/expiry.
// Expired entries are deleted lazily on read.
func (c *Cache) Get(fp string) *Entry {
	c.mu.Lock()
	defer c.mu.Unlock()

	e, ok := c.store[fp]
	if !ok {
		return nil
	}
	if time.Now().After(e.ExpiresAt) {
		delete(c.store, fp)
		return nil
	}
	e.HitCount++
	return e
}

// Set stores a remediation result under the given fingerprint.
// If the cache is at capacity, it evicts the oldest entry.
func (c *Cache) Set(fp, planJSON string) {
	c.mu.Lock()
	defer c.mu.Unlock()

	// Evict oldest if at capacity.
	if len(c.store) >= c.cfg.MaxEntries {
		var oldestKey string
		var oldestTime time.Time
		for k, v := range c.store {
			if oldestKey == "" || v.CreatedAt.Before(oldestTime) {
				oldestKey = k
				oldestTime = v.CreatedAt
			}
		}
		delete(c.store, oldestKey)
	}

	now := time.Now()
	c.store[fp] = &Entry{
		Fingerprint: fp,
		PlanJSON:    planJSON,
		CreatedAt:   now,
		ExpiresAt:   now.Add(c.cfg.TTL),
		HitCount:    0,
	}
}

// Prune removes all expired entries and returns the count removed.
func (c *Cache) Prune() int {
	c.mu.Lock()
	defer c.mu.Unlock()

	now := time.Now()
	var removed int
	for k, v := range c.store {
		if now.After(v.ExpiresAt) {
			delete(c.store, k)
			removed++
		}
	}
	return removed
}

// Len returns the current number of cached entries.
func (c *Cache) Len() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.store)
}

// Stats returns a snapshot of cache statistics.
type Stats struct {
	Entries  int
	Capacity int
	TTL      time.Duration
}

// Stats returns current cache statistics.
func (c *Cache) Stats() Stats {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return Stats{
		Entries:  len(c.store),
		Capacity: c.cfg.MaxEntries,
		TTL:      c.cfg.TTL,
	}
}

// FingerprintForAnomaly is a convenience wrapper that extracts fields from an anomaly-like type.
type Anomaly interface {
	GetEntity() string
	GetNamespace() string
	GetMetricName() string
	GetPattern() string
	GetScore() float64
}

// anomalyAdapter adapts a struct with the right field names to the Anomaly interface.
// Fields are matched by name convention.

// Keys returns all fingerprints currently in the cache (for inspection/debug).
func (c *Cache) Keys() []string {
	c.mu.RLock()
	defer c.mu.RUnlock()

	keys := make([]string, 0, len(c.store))
	for k := range c.store {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
