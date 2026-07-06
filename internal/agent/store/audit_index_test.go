package store

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func TestAuditIndexes_ListAndStatusLookup(t *testing.T) {
	dir := t.TempDir()
	s, err := Open(filepath.Join(dir, "agent.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	now := time.Now()
	_ = s.SaveAuditRecord(&models.AuditRecord{
		ID: "r1", Status: models.AuditPending, CreatedAt: now,
	})
	_ = s.SaveAuditRecord(&models.AuditRecord{
		ID: "r2", Status: models.AuditExecuted, CreatedAt: now.Add(-time.Minute),
	})

	list, err := s.ListAuditRecords(10)
	if err != nil || len(list) != 2 {
		t.Fatalf("list: len=%d err=%v", len(list), err)
	}
	if list[0].ID != "r1" {
		t.Fatalf("expected newest first, got %s", list[0].ID)
	}

	pending, err := s.ListAuditRecordsByStatus(models.AuditPending, 10)
	if err != nil || len(pending) != 1 || pending[0].ID != "r1" {
		t.Fatalf("pending: %+v err=%v", pending, err)
	}

	// Simulate legacy DB without indexes.
	if err := s.RebuildAuditIndexes(); err != nil {
		t.Fatal(err)
	}
	list2, err := s.ListAuditRecords(10)
	if err != nil || len(list2) != 2 {
		t.Fatalf("after rebuild list: len=%d err=%v", len(list2), err)
	}
}
