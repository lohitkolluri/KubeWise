package store

import (
	"encoding/json"
	"fmt"
	"sort"
	"time"

	bolt "go.etcd.io/bbolt"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// SaveHealthScore persists a health score snapshot.  Keyed by entity name —
// a subsequent computation overwrites the previous score for the same entity,
// while the history bucket receives an append-only copy.
func (s *Store) SaveHealthScore(hs models.HealthScore) error {
	return s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketHealthScores)
		if b == nil {
			return fmt.Errorf("bucket %q not found", bucketHealthScores)
		}
		data, err := json.Marshal(hs)
		if err != nil {
			return fmt.Errorf("marshal health score: %w", err)
		}
		// Overwrite the current score.
		key := entityHealthKey(hs.Namespace, hs.Entity)
		if err := b.Put(key, data); err != nil {
			return err
		}
		// Append to history.
		hb := tx.Bucket(bucketHealthHistory)
		if hb == nil {
			return fmt.Errorf("bucket %q not found", bucketHealthHistory)
		}
		histKey := fmt.Sprintf("%s@%d", string(key), hs.GeneratedAt.UnixMilli())
		return hb.Put([]byte(histKey), data)
	})
}

// GetLatestHealthScores returns all current health scores.
func (s *Store) GetLatestHealthScores() ([]models.HealthScore, error) {
	var items []models.HealthScore
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketHealthScores)
		if b == nil {
			return nil
		}
		return b.ForEach(func(k, v []byte) error {
			var hs models.HealthScore
			if err := json.Unmarshal(v, &hs); err != nil {
				return nil // skip corrupt records
			}
			items = append(items, hs)
			return nil
		})
	})
	if err != nil {
		return nil, err
	}
	sort.Slice(items, func(i, j int) bool {
		return items[i].Score < items[j].Score // worst first
	})
	return items, nil
}

// GetHealthScoresByNamespace returns current health scores for one namespace.
func (s *Store) GetHealthScoresByNamespace(ns string) ([]models.HealthScore, error) {
	all, err := s.GetLatestHealthScores()
	if err != nil {
		return nil, err
	}
	var filtered []models.HealthScore
	for _, hs := range all {
		if hs.Namespace == ns {
			filtered = append(filtered, hs)
		}
	}
	return filtered, nil
}

// GetHealthScoreHistory returns historical health scores for one entity,
// newest first, capped at limit.
func (s *Store) GetHealthScoreHistory(entity string, namespace string, limit int) ([]models.HealthScore, error) {
	if limit <= 0 {
		limit = 100
	}
	prefix := entityHealthKey(namespace, entity)
	var items []models.HealthScore
	err := s.db.View(func(tx *bolt.Tx) error {
		hb := tx.Bucket(bucketHealthHistory)
		if hb == nil {
			return nil
		}
		c := hb.Cursor()
		for k, v := c.Last(); k != nil; k, v = c.Prev() {
			if len(k) < len(prefix) || string(k[:len(prefix)]) != string(prefix) {
				continue
			}
			var hs models.HealthScore
			if err := json.Unmarshal(v, &hs); err != nil {
				continue
			}
			items = append(items, hs)
			if len(items) >= limit {
				break
			}
		}
		return nil
	})
	return items, err
}

// ComputeClusterSummary aggregates all current health scores into a summary.
func (s *Store) ComputeClusterSummary() (*models.ClusterHealthSummary, error) {
	scores, err := s.GetLatestHealthScores()
	if err != nil {
		return nil, err
	}
	summary := &models.ClusterHealthSummary{
		ResourceCount: len(scores),
		GeneratedAt:   time.Now(),
	}
	var total float64
	for _, hs := range scores {
		total += hs.Score
		switch {
		case hs.Score >= 80:
			summary.HealthyCount++
		case hs.Score >= 50:
			summary.WarningCount++
		default:
			summary.CriticalCount++
		}
	}
	if len(scores) > 0 {
		summary.OverallScore = total / float64(len(scores))
	}
	// Top 5 worst.
	n := 5
	if len(scores) < n {
		n = len(scores)
	}
	summary.TopRisks = scores[:n]
	return summary, nil
}

// entityHealthKey produces a deterministic key for a namespace+entity pair.
func entityHealthKey(namespace, entity string) []byte {
	if namespace == "" {
		return []byte(entity)
	}
	return []byte(namespace + "/" + entity)
}
