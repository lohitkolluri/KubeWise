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
