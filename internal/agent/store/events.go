package store

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	bolt "go.etcd.io/bbolt"
)

// StoredEvent is a persisted K8s event record, deduplicated by
// (namespace + involved_object + reason) within a compaction window.
type StoredEvent struct {
	ID             string    `json:"id"`
	Reason         string    `json:"reason"`
	Message        string    `json:"message"`
	InvolvedObject string    `json:"involved_object"`
	Namespace      string    `json:"namespace"`
	Source         string    `json:"source"`
	FirstSeen      time.Time `json:"first_seen"`
	LastSeen       time.Time `json:"last_seen"`
	Count          int32     `json:"count"`
}

// eventDedupKey returns the deduplication key for an event.
func eventDedupKey(ns, involved, reason string) string {
	return ns + "|" + involved + "|" + reason
}

// SaveEvent persists an event, deduplicating by (namespace + involved + reason).
// When an event with the same dedup key already exists, it updates the count and
// last-seen timestamp rather than creating a new record.
func (s *Store) SaveEvent(event *StoredEvent) error {
	if event.ID == "" {
		return fmt.Errorf("event ID must not be empty")
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketEvents)
		if b == nil {
			return fmt.Errorf("bucket %s not found", bucketEvents)
		}

		dedupKey := eventDedupKey(event.Namespace, event.InvolvedObject, event.Reason)
		existingID := b.Get([]byte("dedup|" + dedupKey))
		if existingID != nil {
			// Merge into existing record
			data := b.Get(existingID)
			if data != nil {
				var existing StoredEvent
				if err := json.Unmarshal(data, &existing); err == nil {
					existing.Count += event.Count
					existing.LastSeen = event.LastSeen
					if event.Message != "" && existing.Message != event.Message {
						existing.Message = event.Message
					}
					updated, err := json.Marshal(existing)
					if err != nil {
						return fmt.Errorf("marshal merged event: %w", err)
					}
					if err := b.Put(existingID, updated); err != nil {
						return err
					}
					// Update time index entry position
					return s.putEventTimeIndex(tx, existing.LastSeen, existing.ID)
				}
			}
		}

		data, err := json.Marshal(event)
		if err != nil {
			return fmt.Errorf("marshal event: %w", err)
		}
		if err := b.Put([]byte(event.ID), data); err != nil {
			return err
		}
		// Dedup index
		if err := b.Put([]byte("dedup|"+dedupKey), []byte(event.ID)); err != nil {
			return err
		}
		return s.putEventTimeIndex(tx, event.LastSeen, event.ID)
	})
}

func (s *Store) putEventTimeIndex(tx *bolt.Tx, t time.Time, id string) error {
	idx, err := tx.CreateBucketIfNotExists(bucketEventIndex)
	if err != nil {
		return err
	}
	return idx.Put(eventTimeIndexKey(t, id), []byte(id))
}

// eventTimeIndexKey returns a time-ordered key for event listing (newest first).
func eventTimeIndexKey(t time.Time, id string) []byte {
	inv := ^uint64(t.UnixNano())
	return []byte(fmt.Sprintf("%016x|%s", inv, id))
}

// ListEvents returns persisted events matching the filter, newest first.
func (s *Store) ListEvents(namespace string, since time.Time, limit int) ([]StoredEvent, error) {
	if limit <= 0 {
		limit = 50
	}
	var records []StoredEvent
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketEvents)
		idx := tx.Bucket(bucketEventIndex)
		if b == nil || idx == nil {
			return nil
		}
		c := idx.Cursor()
		for k, _ := c.First(); k != nil && len(records) < limit; k, _ = c.Next() {
			parts := splitEventIndexKey(string(k))
			if parts == nil {
				continue
			}
			id := parts[1]

			data := b.Get([]byte(id))
			if data == nil {
				continue
			}
			var e StoredEvent
			if err := json.Unmarshal(data, &e); err != nil {
				continue
			}
			if namespace != "" && e.Namespace != namespace {
				continue
			}
			if !since.IsZero() && e.LastSeen.Before(since) {
				continue
			}
			records = append(records, e)
		}
		return nil
	})
	return records, err
}

func splitEventIndexKey(key string) []string {
	// Format: %016x|id
	if len(key) < 17 || key[16] != '|' {
		return nil
	}
	return []string{key[:16], key[17:]}
}

// EventGroup is a group of events aggregated by reason.
type EventGroup struct {
	Reason       string `json:"reason"`
	Count        int    `json:"count"`
	DistinctPods int    `json:"distinct_pods"`
}

// AggregateEvents groups persisted events by reason within a time window.
func (s *Store) AggregateEvents(window time.Duration) ([]EventGroup, error) {
	since := time.Now().Add(-window)
	all, err := s.ListEvents("", since, 0)
	if err != nil {
		return nil, err
	}

	groups := make(map[string]*EventGroup)
	podsSeen := make(map[string]map[string]bool) // reason -> set of involved objects

	for _, e := range all {
		g, ok := groups[e.Reason]
		if !ok {
			g = &EventGroup{Reason: e.Reason}
			groups[e.Reason] = g
			podsSeen[e.Reason] = make(map[string]bool)
		}
		g.Count++
		podsSeen[e.Reason][e.InvolvedObject] = true
	}

	result := make([]EventGroup, 0, len(groups))
	for reason, g := range groups {
		g.DistinctPods = len(podsSeen[reason])
		result = append(result, *g)
	}
	return result, nil
}

// CompactEvents removes events older than the given retention duration.
func (s *Store) CompactEvents(retention time.Duration) (int, error) {
	cutoff := time.Now().Add(-retention)
	var deleted int
	err := s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketEvents)
		idx := tx.Bucket(bucketEventIndex)
		if b == nil || idx == nil {
			return nil
		}

		var eventIDs []string
		var indexKeys [][]byte
		c := idx.Cursor()
		for k, _ := c.Last(); k != nil; k, _ = c.Prev() {
			parts := splitEventIndexKey(string(k))
			if parts == nil {
				continue
			}
			id := parts[1]
			data := b.Get([]byte(id))
			if data == nil {
				indexKeys = append(indexKeys, append([]byte(nil), k...))
				continue
			}
			var e StoredEvent
			if err := json.Unmarshal(data, &e); err != nil {
				continue
			}
			if e.LastSeen.Before(cutoff) {
				eventIDs = append(eventIDs, id)
				indexKeys = append(indexKeys, append([]byte(nil), k...))
			}
		}

		for _, id := range eventIDs {
			data := b.Get([]byte(id))
			if data != nil {
				var e StoredEvent
				if json.Unmarshal(data, &e) == nil {
					_ = b.Delete([]byte("dedup|" + eventDedupKey(e.Namespace, e.InvolvedObject, e.Reason)))
				}
			}
			_ = b.Delete([]byte(id))
			deleted++
		}
		for _, k := range indexKeys {
			_ = idx.Delete(k)
		}
		return nil
	})
	return deleted, err
}

func isEventID(id string) bool {
	return strings.HasPrefix(id, "evt-")
}
