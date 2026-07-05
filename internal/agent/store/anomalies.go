package store

import (
	"encoding/json"
	"fmt"

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

// ListAnomalies returns the most recent anomaly records up to limit.
func (s *Store) ListAnomalies(limit int) ([]models.AnomalyRecord, error) {
	if limit <= 0 {
		return nil, nil
	}
	var records []models.AnomalyRecord
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketAnomalies)
		c := b.Cursor()
		for k, v := c.Last(); k != nil && len(records) < limit; k, _ = c.Prev() {
			var r models.AnomalyRecord
			if err := json.Unmarshal(v, &r); err != nil {
				return fmt.Errorf("unmarshal anomaly %s: %w", k, err)
			}
			records = append(records, r)
		}
		return nil
	})
	return records, err
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
		return tx.Bucket(bucketAnomalies).Put([]byte(r.ID), data)
	})
}
