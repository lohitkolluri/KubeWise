package store

import (
	"fmt"
	"io"
	"time"

	bolt "go.etcd.io/bbolt"
)

// Store wraps a bbolt database for KubeWise agent storage.
type Store struct {
	db *bolt.DB
}

// Open opens (or creates) the bbolt database at the given path.
func Open(path string) (*Store, error) {
	db, err := bolt.Open(path, 0o600, &bolt.Options{Timeout: 5 * time.Second})
	if err != nil {
		return nil, fmt.Errorf("open bbolt: %w", err)
	}
	s := &Store{db: db}
	if err := s.Init(); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("init bbolt: %w", err)
	}
	if err := s.ensureAnomalyIndexes(); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("rebuild anomaly indexes: %w", err)
	}
	if err := s.ensureAuditIndexes(); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("rebuild audit indexes: %w", err)
	}
	return s, nil
}

func (s *Store) ensureIndexes(mainBucket, indexBucket []byte, rebuildFn func() error) error {
	need := false
	_ = s.db.View(func(tx *bolt.Tx) error {
		main := tx.Bucket(mainBucket)
		idx := tx.Bucket(indexBucket)
		if main != nil && main.Stats().KeyN > 0 && (idx == nil || idx.Stats().KeyN == 0) {
			need = true
		}
		return nil
	})
	if need {
		return rebuildFn()
	}
	return nil
}

func (s *Store) ensureAuditIndexes() error {
	return s.ensureIndexes(bucketAuditLog, bucketAuditIndex, s.RebuildAuditIndexes)
}

func (s *Store) ensureAnomalyIndexes() error {
	return s.ensureIndexes(bucketAnomalies, bucketAnomalyIndex, s.RebuildAnomalyIndexes)
}

// Backup writes a consistent snapshot of the bbolt database to w.
// Note: db.View() captures a consistent snapshot via tx.WriteTo(), which
// serializes the entire database into memory. For large databases (>100MB),
// this can be memory-intensive. The snapshot is written incrementally to w
// during serialization, but the full b-tree must be traversed.
func (s *Store) Backup(w io.Writer) error {
	return s.db.View(func(tx *bolt.Tx) error {
		_, err := tx.WriteTo(w)
		return err
	})
}

// Ping verifies the database is readable.
func (s *Store) Ping() error {
	return s.db.View(func(_ *bolt.Tx) error { return nil })
}

// Close shuts down the database.
func (s *Store) Close() error {
	return s.db.Close()
}
