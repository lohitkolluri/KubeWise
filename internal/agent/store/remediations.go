package store

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
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
		if err := b.Put([]byte(r.ID), data); err != nil {
			return err
		}
		idx, err := tx.CreateBucketIfNotExists(bucketAuditIndex)
		if err != nil {
			return err
		}
		if err := idx.Put(auditTimeIndexKey(r.CreatedAt, r.ID), []byte(r.ID)); err != nil {
			return err
		}
		st, err := tx.CreateBucketIfNotExists(bucketAuditStatus)
		if err != nil {
			return err
		}
		return st.Put(auditStatusIndexKey(r.Status, r.CreatedAt, r.ID), []byte(r.ID))
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
	var records []models.AuditRecord
	err := s.db.View(func(tx *bolt.Tx) error {
		idx := tx.Bucket(bucketAuditIndex)
		main := tx.Bucket(bucketAuditLog)
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
			var r models.AuditRecord
			if err := json.Unmarshal(data, &r); err != nil {
				return err
			}
			records = append(records, r)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	if len(records) == 0 {
		return s.listAllAuditRecordsLimited(limit)
	}
	return records, nil
}

func (s *Store) listAllAuditRecordsLimited(limit int) ([]models.AuditRecord, error) {
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
		var prev models.AuditRecord
		_ = json.Unmarshal(b.Get([]byte(r.ID)), &prev)
		if err := b.Put([]byte(r.ID), data); err != nil {
			return err
		}
		if idx := tx.Bucket(bucketAuditIndex); idx != nil {
			_ = idx.Delete(auditTimeIndexKey(prev.CreatedAt, r.ID))
			_ = idx.Put(auditTimeIndexKey(r.CreatedAt, r.ID), []byte(r.ID))
		}
		if st := tx.Bucket(bucketAuditStatus); st != nil {
			_ = st.Delete(auditStatusIndexKey(prev.Status, prev.CreatedAt, r.ID))
			_ = st.Put(auditStatusIndexKey(r.Status, r.CreatedAt, r.ID), []byte(r.ID))
		}
		return nil
	})
}

// ListAuditRecordsByStatus returns records matching status up to limit.
func (s *Store) ListAuditRecordsByStatus(status models.AuditStatus, limit int) ([]models.AuditRecord, error) {
	if limit <= 0 {
		limit = 20
	}
	prefix := []byte(string(status) + "|")
	var out []models.AuditRecord
	err := s.db.View(func(tx *bolt.Tx) error {
		st := tx.Bucket(bucketAuditStatus)
		main := tx.Bucket(bucketAuditLog)
		if st == nil || main == nil {
			return nil
		}
		c := st.Cursor()
		for k, v := c.Seek(prefix); k != nil && len(out) < limit; k, v = c.Next() {
			if !strings.HasPrefix(string(k), string(prefix)) {
				break
			}
			id := string(v)
			if id == "" {
				parts := strings.Split(string(k), "|")
				if len(parts) >= 3 {
					id = parts[2]
				}
			}
			data := main.Get([]byte(id))
			if data == nil {
				continue
			}
			var r models.AuditRecord
			if err := json.Unmarshal(data, &r); err != nil {
				return err
			}
			out = append(out, r)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	if len(out) == 0 {
		return s.listAuditByStatusLegacy(status, limit)
	}
	return out, nil
}

func (s *Store) listAuditByStatusLegacy(status models.AuditStatus, limit int) ([]models.AuditRecord, error) {
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
