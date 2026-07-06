package store

import (
	"encoding/json"
	"fmt"
	"sort"

	bolt "go.etcd.io/bbolt"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// SaveAnomaly stores an anomaly record.
func (s *Store) SaveAnomaly(r *models.AnomalyRecord) error {
	if r.ID == "" {
		return fmt.Errorf("anomaly ID must not be empty")
	}
	data, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("marshal anomaly: %w", err)
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		return tx.Bucket(bucketAnomalies).Put([]byte(r.ID), data)
	})
}

// ListAnomalies returns the most recent anomaly records up to limit, ordered by DetectedAt descending.
func (s *Store) ListAnomalies(limit int) ([]models.AnomalyRecord, error) {
	records, err := s.listAllAnomalies()
	if err != nil {
		return nil, err
	}
	if limit > 0 && len(records) > limit {
		records = records[:limit]
	}
	return records, nil
}

func (s *Store) listAllAnomalies() ([]models.AnomalyRecord, error) {
	var records []models.AnomalyRecord
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketAnomalies)
		return b.ForEach(func(_, v []byte) error {
			var r models.AnomalyRecord
			if err := json.Unmarshal(v, &r); err != nil {
				return fmt.Errorf("unmarshal anomaly: %w", err)
			}
			records = append(records, r)
			return nil
		})
	})
	if err != nil {
		return nil, err
	}

	sort.Slice(records, func(i, j int) bool {
		ti := records[i].DetectedAt
		tj := records[j].DetectedAt
		if ti == nil && tj == nil {
			return records[i].ID > records[j].ID
		}
		if ti == nil {
			return false
		}
		if tj == nil {
			return true
		}
		return ti.After(*tj)
	})
	return records, nil
}

// GetAnomaly retrieves a single anomaly record by ID.
func (s *Store) GetAnomaly(id string) (*models.AnomalyRecord, error) {
	var record *models.AnomalyRecord
	err := s.db.View(func(tx *bolt.Tx) error {
		data := tx.Bucket(bucketAnomalies).Get([]byte(id))
		if data == nil {
			return nil
		}
		var r models.AnomalyRecord
		if err := json.Unmarshal(data, &r); err != nil {
			return fmt.Errorf("unmarshal anomaly %s: %w", id, err)
		}
		record = &r
		return nil
	})
	return record, err
}

// UpdateAnomaly updates an existing anomaly record.
func (s *Store) UpdateAnomaly(r *models.AnomalyRecord) error {
	if r.ID == "" {
		return fmt.Errorf("anomaly ID must not be empty")
	}
	data, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("marshal anomaly: %w", err)
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketAnomalies)
		if b.Get([]byte(r.ID)) == nil {
			return fmt.Errorf("anomaly %s not found", r.ID)
		}
		return b.Put([]byte(r.ID), data)
	})
}

// isOpenAnomalyStatus reports whether an anomaly is still open for deduplication.
func isOpenAnomalyStatus(status string) bool {
	switch status {
	case models.AnomalyStatusDetected, models.AnomalyStatusActive, models.AnomalyStatusCorrelated:
		return true
	default:
		return false
	}
}

// FindOpenAnomaly returns the newest open anomaly for entity+signal if one exists.
func (s *Store) FindOpenAnomaly(entity, signal string) (*models.AnomalyRecord, error) {
	records, err := s.listAllAnomalies()
	if err != nil {
		return nil, err
	}
	for _, r := range records {
		if r.Entity != entity {
			continue
		}
		if r.MetricName != signal && r.Pattern != signal {
			continue
		}
		if isOpenAnomalyStatus(r.Status) {
			return &r, nil
		}
	}
	return nil, nil
}

// UpsertOpenAnomaly saves a new anomaly or updates score/timestamp on an existing open one.
// Returns true if a new record was created.
func (s *Store) UpsertOpenAnomaly(r *models.AnomalyRecord) (bool, error) {
	signal := r.MetricName
	if signal == "" {
		signal = r.Pattern
	}
	existing, err := s.FindOpenAnomaly(r.Entity, signal)
	if err != nil {
		return false, err
	}
	if existing != nil {
		existing.Score = r.Score
		existing.DetectedAt = r.DetectedAt
		if r.Pattern != "" {
			existing.Pattern = r.Pattern
		}
		return false, s.UpdateAnomaly(existing)
	}
	return true, s.SaveAnomaly(r)
}
