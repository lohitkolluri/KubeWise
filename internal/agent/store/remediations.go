package store

import (
	"encoding/json"
	"fmt"
	"sort"
	"time"

	bolt "go.etcd.io/bbolt"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// SaveAuditRecord persists a remediation audit record.
func (s *Store) SaveAuditRecord(r *models.AuditRecord) error {
	if r.ID == "" {
		return fmt.Errorf("audit record ID must not be empty")
	}
	data, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("marshal audit record: %w", err)
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		b, err := tx.CreateBucketIfNotExists(bucketAuditLog)
		if err != nil {
			return err
		}
		return b.Put([]byte(r.ID), data)
	})
}

func (s *Store) listAllAuditRecords() ([]models.AuditRecord, error) {
	var records []models.AuditRecord
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketAuditLog)
		if b == nil {
			return nil
		}
		return b.ForEach(func(_, v []byte) error {
			var r models.AuditRecord
			if err := json.Unmarshal(v, &r); err != nil {
				return fmt.Errorf("unmarshal audit record: %w", err)
			}
			records = append(records, r)
			return nil
		})
	})
	if err != nil {
		return nil, err
	}
	sort.Slice(records, func(i, j int) bool {
		return records[i].CreatedAt.After(records[j].CreatedAt)
	})
	return records, nil
}

// ListAuditRecords returns the most recent audit records up to limit.
func (s *Store) ListAuditRecords(limit int) ([]models.AuditRecord, error) {
	if limit <= 0 {
		return nil, nil
	}
	records, err := s.listAllAuditRecords()
	if err != nil {
		return nil, err
	}
	if len(records) > limit {
		records = records[:limit]
	}
	return records, nil
}

// ListAuditRecordsSince returns audit records created after a given time.
func (s *Store) ListAuditRecordsSince(since time.Time, limit int) ([]models.AuditRecord, error) {
	if limit <= 0 {
		return nil, nil
	}
	records, err := s.listAllAuditRecords()
	if err != nil {
		return nil, err
	}
	var filtered []models.AuditRecord
	for _, r := range records {
		if r.CreatedAt.After(since) || r.CreatedAt.Equal(since) {
			filtered = append(filtered, r)
		}
		if len(filtered) >= limit {
			break
		}
	}
	return filtered, nil
}

// GetAuditRecord returns a single audit record by exact ID.
func (s *Store) GetAuditRecord(id string) (*models.AuditRecord, error) {
	if id == "" {
		return nil, fmt.Errorf("audit record ID must not be empty")
	}
	var record models.AuditRecord
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketAuditLog)
		if b == nil {
			return fmt.Errorf("audit record %q not found", id)
		}
		data := b.Get([]byte(id))
		if data == nil {
			return fmt.Errorf("audit record %q not found", id)
		}
		return json.Unmarshal(data, &record)
	})
	if err != nil {
		return nil, err
	}
	return &record, nil
}

// UpdateAuditRecord overwrites an existing audit record.
func (s *Store) UpdateAuditRecord(r *models.AuditRecord) error {
	if r == nil || r.ID == "" {
		return fmt.Errorf("audit record ID must not be empty")
	}
	data, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("marshal audit record: %w", err)
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketAuditLog)
		if b == nil {
			return fmt.Errorf("audit bucket missing")
		}
		if b.Get([]byte(r.ID)) == nil {
			return fmt.Errorf("audit record %q not found", r.ID)
		}
		return b.Put([]byte(r.ID), data)
	})
}

// ListAuditRecordsByStatus returns records matching status up to limit.
func (s *Store) ListAuditRecordsByStatus(status models.AuditStatus, limit int) ([]models.AuditRecord, error) {
	if limit <= 0 {
		limit = 20
	}
	records, err := s.listAllAuditRecords()
	if err != nil {
		return nil, err
	}
	var out []models.AuditRecord
	for _, r := range records {
		if r.Status == status {
			out = append(out, r)
			if len(out) >= limit {
				break
			}
		}
	}
	return out, nil
}
