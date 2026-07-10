package cli

import (
	"testing"
	"time"
)

func TestToastStackPushPrune(t *testing.T) {
	var tstack toastStack
	tstack.push("hello", toastSuccess, time.Second)
	tstack.push("warn", toastWarn, time.Second)
	if len(tstack.items) != 2 {
		t.Fatalf("expected 2 toasts, got %d", len(tstack.items))
	}
	tstack.prune(time.Now().Add(2 * time.Second))
	if len(tstack.items) != 0 {
		t.Fatalf("expected pruned toasts, got %d", len(tstack.items))
	}
	if strip := tstack.strip(); strip != "" {
		t.Fatalf("expected empty strip, got %q", strip)
	}
}
