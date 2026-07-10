package store

import (
	"encoding/json"
	"fmt"
	"time"

	bolt "go.etcd.io/bbolt"

	"github.com/lohitkolluri/KubeWise/internal/agent/semcache"
)

type semCacheRecord struct {
	Fingerprint string    `json:"fingerprint"`
	PlanJSON    string    `json:"plan_json"`
	CreatedAt   time.Time `json:"created_at"`
	ExpiresAt   time.Time `json:"expires_at"`
	HitCount    int       `json:"hit_count"`
}

// SemCacheBackend persists semantic cache entries in bbolt.
type SemCacheBackend struct {
	store *Store
}

// NewSemCacheBackend returns a persistence backend for the in-memory semcache.
func NewSemCacheBackend(s *Store) *SemCacheBackend {
	return &SemCacheBackend{store: s}
}

// LoadAll reads all cached entries from the bolt bucket.
func (b *SemCacheBackend) LoadAll() (map[string]*semcache.Entry, error) {
	if b == nil || b.store == nil {
		return nil, nil
	}
	out := make(map[string]*semcache.Entry)
	err := b.store.db.View(func(tx *bolt.Tx) error {
		bkt := tx.Bucket(bucketSemCache)
		if bkt == nil {
			return nil
		}
		return bkt.ForEach(func(k, v []byte) error {
			var rec semCacheRecord
			if err := json.Unmarshal(v, &rec); err != nil {
				return nil
			}
			out[string(k)] = &semcache.Entry{
				Fingerprint: rec.Fingerprint,
				PlanJSON:    rec.PlanJSON,
				CreatedAt:   rec.CreatedAt,
				ExpiresAt:   rec.ExpiresAt,
				HitCount:    rec.HitCount,
			}
			return nil
		})
	})
	return out, err
}

// Save persists a cache entry to the bolt bucket.
func (b *SemCacheBackend) Save(entry *semcache.Entry) error {
	if b == nil || b.store == nil || entry == nil || entry.Fingerprint == "" {
		return nil
	}
	rec := semCacheRecord{
		Fingerprint: entry.Fingerprint,
		PlanJSON:    entry.PlanJSON,
		CreatedAt:   entry.CreatedAt,
		ExpiresAt:   entry.ExpiresAt,
		HitCount:    entry.HitCount,
	}
	data, err := json.Marshal(rec)
	if err != nil {
		return fmt.Errorf("marshal semcache entry: %w", err)
	}
	return b.store.db.Update(func(tx *bolt.Tx) error {
		bkt, err := tx.CreateBucketIfNotExists(bucketSemCache)
		if err != nil {
			return err
		}
		return bkt.Put([]byte(entry.Fingerprint), data)
	})
}

// Delete removes a cache entry by its fingerprint.
func (b *SemCacheBackend) Delete(fp string) error {
	if b == nil || b.store == nil || fp == "" {
		return nil
	}
	return b.store.db.Update(func(tx *bolt.Tx) error {
		bkt := tx.Bucket(bucketSemCache)
		if bkt == nil {
			return nil
		}
		return bkt.Delete([]byte(fp))
	})
}
