package store

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func TestGetAuditRecord_ExactMatchOnly(t *testing.T) {
	dir := t.TempDir()
	s, err := Open(filepath.Join(dir, "agent.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	now := time.Now()
	_ = s.SaveAuditRecord(&models.AuditRecord{
		ID:        "abc123-full-id",
		Status:    models.AuditDryRun,
		CreatedAt: now,
		Plan: models.RemediationPlan{
			Action: models.Action{Type: "noop", Namespace: "demo", Target: "pod", Rationale: "test"},
		},
	})

	if _, err := s.GetAuditRecord("abc"); err == nil {
		t.Fatal("expected prefix lookup to fail")
	}
	rec, err := s.GetAuditRecord("abc123-full-id")
	if err != nil || rec == nil {
		t.Fatalf("expected exact match, err=%v", err)
	}
}
