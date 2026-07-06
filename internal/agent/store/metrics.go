package store

import (
	"encoding/binary"
	"fmt"
	"math"
	"strings"
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

// seriesKey builds a unique storage key from metric name and optional entity labels.
func seriesKey(name string, labels map[string]string) string {
	if len(labels) == 0 {
		return name
	}
	key := name
	for _, k := range []string{"pod", "container", "namespace", "node", "instance"} {
		if v, ok := labels[k]; ok && v != "" {
			key += "/" + k + "=" + v
		}
	}
	return key
}

// AppendMetric adds a sample to a metric's ring buffer (legacy: no labels).
func (s *Store) AppendMetric(name string, value float64, ts time.Time) error {
	return s.AppendMetricSeries(name, nil, value, ts)
}

// AppendMetricSeries adds a sample for a labelled metric series.
func (s *Store) AppendMetricSeries(name string, labels map[string]string, value float64, ts time.Time) error {
	key := seriesKey(name, labels)
	return s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketMetrics).Bucket([]byte(key))
		if b == nil {
			var err error
			b, err = tx.Bucket(bucketMetrics).CreateBucket([]byte(key))
			if err != nil {
				return err
			}
		}

		tsKey := itob(ts.UnixNano())
		val := make([]byte, 8)
		binary.LittleEndian.PutUint64(val, float64bits(value))

		if err := b.Put(tsKey, val); err != nil {
			return err
		}

		// Ring buffer: remove oldest if over limit
		c := b.Cursor()
		count := 0
		for k, _ := c.First(); k != nil; k, _ = c.Next() {
			count++
		}
		if count > maxSamplesPerMetric {
			toDelete := count - maxSamplesPerMetric
			for i := 0; i < toDelete; i++ {
				k, _ := c.First()
				if k == nil {
					break
				}
				if err := b.Delete(k); err != nil {
					return err
				}
			}
		}
		return nil
	})
}

// GetMetrics returns the last n samples for a metric (legacy: unlabelled series).
func (s *Store) GetMetrics(name string, n int) ([]MetricPoint, error) {
	return s.GetMetricSeries(name, nil, n)
}

// GetMetricSeries returns the last n samples for a labelled metric series.
func (s *Store) GetMetricSeries(name string, labels map[string]string, n int) ([]MetricPoint, error) {
	if n <= 0 {
		return nil, nil
	}
	key := seriesKey(name, labels)
	var points []MetricPoint
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketMetrics).Bucket([]byte(key))
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

// ListMetricSeries returns series keys for a metric name prefix.
func (s *Store) ListMetricSeries(metricName string) ([]string, error) {
	var keys []string
	err := s.db.View(func(tx *bolt.Tx) error {
		return tx.Bucket(bucketMetrics).ForEach(func(k, _ []byte) error {
			key := string(k)
			if key == metricName || strings.HasPrefix(key, metricName+"/") {
				keys = append(keys, key)
			}
			return nil
		})
	})
	return keys, err
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

// ParseSeriesKey splits a stored series key into metric name and label components.
func ParseSeriesKey(key string) (string, map[string]string, error) {
	parts := strings.Split(key, "/")
	if len(parts) == 0 {
		return "", nil, fmt.Errorf("empty series key")
	}
	name := parts[0]
	labels := make(map[string]string)
	if len(parts) == 1 {
		return name, labels, nil
	}
	if strings.Contains(parts[1], "=") {
		for _, part := range parts[1:] {
			k, v, ok := strings.Cut(part, "=")
			if ok && k != "" {
				labels[k] = v
			}
		}
		return name, labels, nil
	}
	// Legacy positional keys (container slot may be omitted).
	labelOrder := []string{"pod", "container", "namespace", "node", "instance"}
	rest := parts[1:]
	if len(rest) == 2 {
		labels["pod"] = rest[0]
		labels["namespace"] = rest[1]
		return name, labels, nil
	}
	for i, part := range rest {
		if i < len(labelOrder) {
			labels[labelOrder[i]] = part
		}
	}
	return name, labels, nil
}

// ListMetricNames returns distinct metric names stored in the metrics bucket.
func (s *Store) ListMetricNames() ([]string, error) {
	seen := make(map[string]struct{})
	var names []string
	err := s.db.View(func(tx *bolt.Tx) error {
		return tx.Bucket(bucketMetrics).ForEach(func(k, _ []byte) error {
			metricName, _, err := ParseSeriesKey(string(k))
			if err != nil {
				return nil
			}
			if _, ok := seen[metricName]; !ok {
				seen[metricName] = struct{}{}
				names = append(names, metricName)
			}
			return nil
		})
	})
	return names, err
}
