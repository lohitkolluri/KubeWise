package store

import (
	bolt "go.etcd.io/bbolt"
)

// Bucket names used by the store.
var (
	bucketMetrics      = []byte("metrics")
	bucketAnomalies    = []byte("anomalies")
	bucketRemediations = []byte("remediations")
	bucketAuditLog     = []byte("audit_log")
	bucketConfig       = []byte("config")
)

// Init ensures all required buckets exist.
func (s *Store) Init() error {
	return s.db.Update(func(tx *bolt.Tx) error {
		for _, b := range [][]byte{
			bucketMetrics,
			bucketAnomalies,
			bucketRemediations,
			bucketAuditLog,
			bucketConfig,
		} {
			if _, err := tx.CreateBucketIfNotExists(b); err != nil {
				return err
			}
		}
		return nil
	})
}
