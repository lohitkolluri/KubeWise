package promptctx

import "testing"

func TestBuildLogQLCandidates(t *testing.T) {
	t.Parallel()

	got := buildLogQLCandidates("kubewise", "pod-1")
	if len(got) != 2 {
		t.Fatalf("got %d candidates, want 2", len(got))
	}
	if got[0] != `{namespace="kubewise", pod="pod-1"} |~ "(?i)error|panic|oom|fail|exception|crash|killed|evicted|backoff"` {
		t.Errorf("primary query = %q", got[0])
	}
	if got[1] != `{pod="pod-1"} |~ "(?i)error|panic|oom|fail|exception|crash|killed|evicted|backoff"` {
		t.Errorf("fallback query = %q", got[1])
	}
}

func TestBuildLogQLCandidatesPodOnly(t *testing.T) {
	t.Parallel()

	got := buildLogQLCandidates("", "pod-1")
	if len(got) != 1 || got[0] != `{pod="pod-1"} |~ "(?i)error|panic|oom|fail|exception|crash|killed|evicted|backoff"` {
		t.Fatalf("unexpected candidates: %#v", got)
	}
}

func TestBuildTempoTagCandidates(t *testing.T) {
	t.Parallel()

	got := buildTempoTagCandidates("default", "nginx-abc")
	if len(got) < 3 {
		t.Fatalf("expected at least 3 tag candidates, got %d", len(got))
	}
	if got[0] != "namespace=default pod=nginx-abc" {
		t.Errorf("primary tags = %q", got[0])
	}
}
