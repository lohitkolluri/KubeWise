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
	Persist    PersistBackend
}

// PersistBackend optionally persists cache entries across agent restarts.
type PersistBackend interface {
	LoadAll() (map[string]*Entry, error)
	Save(entry *Entry) error
	Delete(fp string) error
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
	mu      sync.RWMutex
	store   map[string]*Entry
	cfg     Config
	persist PersistBackend
}

// New creates a cache with the given config.
// Zero-value config uses sensible defaults. When Persist is configured, existing
// non-expired entries are loaded on startup.
func New(cfg Config) *Cache {
	if cfg.TTL <= 0 {
		cfg.TTL = DefaultTTL
	}
	if cfg.MaxEntries <= 0 {
		cfg.MaxEntries = DefaultMaxEntries
	}
	c := &Cache{
		store:   make(map[string]*Entry, 64),
		cfg:     cfg,
		persist: cfg.Persist,
	}
	if c.persist != nil {
		if loaded, err := c.persist.LoadAll(); err == nil {
			now := time.Now()
			for fp, e := range loaded {
				if e != nil && now.Before(e.ExpiresAt) {
					c.store[fp] = e
				}
			}
		}
	}
	return c
}

// scoreBucket returns a severity label for a 0-1 score value.
// Used in the fingerprint so different severity levels produce distinct cache keys,
// while minor score fluctuations within the same bucket still hit the cache.
func scoreBucket(score float64) string {
	switch {
	case score >= 0.75:
		return "critical"
	case score >= 0.5:
		return "high"
	case score >= 0.25:
		return "medium"
	default:
		return "low"
	}
}

// Fingerprint computes a content hash of an anomaly's distinguishing fields:
// entity, namespace, metric name, pattern, and score bucket.
// The score bucket (low/medium/high/critical) is included so different severity
// levels produce different cache keys, while minor score fluctuations within the
// same bucket still hit the cache.
// Zero-byte separators between fields prevent collisions (e.g. "ab"+"cd" vs "a"+"bcd").
func Fingerprint(entity, namespace, metricName, pattern string, score float64) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s\x00%s\x00%s\x00%s\x00%s", entity, namespace, metricName, pattern, scoreBucket(score))
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
		if c.persist != nil {
			_ = c.persist.Delete(fp)
		}
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

	// Evict oldest if at capacity and the key doesn't already exist.
	if len(c.store) >= c.cfg.MaxEntries {
		if _, exists := c.store[fp]; !exists {
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
	}

	now := time.Now()
	entry := &Entry{
		Fingerprint: fp,
		PlanJSON:    planJSON,
		CreatedAt:   now,
		ExpiresAt:   now.Add(c.cfg.TTL),
		HitCount:    0,
	}
	c.store[fp] = entry
	if c.persist != nil {
		_ = c.persist.Save(entry)
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
			if c.persist != nil {
				_ = c.persist.Delete(k)
			}
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

// Anomaly is implemented by types that can be fingerprinted for semantic cache lookups.
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
