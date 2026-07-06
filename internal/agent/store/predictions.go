package store

import (
	"encoding/json"
	"fmt"

	bolt "go.etcd.io/bbolt"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

var keyLatestPredictions = []byte("latest")

// SaveLatestPredictions stores the most recent prediction batch from a scrape.
func (s *Store) SaveLatestPredictions(preds []models.PredictionResult) error {
	data, err := json.Marshal(preds)
	if err != nil {
		return fmt.Errorf("marshal predictions: %w", err)
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		b, err := tx.CreateBucketIfNotExists(bucketPredictions)
		if err != nil {
			return err
		}
		return b.Put(keyLatestPredictions, data)
	})
}

// GetLatestPredictions returns the most recent prediction batch.
func (s *Store) GetLatestPredictions() ([]models.PredictionResult, error) {
	var preds []models.PredictionResult
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketPredictions)
		if b == nil {
			return nil
		}
		data := b.Get(keyLatestPredictions)
		if data == nil {
			return nil
		}
		return json.Unmarshal(data, &preds)
	})
	return preds, err
}
