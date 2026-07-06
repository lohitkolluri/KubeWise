package namespace

import "testing"

func TestInScope(t *testing.T) {
	if !InScope("demo", nil) || !InScope("demo", []string{}) {
		t.Fatal("empty watch should allow all")
	}
	if InScope("prod", []string{"demo"}) {
		t.Fatal("out of scope namespace should be rejected")
	}
	if !InScope("demo", []string{"demo", "staging"}) {
		t.Fatal("listed namespace should be allowed")
	}
}
