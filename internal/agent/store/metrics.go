package store

import (
	"encoding/binary"
	"math"
	"time"

	bolt "go.etcd.io/bbolt"
)

const maxSamplesPerMetric = 10080 // 7 days at 1-minute intervals

// MetricPoint represents a single metric sample.
type MetricPoint struct {
	Value float64   `json:"value"`
	TS    time.Time `json:"ts"`
}

func itob(v int64) []byte {
	b := make([]byte, 8)
	binary.BigEndian.PutUint64(b, uint64(v))
	return b
}

func btoi(b []byte) int64 {
	return int64(binary.BigEndian.Uint64(b))
}

// AppendMetric adds a sample to a metric's ring buffer.
func (s *Store) AppendMetric(name string, value float64, ts time.Time) error {
	return s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketMetrics).Bucket([]byte(name))
		if b == nil {
			var err error
			b, err = tx.Bucket(bucketMetrics).CreateBucket([]byte(name))
			if err != nil {
				return err
			}
		}

		key := itob(ts.UnixNano())
		val := make([]byte, 8)
		binary.LittleEndian.PutUint64(val, float64bits(value))

		if err := b.Put(key, val); err != nil {
			return err
		}

		// Ring buffer: remove oldest if over limit
		c := b.Cursor()
		count := 0
		for k, _ := c.First(); k != nil; k, _ = c.Next() {
			count++
		}
		if count > maxSamplesPerMetric {
			k, _ := c.First()
			if k != nil {
				b.Delete(k)
			}
		}
		return nil
	})
}

// GetMetrics returns the last n samples for a metric.
func (s *Store) GetMetrics(name string, n int) ([]MetricPoint, error) {
	if n <= 0 {
		return nil, nil
	}
	var points []MetricPoint
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketMetrics).Bucket([]byte(name))
		if b == nil {
			return nil
		}
		c := b.Cursor()
		k, v := c.Last()
		for k != nil && len(points) < n {
			points = append(points, MetricPoint{
				Value: float64frombits(binary.LittleEndian.Uint64(v)),
				TS:    time.Unix(0, btoi(k)),
			})
			k, v = c.Prev()
		}
		return nil
	})
	// Reverse so oldest is first
	for i, j := 0, len(points)-1; i < j; i, j = i+1, j-1 {
		points[i], points[j] = points[j], points[i]
	}
	return points, err
}

// TrimOlderThan removes metric samples older than the given duration.
func (s *Store) TrimOlderThan(d time.Duration) error {
	cutoff := time.Now().Add(-d)
	cutoffKey := itob(cutoff.UnixNano())
	return s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketMetrics)
		return b.ForEach(func(name, _ []byte) error {
			mb := b.Bucket(name)
			if mb == nil {
				return nil
			}
				c := mb.Cursor()
			for k, _ := c.First(); k != nil; k, _ = c.First() {
				if btoi(k) >= btoi(cutoffKey) {
					break
				}
				if err := mb.Delete(k); err != nil {
					return err
				}
			}
			return nil
		})
	})
}

func float64bits(f float64) uint64 {
	return math.Float64bits(f)
}

func float64frombits(bits uint64) float64 {
	return math.Float64frombits(bits)
}
