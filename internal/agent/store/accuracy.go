package store

import (
	"encoding/json"
	"fmt"
	"sort"
	"time"

	bolt "go.etcd.io/bbolt"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// SaveAccuracySnapshot persists an accuracy snapshot. Keyed by timestamp.
func (s *Store) SaveAccuracySnapshot(snap models.AccuracySnapshot) error {
	data, err := json.Marshal(snap)
	if err != nil {
		return fmt.Errorf("marshal accuracy snapshot: %w", err)
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		b, err := tx.CreateBucketIfNotExists(bucketAccuracySnaps)
		if err != nil {
			return err
		}
		key := fmt.Sprintf("%d", snap.GeneratedAt.UnixMilli())
		return b.Put([]byte(key), data)
	})
}

// GetLatestAccuracySnapshot returns the most recent accuracy snapshot.
func (s *Store) GetLatestAccuracySnapshot() (*models.AccuracySnapshot, error) {
	var snap *models.AccuracySnapshot
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketAccuracySnaps)
		if b == nil {
			return nil
		}
		c := b.Cursor()
		k, v := c.Last()
		if k == nil {
			return nil
		}
		var s models.AccuracySnapshot
		if err := json.Unmarshal(v, &s); err != nil {
			return fmt.Errorf("unmarshal accuracy snapshot: %w", err)
		}
		snap = &s
		return nil
	})
	return snap, err
}

// GetAccuracyHistory returns accuracy snapshots in reverse chronological order.
func (s *Store) GetAccuracyHistory(limit int) ([]models.AccuracySnapshot, error) {
	if limit <= 0 {
		limit = 168
	}
	var items []models.AccuracySnapshot
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketAccuracySnaps)
		if b == nil {
			return nil
		}
		c := b.Cursor()
		for k, v := c.Last(); k != nil; k, v = c.Prev() {
			var snap models.AccuracySnapshot
			if err := json.Unmarshal(v, &snap); err != nil {
				continue
			}
			items = append(items, snap)
			if len(items) >= limit {
				break
			}
		}
		return nil
	})
	return items, err
}

// ComputeAccuracySnapshot performs the aggregation query across TrackedPrediction
// and AnomalyRecord records within the window, then persists and returns the result.
func (s *Store) ComputeAccuracySnapshot(window time.Duration) (*models.AccuracySnapshot, error) {
	since := time.Now().Add(-window)
	if window <= 0 {
		since = time.Time{}
	}

	preds, err := s.ListPredictionsInWindow(since, 100000)
	if err != nil {
		return nil, fmt.Errorf("list predictions in window: %w", err)
	}

	anomalyCount := s.countAnomaliesInWindow(since)

	byKind := make(map[string][]models.TrackedPrediction)
	byPred := make(map[string][]models.TrackedPrediction)
	byNS := make(map[string][]models.TrackedPrediction)
	byMetric := make(map[string][]models.TrackedPrediction)

	for _, tp := range preds {
		kind := resourceKindFromEntity(tp.Entity)
		byKind[kind] = append(byKind[kind], tp)
		byPred[tp.Pattern] = append(byPred[tp.Pattern], tp)
		byNS[tp.Namespace] = append(byNS[tp.Namespace], tp)
		byMetric[tp.MetricName] = append(byMetric[tp.MetricName], tp)
	}

	snap := &models.AccuracySnapshot{
		GeneratedAt:    time.Now(),
		Overall:        accuracyMetrics(preds, anomalyCount),
		ByResourceKind: make(map[string]models.AccuracyMetrics, len(byKind)),
		ByPredictor:    make(map[string]models.AccuracyMetrics, len(byPred)),
		ByNamespace:    make(map[string]models.AccuracyMetrics, len(byNS)),
		ByMetric:       make(map[string]models.AccuracyMetrics, len(byMetric)),
		RollingWindow:  window.String(),
	}

	for k, v := range byKind {
		snap.ByResourceKind[k] = accuracyMetrics(v, anomalyCount)
	}
	for k, v := range byPred {
		snap.ByPredictor[k] = accuracyMetrics(v, anomalyCount)
	}
	for k, v := range byNS {
		snap.ByNamespace[k] = accuracyMetrics(v, anomalyCount)
	}
	for k, v := range byMetric {
		snap.ByMetric[k] = accuracyMetrics(v, anomalyCount)
	}

	if err := s.SaveAccuracySnapshot(*snap); err != nil {
		return nil, fmt.Errorf("save accuracy snapshot: %w", err)
	}
	return snap, nil
}

// accuracyMetrics computes precision/recall/F1 from tracked predictions.
func accuracyMetrics(preds []models.TrackedPrediction, totalAnomalies int) models.AccuracyMetrics {
	m := models.AccuracyMetrics{
		TotalPredictions: len(preds),
	}
	var totalConf float64
	for _, tp := range preds {
		switch tp.Outcome {
		case models.PredictionOutcomeHit:
			m.Hits++
			totalConf += tp.Confidence
		case models.PredictionOutcomeMiss:
			m.Misses++
		case models.PredictionOutcomePending:
			m.Pending++
		case models.PredictionOutcomeExpired:
			m.Expired++
		}
	}
	resolved := m.Hits + m.Misses
	if resolved > 0 {
		m.Precision = float64(m.Hits) / float64(resolved)
	}
	if m.Hits > 0 {
		m.AvgConfidence = totalConf / float64(m.Hits)
	}
	if totalAnomalies > 0 {
		m.Recall = float64(m.Hits) / float64(totalAnomalies)
	}
	if m.Precision+m.Recall > 0 {
		m.F1Score = 2 * m.Precision * m.Recall / (m.Precision + m.Recall)
	}
	return m
}

// countAnomaliesInWindow counts AnomalyRecords with status "remediated" or "resolved".
func (s *Store) countAnomaliesInWindow(since time.Time) int {
	records, err := s.ListAnomalies(100000)
	if err != nil {
		return 0
	}
	count := 0
	for _, r := range records {
		if r.DetectedAt != nil && r.DetectedAt.Before(since) {
			continue
		}
		switch r.Status {
		case models.AnomalyStatusRemediated, models.AnomalyStatusResolved:
			count++
		}
	}
	return count
}

// resourceKindFromEntity tries to infer the resource kind from the entity name.
func resourceKindFromEntity(entity string) string {
	if entity == "" {
		return "unknown"
	}
	return "pod"
}

// ListPredictionsInWindow returns TrackedPredictions created at or after since.
func (s *Store) ListPredictionsInWindow(since time.Time, limit int) ([]models.TrackedPrediction, error) {
	if limit <= 0 {
		limit = 100000
	}
	var items []models.TrackedPrediction
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketOutcomes)
		if b == nil {
			return nil
		}
		return b.ForEach(func(k, v []byte) error {
			var tp models.TrackedPrediction
			if err := json.Unmarshal(v, &tp); err != nil {
				return nil
			}
			if !since.IsZero() && tp.CreatedAt.Before(since) {
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
