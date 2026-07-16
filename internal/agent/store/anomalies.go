package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	bolt "go.etcd.io/bbolt"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

var errLimitReached = errors.New("limit reached")

func anomalyTimeIndexKey(t *time.Time, id string) []byte {
	if t == nil || t.IsZero() {
		// Use max value (all 1s inverted = all 0s) for nil/zero timestamps so they sort last.
		return []byte(fmt.Sprintf("%016x|%s", ^uint64(0), id))
	}
	return timeIndexKey(*t, id)
}

func anomalyOpenKey(entity, signal string) []byte {
	return []byte(entity + "|" + signal)
}

func auditTimeIndexKey(t time.Time, id string) []byte {
	return timeIndexKey(t, id)
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
	return s.db.Update(func(tx *bolt.Tx) error {
		_, err := s.saveAnomalyTx(tx, r)
		return err
	})
}

// saveAnomalyTx stores an anomaly record inside an existing transaction.
func (s *Store) saveAnomalyTx(tx *bolt.Tx, r *models.AnomalyRecord) (bool, error) {
	if r.ID == "" {
		return false, errors.New("anomaly ID must not be empty")
	}
	data, err := json.Marshal(r)
	if err != nil {
		return false, fmt.Errorf("marshal anomaly: %w", err)
	}
	if err := tx.Bucket(bucketAnomalies).Put([]byte(r.ID), data); err != nil {
		return false, err
	}
	idx, err := tx.CreateBucketIfNotExists(bucketAnomalyIndex)
	if err != nil {
		return false, err
	}
	if err := idx.Put(anomalyTimeIndexKey(r.DetectedAt, r.ID), []byte(r.ID)); err != nil {
		return false, err
	}
	return true, s.syncAnomalyOpenIndex(tx, r, nil)
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
			if len(records) >= maxRecordsInMemory {
				return fmt.Errorf("too many anomaly records (>= %d); use filtering to narrow results", maxRecordsInMemory)
			}
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

// ListAnomaliesByOwner returns anomalies matching an owner kind and name, up to limit.
// This enables querying anomalies across pod restarts for the same owner (e.g. a Deployment).
func (s *Store) ListAnomaliesByOwner(ownerKind, ownerName string, limit int) ([]models.AnomalyRecord, error) {
	if limit <= 0 {
		limit = 20
	}
	var records []models.AnomalyRecord
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketAnomalies)
		if b == nil {
			return nil
		}
		return b.ForEach(func(_, v []byte) error {
			var r models.AnomalyRecord
			if err := json.Unmarshal(v, &r); err != nil {
				return nil // skip corrupt records
			}
			if r.OwnerKind == ownerKind && r.OwnerName == ownerName {
				records = append(records, r)
				if len(records) >= limit {
					return errLimitReached
				}
			}
			return nil
		})
	})
	if errors.Is(err, errLimitReached) {
		return records, nil
	}
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
	return s.db.Update(func(tx *bolt.Tx) error {
		_, err := s.updateAnomalyTx(tx, r)
		return err
	})
}

