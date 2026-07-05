package store

import (
	"encoding/json"
	"fmt"
	"time"

	bolt "go.etcd.io/bbolt"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

const (
	auditBucket = "audit_log"
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
		b, err := tx.CreateBucketIfNotExists([]byte(auditBucket))
		if err != nil {
			return err
		}
		return b.Put([]byte(r.ID), data)
	})
}

// ListAuditRecords returns the most recent audit records up to limit.
func (s *Store) ListAuditRecords(limit int) ([]models.AuditRecord, error) {
	if limit <= 0 {
		return nil, nil
	}
	var records []models.AuditRecord
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket([]byte(auditBucket))
		if b == nil {
			return nil
		}
		c := b.Cursor()
		for k, v := c.Last(); k != nil && len(records) < limit; k, _ = c.Prev() {
			var r models.AuditRecord
			if err := json.Unmarshal(v, &r); err != nil {
				return fmt.Errorf("unmarshal audit record %s: %w", k, err)
			}
			records = append(records, r)
		}
		return nil
	})
	return records, err
}

// ListAuditRecordsSince returns audit records created after a given time.
func (s *Store) ListAuditRecordsSince(since time.Time, limit int) ([]models.AuditRecord, error) {
	if limit <= 0 {
		return nil, nil
	}
	var records []models.AuditRecord
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket([]byte(auditBucket))
		if b == nil {
			return nil
		}
		c := b.Cursor()
		for k, v := c.Last(); k != nil && len(records) < limit; k, _ = c.Prev() {
			var r models.AuditRecord
			if err := json.Unmarshal(v, &r); err != nil {
				return fmt.Errorf("unmarshal audit record %s: %w", k, err)
			}
			if r.CreatedAt.After(since) || r.CreatedAt.Equal(since) {
				records = append(records, r)
			}
		}
		return nil
	})
	return records, err
}
