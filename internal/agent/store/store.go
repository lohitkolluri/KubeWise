package store

import (
	"fmt"

	bolt "go.etcd.io/bbolt"
)

// Store wraps a bbolt database for KubeWise agent storage.
type Store struct {
	db *bolt.DB
}

// Open opens (or creates) the bbolt database at the given path.
func Open(path string) (*Store, error) {
	db, err := bolt.Open(path, 0o600, nil)
	if err != nil {
		return nil, fmt.Errorf("open bbolt: %w", err)
	}
	s := &Store{db: db}
	if err := s.Init(); err != nil {
		db.Close()
		return nil, fmt.Errorf("init bbolt: %w", err)
	}
	return s, nil
}

// Close shuts down the database.
func (s *Store) Close() error {
	return s.db.Close()
}

// DB returns the underlying bbolt handle for read/write transactions.
func (s *Store) DB() *bolt.DB {
	return s.db
}
