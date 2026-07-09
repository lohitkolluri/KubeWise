package store

import (
	"path/filepath"
	"testing"
	"time"
)

func openTestStore(t *testing.T) *Store {
	t.Helper()
	dir := t.TempDir()
	s, err := Open(filepath.Join(dir, "test.db"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func TestSaveEventDedup(t *testing.T) {
	s := openTestStore(t)
	now := time.Now().UTC().Truncate(time.Millisecond)
	first := &StoredEvent{
		ID:             "evt-100-1",
		Reason:         "BackOff",
		Message:        "back-off restarting",
		InvolvedObject: "pod-a",
		Namespace:      "default",
		FirstSeen:      now.Add(-time.Minute),
		LastSeen:       now,
		Count:          1,
	}
	if err := s.SaveEvent(first); err != nil {
		t.Fatalf("save first: %v", err)
	}
	second := &StoredEvent{
		ID:             "evt-200-2",
		Reason:         "BackOff",
		Message:        "back-off restarting",
		InvolvedObject: "pod-a",
		Namespace:      "default",
		FirstSeen:      now,
		LastSeen:       now.Add(time.Second),
		Count:          2,
	}
	if err := s.SaveEvent(second); err != nil {
		t.Fatalf("save second: %v", err)
	}

	events, err := s.ListEvents("default", time.Time{}, 10)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	byID := make(map[string]StoredEvent)
	for _, e := range events {
		byID[e.ID] = e
	}
	if len(byID) != 1 {
		t.Fatalf("expected 1 deduped event, got %d unique ids from %d rows", len(byID), len(events))
	}
	for _, e := range byID {
		if e.Count != 3 {
			t.Fatalf("count = %d, want 3", e.Count)
		}
	}
}

func TestCompactEventsRemovesOldRecordsAndIndex(t *testing.T) {
	s := openTestStore(t)
	now := time.Now().UTC().Truncate(time.Millisecond)
	old := &StoredEvent{
		ID:             "evt-1749400000000000000-1",
		Reason:         "OOMKilling",
		Message:        "memory limit",
		InvolvedObject: "pod-old",
		Namespace:      "default",
		FirstSeen:      now.Add(-48 * time.Hour),
		LastSeen:       now.Add(-48 * time.Hour),
		Count:          1,
	}
	fresh := &StoredEvent{
		ID:             "evt-1749400000000000001-2",
		Reason:         "Started",
		Message:        "started container",
		InvolvedObject: "pod-new",
		Namespace:      "default",
		FirstSeen:      now,
		LastSeen:       now,
		Count:          1,
	}
	for _, e := range []*StoredEvent{old, fresh} {
		if err := s.SaveEvent(e); err != nil {
			t.Fatalf("save %s: %v", e.ID, err)
		}
	}

	deleted, err := s.CompactEvents(24 * time.Hour)
	if err != nil {
		t.Fatalf("compact: %v", err)
	}
	if deleted != 1 {
		t.Fatalf("deleted = %d, want 1", deleted)
	}

	events, err := s.ListEvents("", time.Time{}, 10)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 remaining event, got %d", len(events))
	}
	if events[0].ID != fresh.ID {
		t.Fatalf("remaining id = %q, want %q", events[0].ID, fresh.ID)
	}
}

func TestAggregateEventsGroupsByReason(t *testing.T) {
	s := openTestStore(t)
	now := time.Now().UTC().Truncate(time.Millisecond)
	for _, obj := range []string{"pod-a", "pod-b", "pod-c"} {
		if err := s.SaveEvent(&StoredEvent{
			ID:             "evt-300-" + obj,
			Reason:         "BackOff",
			InvolvedObject: obj,
			Namespace:      "default",
			FirstSeen:      now,
			LastSeen:       now,
			Count:          1,
		}); err != nil {
			t.Fatalf("save: %v", err)
		}
	}

	groups, err := s.AggregateEvents(time.Hour)
	if err != nil {
		t.Fatalf("aggregate: %v", err)
	}
	if len(groups) != 1 {
		t.Fatalf("groups = %d, want 1", len(groups))
	}
	if groups[0].Count != 3 || groups[0].DistinctPods != 3 {
		t.Fatalf("group = %+v, want count=3 distinct_pods=3", groups[0])
	}
}

func TestIsEventID(t *testing.T) {
	if !isEventID("evt-1749400000000000000-1") {
		t.Fatal("expected evt prefix to be recognized")
	}
	if isEventID("dedup|default|pod|BackOff") {
		t.Fatal("dedup key should not be treated as event id")
	}
}
