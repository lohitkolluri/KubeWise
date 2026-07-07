package store

import (
	bolt "go.etcd.io/bbolt"
)

// Bucket names used by the store.
var (
	bucketMetrics       = []byte("metrics")
	bucketAnomalies     = []byte("anomalies")
	bucketRemediations  = []byte("remediations")
	bucketAuditLog      = []byte("audit_log")
	bucketConfig        = []byte("config")
	bucketPredictions   = []byte("predictions")
	bucketOutcomes      = []byte("outcomes")
	bucketAnomalyIndex  = []byte("anomaly_idx")
	bucketAnomalyOpen   = []byte("anomaly_open")
	bucketAuditIndex    = []byte("audit_idx")
	bucketAuditStatus   = []byte("audit_status")
	bucketHealthScores  = []byte("health_scores")
	bucketHealthHistory = []byte("health_history")
	bucketAccuracySnaps = []byte("accuracy_snapshots")
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
			bucketPredictions,
			bucketOutcomes,
			bucketAnomalyIndex,
			bucketAnomalyOpen,
			bucketAuditIndex,
			bucketAuditStatus,
			bucketHealthScores,
			bucketHealthHistory,
			bucketAccuracySnaps,
		} {
			if _, err := tx.CreateBucketIfNotExists(b); err != nil {
				return err
			}
		}
		return nil
	})
}
