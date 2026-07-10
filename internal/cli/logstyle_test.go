package cli

import (
	"strings"
	"testing"
)

func TestColorizeLogLine(t *testing.T) {
	tests := []struct {
		line  string
		level logLevel
		ok    bool
	}{
		{"INFO agent: started interval=30s", logLevelInfo, true},
		{"ERROR remediator: failed err=timeout", logLevelError, true},
		{"WARN gate: dropped count=3", logLevelWarn, true},
		{"DEBUG predictor: scoring", logLevelDebug, true},
		{"plain status line", 0, false},
	}
	for _, tc := range tests {
		gotLevel, ok := detectLogLevel(tc.line)
		if ok != tc.ok {
			t.Fatalf("detectLogLevel(%q) ok=%v want %v", tc.line, ok, tc.ok)
		}
		if ok && gotLevel != tc.level {
			t.Fatalf("detectLogLevel(%q)=%v want %v", tc.line, gotLevel, tc.level)
		}
		out := colorizeLogLine(tc.line)
		if out == "" {
			t.Fatalf("colorizeLogLine returned empty for %q", tc.line)
		}
	}
	joined := colorizeLogs("INFO one\nERROR two\n")
	if !strings.Contains(joined, "one") || !strings.Contains(joined, "two") {
		t.Fatalf("colorizeLogs dropped content: %q", joined)
	}
}
