package api

import (
	"strings"
	"testing"
)

func TestRedactSensitive_BearerToken(t *testing.T) {
	in := "request failed: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
	got := redactSensitive(in)
	if strings.Contains(got, "eyJ") {
		t.Fatalf("token leaked: %q", got)
	}
	if !strings.Contains(got, "Bearer ***") {
		t.Fatalf("expected redacted bearer, got %q", got)
	}
}