// updateAnomalyTx updates an anomaly record inside an existing transaction.
func (s *Store) updateAnomalyTx(tx *bolt.Tx, r *models.AnomalyRecord) (bool, error) {
	if r.ID == "" {
		return false, errors.New("anomaly ID must not be empty")
	}
	data, err := json.Marshal(r)
	if err != nil {
		return false, fmt.Errorf("marshal anomaly: %w", err)
	}
	main := tx.Bucket(bucketAnomalies)
	if main == nil || main.Get([]byte(r.ID)) == nil {
		return false, fmt.Errorf("anomaly %s not found", r.ID)
	}
	var prev models.AnomalyRecord
	_ = json.Unmarshal(main.Get([]byte(r.ID)), &prev)

	if err := main.Put([]byte(r.ID), data); err != nil {
		return false, err
	}
	idx := tx.Bucket(bucketAnomalyIndex)
	if idx != nil {
		_ = idx.Delete(anomalyTimeIndexKey(prev.DetectedAt, r.ID))
		if err := idx.Put(anomalyTimeIndexKey(r.DetectedAt, r.ID), []byte(r.ID)); err != nil {
			return false, err
		}
	}
	return true, s.syncAnomalyOpenIndex(tx, r, &prev)
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
// This is atomic: find, compare, and save/update all happen in a single bbolt Update transaction.
func (s *Store) UpsertOpenAnomaly(r *models.AnomalyRecord) (bool, error) {
	signal := anomalySignal(r)
	var created bool
	err := s.db.Update(func(tx *bolt.Tx) error {
		c, err := s.upsertOpenAnomalyTx(tx, r, signal)
		if err != nil {
			return err
		}
		created = c
		return nil
	})
	return created, err
}

// upsertOpenAnomalyTx performs the upsert inside an existing transaction.
// It returns true if a new record was created, false if an existing one was updated.
func (s *Store) upsertOpenAnomalyTx(tx *bolt.Tx, r *models.AnomalyRecord, signal string) (bool, error) {
	// Look up the existing open anomaly within this same transaction.
	var existing *models.AnomalyRecord
	if open := tx.Bucket(bucketAnomalyOpen); open != nil {
		if data := open.Get(anomalyOpenKey(r.Entity, signal)); data != nil {
			if main := tx.Bucket(bucketAnomalies); main != nil {
				if raw := main.Get(data); raw != nil {
					var rec models.AnomalyRecord
					if err := json.Unmarshal(raw, &rec); err == nil {
						existing = &rec
					}
				}
			}
		}
	}

	if existing != nil {
		scoreIncreased := r.Score > existing.Score
		existing.Score = r.Score
		existing.DetectedAt = r.DetectedAt
		if r.MetricName != "" {
			existing.MetricName = r.MetricName
		}
		if r.Pattern != "" {
			existing.Pattern = r.Pattern
		}
		if scoreIncreased {
			existing.Status = models.AnomalyStatusDetected
		}
		_, err := s.updateAnomalyTx(tx, existing)
		return false, err
	}
	_, err := s.saveAnomalyTx(tx, r)
	return true, err
}

type pruneTarget struct {
	id         string
	detectedAt *time.Time
	status     string
}

// PruneAnomalies removes resolved/remediated anomalies older than 7 days.
// Returns the count of removed records.
func (s *Store) PruneAnomalies() (int, error) {
	cutoff := time.Now().Add(-7 * 24 * time.Hour)
	var toDelete []pruneTarget
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketAnomalies)
		if b == nil {
			return nil
		}
		return b.ForEach(func(k, v []byte) error {
			var r models.AnomalyRecord
			if err := json.Unmarshal(v, &r); err != nil {
				return nil
			}
			if r.Status != models.AnomalyStatusRemediated && r.Status != models.AnomalyStatusResolved {
				return nil
			}
			if r.DetectedAt == nil || r.DetectedAt.IsZero() || !r.DetectedAt.Before(cutoff) {
				return nil
			}
			toDelete = append(toDelete, pruneTarget{id: r.ID, detectedAt: r.DetectedAt, status: r.Status})
			return nil
		})
	})
	if err != nil || len(toDelete) == 0 {
		return 0, err
	}
	err = s.db.Update(func(tx *bolt.Tx) error {
		main := tx.Bucket(bucketAnomalies)
		idx := tx.Bucket(bucketAnomalyIndex)
		open := tx.Bucket(bucketAnomalyOpen)
		for _, d := range toDelete {
			if err := main.Delete([]byte(d.id)); err != nil {
				return err
			}
			if idx != nil {
				_ = idx.Delete(anomalyTimeIndexKey(d.detectedAt, d.id))
			}
			if open != nil && isOpenAnomalyStatus(d.status) {
				// Open index entry carries entity+signal we don't have here;
				// stale entries are harmless and rebuilt on next Open.
			}
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return len(toDelete), nil
}

// RebuildAnomalyIndexes rebuilds indexes from the anomalies bucket (migration helper).
func (s *Store) RebuildAnomalyIndexes() error {
	records, err := s.listAllAnomalies()
	if err != nil {
		return err
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		if err := tx.DeleteBucket(bucketAnomalyIndex); err != nil && !errors.Is(err, bolt.ErrBucketNotFound) {
			return err
		}
		if err := tx.DeleteBucket(bucketAnomalyOpen); err != nil && !errors.Is(err, bolt.ErrBucketNotFound) {
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
