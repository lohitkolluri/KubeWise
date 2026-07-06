package store

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func TestAnomalyIndexes_ListAndOpenLookup(t *testing.T) {
	dir := t.TempDir()
	s, err := Open(filepath.Join(dir, "agent.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	now := time.Now()
	_ = s.SaveAnomaly(&models.AnomalyRecord{
		ID: "a1", Entity: "demo/pod-1", Namespace: "demo",
		MetricName: "cpu", Status: models.AnomalyStatusDetected, DetectedAt: &now,
	})
	_ = s.SaveAnomaly(&models.AnomalyRecord{
		ID: "a2", Entity: "demo/pod-2", Namespace: "demo",
		MetricName: "mem", Status: models.AnomalyStatusRejected,
		DetectedAt: ptrTime(now.Add(-time.Minute)),
	})

	list, err := s.ListAnomalies(10)
	if err != nil || len(list) != 2 {
		t.Fatalf("list: len=%d err=%v", len(list), err)
	}
	if list[0].ID != "a1" {
		t.Fatalf("expected newest first, got %s", list[0].ID)
	}

	open, err := s.FindOpenAnomaly("demo/pod-1", "cpu")
	if err != nil || open == nil || open.ID != "a1" {
		t.Fatalf("FindOpenAnomaly open: %+v err=%v", open, err)
	}
	miss, err := s.FindOpenAnomaly("demo/pod-2", "mem")
	if err != nil || miss != nil {
		t.Fatalf("expected no open anomaly for rejected record, got %+v err=%v", miss, err)
	}
}

func ptrTime(t time.Time) *time.Time { return &t }
