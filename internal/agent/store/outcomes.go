package store

import (
	"encoding/json"
	"fmt"
	"sort"
	"time"

	bolt "go.etcd.io/bbolt"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// SaveTrackedPrediction persists a new tracked prediction.
func (s *Store) SaveTrackedPrediction(tp models.TrackedPrediction) error {
	data, err := json.Marshal(tp)
	if err != nil {
		return fmt.Errorf("marshal tracked prediction: %w", err)
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		b, err := tx.CreateBucketIfNotExists(bucketOutcomes)
		if err != nil {
			return err
		}
		return b.Put([]byte(tp.ID), data)
	})
}

// ListTrackedPredictions returns tracked predictions, optionally filtered by outcome.
func (s *Store) ListTrackedPredictions(outcome string, limit int) ([]models.TrackedPrediction, error) {
	if limit <= 0 {
		limit = 100
	}
	var items []models.TrackedPrediction
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketOutcomes)
		if b == nil {
			return nil
		}
		return b.ForEach(func(_, v []byte) error {
			if len(items) >= maxRecordsInMemory {
				return fmt.Errorf("too many tracked predictions (>= %d); use filtering to narrow results", maxRecordsInMemory)
			}
			var tp models.TrackedPrediction
			if err := json.Unmarshal(v, &tp); err != nil {
				return nil
			}
			if outcome != "" && tp.Outcome != outcome {
				return nil
			}
			items = append(items, tp)
			return nil
		})
	})
	if err != nil {
		return nil, err
	}
	sort.Slice(items, func(i, j int) bool {
		return items[i].CreatedAt.After(items[j].CreatedAt)
	})
	if len(items) > limit {
		items = items[:limit]
	}
	return items, nil
}

// UpdateTrackedPrediction writes an updated tracked prediction.
func (s *Store) UpdateTrackedPrediction(tp models.TrackedPrediction) error {
	return s.SaveTrackedPrediction(tp)
}

// ComputeAgentStats aggregates prediction and remediation outcome metrics.
func (s *Store) ComputeAgentStats() (models.AgentStats, error) {
	stats := models.AgentStats{}

	pending, err := s.ListTrackedPredictions(models.PredictionOutcomePending, 10000)
	if err != nil {
		return stats, err
	}
	stats.PredictionsPending = len(pending)

	all, err := s.ListTrackedPredictions("", 10000)
	if err != nil {
		return stats, err
	}
	for _, tp := range all {
		stats.PredictionsTotal++
		switch tp.Outcome {
		case models.PredictionOutcomeHit:
			stats.PredictionsHit++
		case models.PredictionOutcomeMiss:
			stats.PredictionsMissed++
		case models.PredictionOutcomeExpired:
			stats.PredictionsExpired++
		}
	}
	resolved := stats.PredictionsHit + stats.PredictionsMissed
	if resolved > 0 {
		stats.PredictionAccuracy = float64(stats.PredictionsHit) / float64(resolved)
	}

	audits, err := s.ListAuditRecords(10000)
	if err != nil {
		return stats, err
	}
	for _, a := range audits {
		stats.RemediationsTotal++
		switch a.Status {
		case models.AuditVerified, models.AuditExecuted:
			stats.RemediationsVerified++
		case models.AuditFailed, models.AuditVerifyFailed, models.AuditRejected:
			stats.RemediationsFailed++
		case models.AuditDryRun:
			stats.RemediationsDryRun++
		case models.AuditPending:
			stats.RemediationsPending++
		}
	}
	return stats, nil
}

// PruneExpiredPredictions marks pending predictions past ExpiresAt as expired.
func (s *Store) PruneExpiredPredictions(now time.Time) (int, error) {
	pending, err := s.ListTrackedPredictions(models.PredictionOutcomePending, 10000)
	if err != nil {
		return 0, err
	}
	count := 0
	for _, tp := range pending {
		if now.Before(tp.ExpiresAt) {
			continue
		}
		tp.Outcome = models.PredictionOutcomeMiss
		t := now
		tp.ResolvedAt = &t
		if err := s.UpdateTrackedPrediction(tp); err != nil {
			return count, err
		}
		count++
	}
	return count, nil
}
