package cli

import (
	"strings"
	"testing"

	"github.com/charmbracelet/lipgloss"
)

func TestFormatDetailKVWrapsValue(t *testing.T) {
	width := 40
	got := formatDetailKV(width, "Root cause:", strings.Repeat("x", 60), detailValueStyle)
	if !strings.Contains(got, "\n") {
		t.Fatalf("expected wrapped output, got %q", got)
	}
	lines := strings.Split(got, "\n")
	if len(lines) < 2 {
		t.Fatalf("expected multiple lines, got %d", len(lines))
	}
	labelW := lipgloss.Width(detailLabelStyle.Render("Root cause:"))
	gutter := strings.Repeat(" ", labelW+1)
	if !strings.HasPrefix(lines[1], gutter) {
		t.Fatalf("continuation line should align under value column, got %q want prefix %q", lines[1], gutter)
	}
}

func TestWriteDetailSectionNoDoublePadding(t *testing.T) {
	var b strings.Builder
	writeDetailSection(&b, "Diagnosis")
	writeDetailKV(&b, 60, "Root cause:", "pod crash")
	out := b.String()
	if strings.Contains(out, "\n\n\n") {
		t.Fatalf("unexpected extra blank lines: %q", out)
	}
	if !strings.Contains(out, "── Diagnosis ──") {
		t.Fatalf("missing section header: %q", out)
	}
}
