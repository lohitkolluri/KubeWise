package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	bolterrors "go.etcd.io/bbolt/errors"

	bolt "go.etcd.io/bbolt"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func anomalyTimeIndexKey(t *time.Time, id string) []byte {
	var inv = ^uint64(0)
	if t != nil && !t.IsZero() {
		inv = ^uint64(t.UnixNano())
	}
	return []byte(fmt.Sprintf("%016x|%s", inv, id))
}

func anomalyOpenKey(entity, signal string) []byte {
	return []byte(entity + "|" + signal)
}

func auditTimeIndexKey(t time.Time, id string) []byte {
	inv := ^uint64(t.UnixNano())
	return []byte(fmt.Sprintf("%016x|%s", inv, id))
}

func auditStatusIndexKey(status models.AuditStatus, t time.Time, id string) []byte {
	inv := ^uint64(t.UnixNano())
	return []byte(fmt.Sprintf("%s|%016x|%s", status, inv, id))
}

func anomalySignal(r *models.AnomalyRecord) string {
	if r.MetricName != "" {
		return r.MetricName
	}
	return r.Pattern
}

// SaveAnomaly stores an anomaly record and maintains indexes.
func (s *Store) SaveAnomaly(r *models.AnomalyRecord) error {
	if r.ID == "" {
		return fmt.Errorf("anomaly ID must not be empty")
	}
	data, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("marshal anomaly: %w", err)
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		if err := tx.Bucket(bucketAnomalies).Put([]byte(r.ID), data); err != nil {
			return err
		}
		idx, err := tx.CreateBucketIfNotExists(bucketAnomalyIndex)
		if err != nil {
			return err
		}
		if err := idx.Put(anomalyTimeIndexKey(r.DetectedAt, r.ID), []byte(r.ID)); err != nil {
			return err
		}
		return s.syncAnomalyOpenIndex(tx, r, nil)
	})
}

// ListAnomalies returns the most recent anomaly records up to limit, ordered by DetectedAt descending.
func (s *Store) ListAnomalies(limit int) ([]models.AnomalyRecord, error) {
	if limit <= 0 {
		limit = 20
	}
	var records []models.AnomalyRecord
	err := s.db.View(func(tx *bolt.Tx) error {
		idx := tx.Bucket(bucketAnomalyIndex)
		main := tx.Bucket(bucketAnomalies)
		if idx == nil || main == nil {
			return nil
		}
		c := idx.Cursor()
		for k, _ := c.First(); k != nil && len(records) < limit; k, _ = c.Next() {
			parts := strings.SplitN(string(k), "|", 2)
			if len(parts) != 2 {
				continue
			}
			data := main.Get([]byte(parts[1]))
			if data == nil {
				continue
			}
			var r models.AnomalyRecord
			if err := json.Unmarshal(data, &r); err != nil {
				return fmt.Errorf("unmarshal anomaly: %w", err)
			}
			records = append(records, r)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	if len(records) == 0 {
		return s.listAllAnomaliesLegacy(limit)
	}
	return records, nil
}

func (s *Store) listAllAnomaliesLegacy(limit int) ([]models.AnomalyRecord, error) {
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
		if b == nil {
			return nil
		}
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
		ti, tj := records[i].DetectedAt, records[j].DetectedAt
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

// UpdateAnomaly updates an existing anomaly record and indexes.
func (s *Store) UpdateAnomaly(r *models.AnomalyRecord) error {
	if r.ID == "" {
		return fmt.Errorf("anomaly ID must not be empty")
	}
	data, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("marshal anomaly: %w", err)
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		main := tx.Bucket(bucketAnomalies)
		if main == nil || main.Get([]byte(r.ID)) == nil {
			return fmt.Errorf("anomaly %s not found", r.ID)
		}
		var prev models.AnomalyRecord
		_ = json.Unmarshal(main.Get([]byte(r.ID)), &prev)

		if err := main.Put([]byte(r.ID), data); err != nil {
			return err
		}
		idx := tx.Bucket(bucketAnomalyIndex)
		if idx != nil {
			_ = idx.Delete(anomalyTimeIndexKey(prev.DetectedAt, r.ID))
			if err := idx.Put(anomalyTimeIndexKey(r.DetectedAt, r.ID), []byte(r.ID)); err != nil {
				return err
			}
		}
		return s.syncAnomalyOpenIndex(tx, r, &prev)
	})
}

func (s *Store) syncAnomalyOpenIndex(tx *bolt.Tx, cur *models.AnomalyRecord, prev *models.AnomalyRecord) error {
	open, err := tx.CreateBucketIfNotExists(bucketAnomalyOpen)
	if err != nil {
		return err
	}
	if prev != nil && isOpenAnomalyStatus(prev.Status) {
		_ = open.Delete(anomalyOpenKey(prev.Entity, anomalySignal(prev)))
	}
	if isOpenAnomalyStatus(cur.Status) {
		return open.Put(anomalyOpenKey(cur.Entity, anomalySignal(cur)), []byte(cur.ID))
	}
	return nil
}

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
	var id string
	err := s.db.View(func(tx *bolt.Tx) error {
		open := tx.Bucket(bucketAnomalyOpen)
		if open == nil {
			return nil
		}
		data := open.Get(anomalyOpenKey(entity, signal))
		if data == nil {
			return nil
		}
		id = string(data)
		return nil
	})
	if err != nil || id == "" {
		if id == "" {
			return s.findOpenAnomalyLegacy(entity, signal)
		}
		return nil, err
	}
	return s.GetAnomaly(id)
}

func (s *Store) findOpenAnomalyLegacy(entity, signal string) (*models.AnomalyRecord, error) {
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
func (s *Store) UpsertOpenAnomaly(r *models.AnomalyRecord) (bool, error) {
	signal := anomalySignal(r)
	existing, err := s.FindOpenAnomaly(r.Entity, signal)
	if err != nil {
		return false, err
	}
	if existing != nil {
		existing.Score = r.Score
		existing.DetectedAt = r.DetectedAt
		if r.MetricName != "" {
			existing.MetricName = r.MetricName
		}
		if r.Pattern != "" {
			existing.Pattern = r.Pattern
		}
		return false, s.UpdateAnomaly(existing)
	}
	return true, s.SaveAnomaly(r)
}

// RebuildAnomalyIndexes rebuilds indexes from the anomalies bucket (migration helper).
func (s *Store) RebuildAnomalyIndexes() error {
	records, err := s.listAllAnomalies()
	if err != nil {
		return err
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		if err := tx.DeleteBucket(bucketAnomalyIndex); err != nil && !errors.Is(err, bolterrors.ErrBucketNotFound) {
			return err
		}
		if err := tx.DeleteBucket(bucketAnomalyOpen); err != nil && !errors.Is(err, bolterrors.ErrBucketNotFound) {
			return err
		}
		idx, err := tx.CreateBucket(bucketAnomalyIndex)
		if err != nil {
			return err
		}
		open, err := tx.CreateBucket(bucketAnomalyOpen)
		if err != nil {
			return err
		}
		for i := range records {
			r := records[i]
			if err := idx.Put(anomalyTimeIndexKey(r.DetectedAt, r.ID), []byte(r.ID)); err != nil {
				return err
			}
			if isOpenAnomalyStatus(r.Status) {
				if err := open.Put(anomalyOpenKey(r.Entity, anomalySignal(&r)), []byte(r.ID)); err != nil {
					return err
				}
			}
		}
		return nil
	})
}
